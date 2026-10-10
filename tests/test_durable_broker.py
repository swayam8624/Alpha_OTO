"""Durable broker emulation across restart and ambiguous acknowledgments."""
import sqlite3
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from alpha_oto.production import D, Instrument, LedgerStore, OrderIntent, Quote, SimulationGateway, UnknownSubmission, IntegrityFailure
from alpha_oto.production.broker_durable import DurableSimulatedBroker


class DurableRemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.remote=Path(self.tmp.name)/'remote.sqlite3'
        self.local=Path(self.tmp.name)/'ledger.sqlite3'
        self.now=datetime(2026,10,10,tzinfo=timezone.utc)
        self.inst=Instrument('BTC-USD','SIMULATED','USD',D('.01'),D('.001'),D('2'))
        self.intent=OrderIntent('stable-001','BTC-USD','BUY',D('.5'),D('100'),self.now,'locked','v01')
        self.quote=Quote('BTC-USD',D('99.99'),D('100'),D('3'),D('3'),self.now,self.now)
        self.store=LedgerStore(self.local,instruments=(self.inst,))
        self.broker=DurableSimulatedBroker(self.remote)
        self.addCleanup(self._cleanup)
        self.gw=SimulationGateway(self.store,self.broker)

    def _cleanup(self):
        for x in (self.store,self.broker):
            try:x.close()
            except Exception:pass

    def restart(self):
        self.store.close();self.broker.close()
        self.store=LedgerStore(self.local,instruments=(self.inst,))
        self.broker=DurableSimulatedBroker(self.remote)
        self.gw=SimulationGateway(self.store,self.broker)

    def enter(self, timeout=False):
        self.gw.propose(self.intent,self.quote,self.now)
        self.broker.accept_then_timeout=timeout
        if timeout:
            with self.assertRaises(UnknownSubmission):
                self.gw.dispatch(self.intent)
        else:
            self.gw.dispatch(self.intent)

    def test_accept_timeout_survives_two_process_restarts_no_double_submit(self):
        self.enter(timeout=True)
        self.assertEqual(self.broker.calls,1)
        self.assertEqual(self.store.order(self.intent.client_id)['state'],'UNKNOWN')
        self.restart()
        self.assertEqual(self.broker.calls,1)
        self.gw.reconcile(now=self.now)
        self.assertEqual(self.store.order(self.intent.client_id)['state'],'ACKNOWLEDGED')
        self.gw.dispatch(self.intent)
        self.assertEqual(self.broker.calls,1)
        self.restart()
        self.assertEqual(self.broker.calls,1)
        self.assertEqual(self.gw.reconcile(now=self.now)['remote_orders'],1)

    def test_partial_fill_restarts_and_final_reconciliation(self):
        self.enter()
        self.broker.fill(self.intent.client_id,D('.2'),D('100'),fill_id='one')
        self.restart()
        self.gw.reconcile(now=self.now)
        self.assertEqual(self.store.order(self.intent.client_id)['state'],'PARTIAL')
        self.assertEqual(D(self.store.account()['cash']),D('9979.98'))
        self.broker.fill(self.intent.client_id,D('.3'),D('100'),fill_id='two')
        self.restart()
        self.assertEqual(self.gw.reconcile(now=self.now)['remote_fills'],2)
        self.assertEqual(self.store.order(self.intent.client_id)['state'],'FILLED')
        self.assertEqual(self.store.positions()['BTC-USD']['quantity'],D('.5'))
        self.assertEqual(D(self.store.account()['cash']),D('9949.95'))
        self.assertEqual(self.broker.verify()['remote_journal_events'],4)

    def test_broker_has_fill_local_does_not_then_reconcile_imports(self):
        self.enter()
        self.broker.fill(self.intent.client_id,D('.5'),D('100'),fill_id='fill-one')
        self.restart()
        self.assertEqual(self.store.positions()['BTC-USD']['quantity'],D('0'))
        self.gw.reconcile(now=self.now)
        self.assertEqual(self.store.positions()['BTC-USD']['quantity'],D('.5'))
        self.gw.reconcile(now=self.now)
        self.assertEqual(self.store.verify()['fills'],1)

    def test_conflicting_client_id_after_restart_is_blocked(self):
        self.enter()
        self.restart()
        altered=OrderIntent('stable-001','BTC-USD','BUY',D('.4'),D('100'),self.now,'locked','v01')
        with self.assertRaises(IntegrityFailure):
            self.gw.propose(altered,self.quote,self.now)
        with self.assertRaises(RuntimeError):
            self.broker.submit(altered)
        self.assertEqual(self.broker.calls,1)
        self.assertEqual(self.broker.verify()['orders'],1)

    def test_cancel_persists_and_does_not_duplicate(self):
        self.enter()
        self.broker.cancel(self.intent.client_id)
        self.restart()
        self.gw.reconcile(now=self.now)
        self.assertEqual(self.store.order(self.intent.client_id)['state'],'CANCELLED')
        self.gw.reconcile(now=self.now)
        self.assertEqual(self.broker.verify()['orders'],1)

    def test_rejected_order_persists_across_restarts(self):
        self.broker.reject_next=True
        self.enter()
        self.restart()
        self.gw.reconcile(now=self.now)
        self.assertEqual(self.store.order(self.intent.client_id)['state'],'REJECTED')
        self.assertEqual(self.broker.verify()['fills'],0)

    def test_failed_fill_is_rolled_back_with_no_new_journal_event(self):
        self.enter()
        old=self.broker.verify()
        with self.assertRaises(RuntimeError):
            self.broker.fill(self.intent.client_id,D('.5'),D('101'),fill_id='illegal')
        self.assertEqual(self.broker.verify(),old)
        self.restart()
        self.assertEqual(self.broker.verify(),old)

    def test_duplicate_fill_id_across_restarts_is_rejected(self):
        self.enter()
        self.broker.fill(self.intent.client_id,D('.2'),D('100'),fill_id='dup')
        self.restart()
        with self.assertRaises(RuntimeError):
            self.broker.fill(self.intent.client_id,D('.1'),D('100'),fill_id='dup')
        self.assertEqual(self.broker.verify()['fills'],1)
        self.gw.reconcile(now=self.now)
        self.assertEqual(D(self.store.account()['cash']),D('9979.98'))

    def test_remote_corruption_detected_and_no_submission(self):
        self.enter()
        self.broker.close()
        with sqlite3.connect(self.remote) as db:
            db.execute("UPDATE account SET state='{}' WHERE id=1")
        with self.assertRaises(IntegrityFailure):
            DurableSimulatedBroker(self.remote)
        self.broker=DurableSimulatedBroker.__new__(DurableSimulatedBroker)
        # Replace unusable handle to keep teardown deterministic.
        self.broker.close=lambda:None

    def test_read_only_pair_inspection_detects_unimported_fill(self):
        from alpha_oto.production.health import inspect_durable_pair
        self.enter()
        self.assertEqual(inspect_durable_pair(self.store,self.broker)['status'],'GREEN')
        self.broker.fill(self.intent.client_id,D('.2'),D('100'),fill_id='unimported')
        dirty=inspect_durable_pair(self.store,self.broker)
        self.assertEqual(dirty['status'],'RED')
        self.assertIn('FILL_JOURNAL_DIVERGENCE:unimported',dirty['issues'])
        self.assertFalse(dirty['safe_for_new_simulated_orders'])
        self.gw.reconcile(now=self.now)
        clean=inspect_durable_pair(self.store,self.broker)
        self.assertEqual(clean['status'],'GREEN')
        self.assertFalse(clean['live_approved'])

    def test_read_only_pair_inspection_detects_unrecognized_remote(self):
        from alpha_oto.production.health import inspect_durable_pair
        rogue=OrderIntent('surprise-001','BTC-USD','BUY',D('.1'),D('100'),self.now,'rogue','v1')
        self.broker.submit(rogue)
        issue=inspect_durable_pair(self.store,self.broker)
        self.assertEqual(issue['status'],'RED')
        self.assertIn('UNRECOGNIZED_REMOTE_ORDER:surprise-001',issue['issues'])

    def test_empty_initial_state_roundtrips(self):
        self.assertEqual(self.broker.verify()['orders'],0)
        self.restart()
        self.assertEqual(self.broker.snapshot()['cash'],Decimal('10000'))
        self.assertEqual(self.broker.verify()['remote_journal_events'],1)

    def test_lost_broker_lookup_requires_ambiguous_quarantine(self):
        self.enter(timeout=True)
        self.restart()
        self.broker.lookup_unavailable=True
        with self.assertRaises(UnknownSubmission):
            self.gw.dispatch(self.intent)
        self.assertEqual(self.store.order(self.intent.client_id)['state'],'UNKNOWN')
        self.broker.lookup_unavailable=False
        self.gw.reconcile(now=self.now)
        self.assertEqual(self.store.order(self.intent.client_id)['state'],'ACKNOWLEDGED')

    def test_separate_python_processes_survive_lost_ack_and_reconcile(self):
        # More realistic than reopening two SQLite connections in one process.
        self.broker.close();self.store.close()
        remote=Path(self.tmp.name)/'process_remote.sqlite3'
        ledger=Path(self.tmp.name)/'process_ledger.sqlite3'
        env=dict(os.environ)
        env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
        def run(command):
            proc=subprocess.run([sys.executable,'-m','alpha_oto.production',command,
                                 '--db',str(ledger),'--broker-db',str(remote)],
                                env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(proc.returncode,0,proc.stderr)
            return json.loads(proc.stdout)
        started=run('durable-start')
        self.assertEqual(started['local_state'],'UNKNOWN')
        self.assertEqual(started['broker_calls'],1)
        partial=run('durable-recover')
        self.assertEqual(partial['order']['state'],'PARTIAL')
        self.assertEqual(partial['broker_calls'],1)
        repeated=run('durable-recover')
        self.assertEqual(repeated['cash'],partial['cash'])
        complete=run('durable-complete')
        self.assertEqual(complete['order']['state'],'FILLED')
        self.assertEqual(complete['broker_calls'],1)
        self.assertEqual(complete['reconciliation']['cash'],'9949.95')
        self.assertEqual(run('durable-complete')['broker_calls'],1)
        report=run('durable-status')
        self.assertEqual(report['remote']['fills'],2)
        self.assertEqual(report['ledger']['cash'],'9949.95')
        self.assertEqual(report['audit']['status'],'GREEN')
        self.assertEqual(report['safety'],'SIMULATOR_ONLY_LIVE_ORDERS_IMPOSSIBLE')

    def test_initial_cash_mismatch_after_restart_is_rejected(self):
        self.broker.close()
        with self.assertRaises(IntegrityFailure):
            DurableSimulatedBroker(self.remote,cash=D('9000'))
        self.broker=DurableSimulatedBroker(self.remote)

    def test_tampered_remote_event_chain_detected(self):
        self.enter()
        self.broker.close()
        with sqlite3.connect(self.remote) as db:
            db.execute("UPDATE events SET previous_hash='invalid' WHERE sequence=1")
        with self.assertRaises(IntegrityFailure):
            DurableSimulatedBroker(self.remote)
        self.broker.close=lambda:None

    def test_unauthorized_external_remote_order_causes_halt(self):
        self.enter()
        rogue=OrderIntent('rogue-001','BTC-USD','BUY',D('.1'),D('100'),self.now,'unauthorized','rogue-v1')
        self.broker.submit(rogue)
        with self.assertRaises(IntegrityFailure):
            self.gw.reconcile(now=self.now)
        self.assertEqual(self.store._meta('halted'),'1')
        self.restart()
        self.assertEqual(self.store._meta('halted'),'1')

    def test_remote_order_not_resent_even_when_dispatch_called_many_times(self):
        self.enter(timeout=True)
        self.restart()
        for _ in range(5):
            self.gw.dispatch(self.intent)
        self.assertEqual(self.broker.calls,1)
        self.assertEqual(self.store.order(self.intent.client_id)['state'],'ACKNOWLEDGED')

    def test_crash_after_remote_fill_pre_accounting_and_duplicate_replay(self):
        self.enter()
        self.broker.fill(self.intent.client_id,D('.2'),D('100'),fill_id='durablefill')
        self.restart()
        self.gw.reconcile(now=self.now)
        first=self.store.verify()
        self.restart()
        self.gw.reconcile(now=self.now)
        self.assertEqual(first,self.store.verify())
        self.assertEqual(self.broker.verify()['fills'],1)


if __name__=='__main__':unittest.main()
