"""Offline risk-process, market calendar, sequenced quote and crash tests."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time as systemtime
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from alpha_oto.production.contracts import D, Instrument, IntegrityFailure, OrderIntent, Quote, RiskRejected
from alpha_oto.production.market_registry import MarketSpec,MarketRegistry
from alpha_oto.production.market_feed import DurableQuoteFeed,SequenceGap,quote_payload,parse_quote,replay_jsonl
from alpha_oto.production.risk_service import (
    IsolatedRiskAuthority,RiskClient,RiskUnavailable,SeparatedSimulationGateway,
    open_server,intent_payload,
)
from alpha_oto.production.store import LedgerStore
from alpha_oto.production.broker import SimulatedBroker
from alpha_oto.production.gateway import SimulationGateway

UTC=timezone.utc
T=datetime(2026,10,3,2,30,tzinfo=UTC)


def mkquote(ts=T,symbol='BTC-USD',bid='99.99',ask='100',quantity='10'):
    return Quote(symbol,D(bid),D(ask),D(quantity),D(quantity),ts,ts)


def mkintent(at=T,cid='sim-001',symbol='BTC-USD',side='BUY',quantity='0.5',limit='100'):
    return OrderIntent(cid,symbol,side,D(quantity),D(limit),at,'predeclared-system','frozen-v1')


def sim_registry():
    return MarketRegistry((MarketSpec('BTC-USD','SIMULATED','USD','0.01','0.001','2','UTC',True),),version='v0.8-test')


class CalendarTests(unittest.TestCase):
    def test_continuous_market_weekends_open(self):
        self.assertTrue(sim_registry().markets['BTC-USD'].is_open(T))
        self.assertTrue(sim_registry().markets['BTC-USD'].is_open(T+timedelta(days=1)))

    def test_instrument_tick_and_lot_bound(self):
        reg=sim_registry()
        with self.assertRaisesRegex(RiskRejected,'lot'):
            reg.authorize(mkintent(quantity='.0003'),mkquote(),T)
        with self.assertRaisesRegex(RiskRejected,'tick'):
            reg.authorize(mkintent(limit='100.001'),mkquote(),T)

    def test_reject_unknown_symbol(self):
        with self.assertRaises(RiskRejected):sim_registry().authorize(mkintent(symbol='ETH-USD'),mkquote(symbol='ETH-USD'),T)

    def test_session_weekends_and_holidays(self):
        m=MarketSpec('SIM-STOCK','NSE-SIM','INR','0.05','1','500','Asia/Kolkata',False,
            (0,1,2,3,4),'09:15','15:30',('2026-10-12',),'2026-10-31')
        self.assertFalse(m.is_open(datetime(2026,10,10,6,tzinfo=UTC))) # Saturday
        self.assertFalse(m.is_open(datetime(2026,10,12,6,tzinfo=UTC))) # supplied holiday
        self.assertTrue(m.is_open(datetime(2026,10,13,6,tzinfo=UTC)))
        self.assertFalse(m.is_open(datetime(2026,10,13,12,tzinfo=UTC)))
        self.assertFalse(m.is_open(datetime(2026,11,2,6,tzinfo=UTC))) # beyond verified calendar horizon

    def test_ny_daylight_saving_local_sessions(self):
        m=MarketSpec('SIM-NY','US-SIM','USD','0.01','1','10','America/New_York',False,
            (0,1,2,3,4),'09:30','16:00',(),'2026-12-31')
        self.assertTrue(m.is_open(datetime(2026,10,30,14,tzinfo=UTC)))
        self.assertTrue(m.is_open(datetime(2026,11,2,15,tzinfo=UTC)))
        self.assertFalse(m.is_open(datetime(2026,11,2,14,tzinfo=UTC)))

    def test_overnight_trading_day_date(self):
        m=MarketSpec('SIM-OVERNIGHT','SIM','USD','0.01','1','10','UTC',False,
            (4,),'22:00','02:00',(),'2026-10-16')
        self.assertTrue(m.is_open(datetime(2026,10,10,1,tzinfo=UTC))) # Fri session continues Sat
        self.assertFalse(m.is_open(datetime(2026,10,10,23,tzinfo=UTC)))

    def test_registry_digest_order_invariance(self):
        a=MarketSpec('AAA-USD','SIM','USD','0.01','1','10','UTC',True)
        b=MarketSpec('BBB-USD','SIM','USD','0.01','1','10','UTC',True)
        self.assertEqual(MarketRegistry([a,b],version='x').digest,MarketRegistry([b,a],version='x').digest)

    def test_fail_closed_invalid_session_setup(self):
        with self.assertRaises(ValueError):
            MarketSpec('BAD','SIM','USD','0.01','1','10','UTC',False,(0,),'09:15','09:15',(),'2026-12-31')
        with self.assertRaises(ValueError):
            MarketSpec('BAD','SIM','USD','0.01','1','10','UTC',False,(0,),'09:15','15:30')

    def test_reject_unknown_fields_in_registry_file(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'m.json'
            p.write_text(json.dumps({'version':'v','markets':[{'symbol':'BTC-USD','venue':'SIMULATED',
                'currency':'USD','tick':'0.01','lot':'0.001','max_units':'2','timezone':'UTC',
                'continuous':True,'network_secret':'UNEXPECTED'}]}))
            with self.assertRaises(ValueError):MarketRegistry.from_file(p)


class QuoteFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'feed.db';self.feed=DurableQuoteFeed(self.path)
        self.addCleanup(self.feed.close)

    def test_first_seq_and_roundtrip(self):
        self.assertEqual(self.feed.publish(100,mkquote())['status'],'QUOTE_STORED')
        self.assertEqual(self.feed.latest('BTC-USD'),mkquote())
        self.assertEqual(self.feed.verify()['events'],1)

    def test_duplicate_same_sequence_idempotent(self):
        self.feed.publish(2,mkquote());self.assertEqual(self.feed.publish(2,mkquote())['status'],'DUPLICATE_SAME_QUOTE')
        self.assertEqual(self.feed.verify()['events'],1)

    def test_sequence_gap_persistently_quarantines(self):
        self.feed.publish(1,mkquote())
        with self.assertRaises(SequenceGap):self.feed.publish(3,mkquote(T+timedelta(seconds=1)))
        with self.assertRaisesRegex(RiskRejected,'quarantined'):self.feed.latest('BTC-USD')
        with self.assertRaises(SequenceGap):self.feed.publish(2,mkquote(T+timedelta(seconds=1)))
        self.assertEqual(self.feed.status()['quotes'][0]['reason'],'SEQUENCE_GAP')

    def test_sequence_conflict_quarantines(self):
        self.feed.publish(1,mkquote())
        with self.assertRaises(SequenceGap):self.feed.publish(1,mkquote(bid='99.98'))
        self.assertTrue(self.feed.status()['quotes'][0]['quarantined'])

    def test_clock_rollback_quarantines(self):
        self.feed.publish(1,mkquote())
        with self.assertRaises(SequenceGap):self.feed.publish(2,mkquote(T-timedelta(seconds=1)))
        self.assertEqual(self.feed.status()['quotes'][0]['reason'],'CLOCK_ROLLBACK')

    def test_hash_event_tamper_detected(self):
        self.feed.publish(1,mkquote())
        self.feed.db.execute('UPDATE quote_events SET payload=? WHERE event_id=1',('{}',))
        with self.assertRaises(IntegrityFailure):self.feed.verify()

    def test_latest_tamper_detected(self):
        self.feed.publish(1,mkquote())
        self.feed.db.execute('UPDATE quote_latest SET payload=? WHERE symbol=?',('{}','BTC-USD'))
        with self.assertRaises(IntegrityFailure):self.feed.latest('BTC-USD')

    def test_restart_retains_feed_sequence(self):
        self.feed.publish(1,mkquote())
        other=DurableQuoteFeed(self.path)
        try:
            other.publish(2,mkquote(T+timedelta(seconds=1)))
            self.assertEqual(other.latest('BTC-USD').source_time,T+timedelta(seconds=1))
        finally:other.close()

    def test_multiple_market_sequences_independent(self):
        self.feed.publish(77,mkquote())
        self.feed.publish(1,mkquote(symbol='ETH-USD'))
        self.assertEqual(len(self.feed.status()['quotes']),2)

    def test_quarantine_bit_tampering_is_detected(self):
        self.feed.publish(1,mkquote())
        with self.assertRaises(SequenceGap):self.feed.publish(3,mkquote(T+timedelta(seconds=1)))
        self.feed.db.execute('UPDATE quote_latest SET quarantined=0,reason=""')
        with self.assertRaisesRegex(IntegrityFailure,'quarantine state'):
            self.feed.latest('BTC-USD')

    def test_quarantine_incident_log_tampering_is_detected(self):
        self.feed.publish(1,mkquote())
        with self.assertRaises(SequenceGap):self.feed.publish(3,mkquote(T+timedelta(seconds=1)))
        self.feed.db.execute('UPDATE quote_incidents SET reason="FALSE"')
        with self.assertRaisesRegex(IntegrityFailure,'incident log'):
            self.feed.verify()

    def test_trace_replay_and_disallow_unexpected_keys(self):
        trace=Path(self.tmp.name)/'trace.jsonl'
        trace.write_text(json.dumps({'sequence':1,'quote':quote_payload(mkquote())})+'\n')
        self.assertEqual(replay_jsonl(trace,self.feed)['observed_trace_records'],1)
        trace.write_text(json.dumps({'sequence':2,'quote':quote_payload(mkquote()),'BROKER_ORDER':'BUY'})+'\n')
        with self.assertRaises(ValueError):replay_jsonl(trace,self.feed)


class IsolatedAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.d=Path(self.tmp.name);self.ledger=self.d/'ledger.db';self.feed=self.d/'feed.db'
        self.authority=IsolatedRiskAuthority(self.ledger,self.feed,sim_registry())
        self.addCleanup(self.authority.close)
        self.authority.feed.publish(1,mkquote())

    def test_real_authorize_writes_ledger_and_no_broker_order(self):
        result=self.authority.authorize(mkintent(),now=T+timedelta(milliseconds=250))
        self.assertEqual(result['state'],'CREATED')
        self.assertFalse(result['live_approved'])
        self.assertEqual(len(self.authority.store.orders()),1)
        self.assertEqual(self.authority.store.account()['cash'],'10000')

    def test_clock_lag_rejected(self):
        with self.assertRaises(RiskRejected):
            self.authority.authorize(mkintent(),now=T+timedelta(minutes=1))

    def test_rejected_no_future_source(self):
        with self.assertRaises(RiskRejected):self.authority.authorize(mkintent(at=T+timedelta(minutes=2)),now=T)

    def test_unapproved_instrument_rejected(self):
        with self.assertRaises(RiskRejected):self.authority.authorize(mkintent(symbol='ETH-USD'),now=T)

    def test_risk_limits_still_enforced(self):
        with self.assertRaisesRegex(RiskRejected,'notional cap'):
            self.authority.authorize(mkintent(quantity='2',limit='1000'),now=T)

    def test_duplicate_intent_only_one_order(self):
        self.authority.authorize(mkintent(),now=T)
        self.authority.authorize(mkintent(),now=T+timedelta(milliseconds=250))
        self.assertEqual(len(self.authority.store.orders()),1)

    def test_halt_blocks_new_risk(self):
        self.authority.halt()
        with self.assertRaises(RiskRejected):self.authority.authorize(mkintent(),now=T)
        self.assertEqual(self.authority.health()['status'],'HALTED')

    def test_no_rpc_quote_or_time_spoofing(self):
        packet={'op':'AUTHORIZE','intent':intent_payload(mkintent()),'now':'2020-01-01'}
        with self.assertRaises(ValueError):self.authority.handle(packet)
        packet={'op':'AUTHORIZE','intent':dict(intent_payload(mkintent()),quote={'ask':'0.01'})}
        with self.assertRaises(ValueError):self.authority.handle(packet)

    def test_registry_pin_on_restart(self):
        self.authority.close()
        a=IsolatedRiskAuthority(self.ledger,self.feed,sim_registry())
        a.close()
        different=MarketRegistry(tuple(sim_registry().markets.values()),version='different')
        with self.assertRaisesRegex(IntegrityFailure,'Registry/calendar changed'):
            IsolatedRiskAuthority(self.ledger,self.feed,different)
        # Avoid cleanup double-close exception by re-open to a valid instance.
        self.authority=IsolatedRiskAuthority(self.ledger,self.feed,sim_registry())

    def test_feed_corruption_blocks_new_order(self):
        self.authority.feed.db.execute('UPDATE quote_events SET hash=? WHERE event_id=1',('dead',))
        with self.assertRaises(IntegrityFailure):self.authority.authorize(mkintent(),now=T)
        self.assertFalse(self.authority.store.orders())

    def _start_real_risk_subprocess(self):
        # Unlike a helper thread, this exercises a real separate Python PID,
        # independent SQLite connections, and a Unix-domain RPC boundary.
        path=self.d/'risk.sock'
        reg=self.d/'registry.json'
        from alpha_oto.production.market_registry import MarketRegistry
        reg.write_text(json.dumps({'version':'v0.8-test','markets':[asdict(x) for x in sim_registry().markets.values()]}))
        proc=subprocess.Popen([sys.executable,'-m','alpha_oto.production.riskd','serve',
            '--ledger',str(self.ledger),'--feed',str(self.feed),
            '--registry',str(reg),'--socket',str(path)],
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def cleanup():
            proc.terminate()
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
            if proc.stderr:proc.stderr.close()
        self.addCleanup(cleanup)
        client=RiskClient(path)
        for _ in range(150):
            if proc.poll() is not None:
                err=proc.stderr.read().decode()
                self.fail('Risk subprocess unexpectedly died: '+err)
            if path.exists():
                try:client.health();break
                except RiskUnavailable:pass
            systemtime.sleep(.02)
        else:self.fail('Risk service did not launch')
        return client,path

    def test_os_socket_and_client_halt(self):
        client,path=self._start_real_risk_subprocess()
        self.assertEqual(os.stat(path).st_mode & 0o777,0o600)
        self.assertEqual(client.health()['status'],'STALE_FEED')
        client.halt()
        self.assertEqual(client.health()['status'],'HALTED')

    def test_socket_service_authorizes_using_current_source(self):
        now=datetime.now(UTC)
        self.authority.feed.publish(2,mkquote(now))
        client,path=self._start_real_risk_subprocess()
        reply=client.authorize(mkintent(now))
        self.assertEqual(reply['state'],'CREATED')
        self.assertFalse(reply['live_approved'])
        self.assertEqual(len(self.authority.store.orders()),1)

    def test_separated_gateway_uses_authorized_intent_only(self):
        broker=SimulatedBroker()
        gateway=SimulationGateway(self.authority.store,broker)
        with self.assertRaises(IntegrityFailure):gateway.dispatch(mkintent())
        now=datetime.now(UTC)
        self.authority.feed.publish(2,mkquote(now))
        client,_=self._start_real_risk_subprocess()
        valid=mkintent(now)
        separate=SeparatedSimulationGateway(client,gateway)
        self.assertEqual(separate.propose(valid)['state'],'CREATED')
        self.assertEqual(separate.dispatch(valid)['state'],'ACKNOWLEDGED')
        self.assertEqual(broker.calls,1)

    def test_unavailable_rpc_must_not_create_order(self):
        with self.assertRaises(RiskUnavailable):RiskClient(self.d/'nonexistent.sock').authorize(mkintent())
        self.assertFalse(self.authority.store.orders())

    def test_reject_failed_market_session_without_order(self):
        # Use separate isolated account with explicit session + valid calendar.
        other=self.d/'weekday.db'
        registry=MarketRegistry([MarketSpec('BTC-USD','SIMULATED','USD','0.01','0.001','2',
            'UTC',False,(0,1,2,3,4),'09:00','17:00',(),'2026-10-31')],version='weekday')
        authority=IsolatedRiskAuthority(other,self.feed,registry)
        try:
            with self.assertRaisesRegex(RiskRejected,'closed'):
                authority.authorize(mkintent(),now=T)
            self.assertFalse(authority.store.orders())
        finally:authority.close()


if __name__=='__main__':unittest.main()
