"""Simulated production reliability acceptance tests; offline, no brokerage."""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import sqlite3
import tempfile
import unittest
from pathlib import Path

from alpha_oto.production import (D, Instrument, Quote, OrderIntent, RiskLimits,
    RiskRejected, IntegrityFailure, UnknownSubmission, SimulatedBroker, LedgerStore, SimulationGateway)


def instrument(symbol='BTC-USD',authorized=True):
    return Instrument(symbol,'SIMULATED','USD',D('.01'),D('.001'),D('2'),authorized)


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'ledger.sqlite3'
        self.now=datetime(2026,10,10,tzinfo=timezone.utc)
        self.inst=instrument()
        self.limits=RiskLimits()
        self.store=LedgerStore(self.path,instruments=(self.inst,),limits=self.limits)
        self.addCleanup(self.store.close)
        self.broker=SimulatedBroker()
        self.gateway=SimulationGateway(self.store,self.broker)

    def q(self,**changes):
        x=dict(symbol='BTC-USD',bid=D('99.99'),ask=D('100.00'),bid_size=D('10'),
               ask_size=D('10'),source_time=self.now,received_time=self.now)
        x.update(changes)
        return Quote(**x)

    def intent(self,ident='trade-1',side='BUY',qty='0.5',limit='100',**changes):
        x=dict(client_id=ident,symbol='BTC-USD',side=side,quantity=D(qty),limit=D(limit),
               created_at=self.now,strategy='baseline',model_version='frozen-001')
        x.update(changes)
        return OrderIntent(**x)

    def enter(self,intent=None,quote=None):
        i=intent or self.intent();q=quote or self.q()
        self.gateway.propose(i,q,self.now)
        self.gateway.dispatch(i)
        return i

    def buy_full(self):
        i=self.enter()
        self.broker.fill(i.client_id,i.quantity,i.limit,fill_id='fill-buy')
        self.gateway.reconcile(now=self.now)
        return i

    def test_smoke_partial_fill_then_full_reconciliation(self):
        i=self.enter()
        self.broker.fill(i.client_id,D('.2'),D('100'),fill_id='first')
        a=self.gateway.reconcile(now=self.now)
        self.assertEqual(self.store.order(i.client_id)['state'],'PARTIAL')
        self.assertEqual(a['cash'],'9979.98')
        self.broker.fill(i.client_id,D('.3'),D('100'),fill_id='second')
        a=self.gateway.reconcile(now=self.now)
        self.assertEqual(self.store.order(i.client_id)['state'],'FILLED')
        self.assertEqual(self.store.positions()['BTC-USD']['quantity'],D('.5'))
        self.assertEqual(self.store.positions()['BTC-USD']['basis'],D('50'))
        self.assertEqual(D(a['cash']),D('9949.95'))
        self.assertEqual(self.store.verify()['fills'],2)

    def test_reconcile_is_idempotent(self):
        self.buy_full()
        old=self.store.verify()
        self.gateway.reconcile(now=self.now)
        self.assertEqual(self.store.verify(),old)

    def test_accepted_but_timeout_does_not_resubmit(self):
        i=self.intent();self.gateway.propose(i,self.q(),self.now)
        self.broker.accept_then_timeout=True
        with self.assertRaises(UnknownSubmission):self.gateway.dispatch(i)
        self.assertEqual(self.store.order(i.client_id)['state'],'UNKNOWN')
        self.assertEqual(self.broker.calls,1)
        self.gateway.reconcile(now=self.now)
        self.assertEqual(self.store.order(i.client_id)['state'],'ACKNOWLEDGED')
        self.gateway.dispatch(i)
        self.assertEqual(self.broker.calls,1)

    def test_crash_after_submit_before_confirmation_does_not_retry(self):
        i=self.intent();self.gateway.propose(i,self.q(),self.now)
        self.store.transition(i.client_id,'SUBMITTING')
        with self.assertRaises(UnknownSubmission):self.gateway.dispatch(i)
        self.assertEqual(self.broker.calls,0)
        self.assertEqual(self.store.order(i.client_id)['state'],'UNKNOWN')

    def test_reject_is_terminal(self):
        i=self.intent();self.gateway.propose(i,self.q(),self.now)
        self.broker.reject_next=True
        self.gateway.dispatch(i)
        self.assertEqual(self.store.order(i.client_id)['state'],'REJECTED')
        self.assertEqual(self.store.account()['cash'],'10000')
        with self.assertRaises(IntegrityFailure):self.store.transition(i.client_id,'ACKNOWLEDGED')

    def test_cancel_partial_and_reconcile(self):
        i=self.enter();self.broker.fill(i.client_id,D('.2'),D('100'),fill_id='one')
        self.gateway.reconcile(now=self.now)
        self.gateway.cancel(i.client_id)
        self.assertEqual(self.store.order(i.client_id)['state'],'CANCELLED')
        self.assertEqual(self.store.positions()['BTC-USD']['quantity'],D('.2'))

    def test_selling_reduces_position_and_realizes_gross_pnl(self):
        self.buy_full()
        q=self.q(bid=D('109.99'),ask=D('110'),source_time=self.now+timedelta(seconds=1),received_time=self.now+timedelta(seconds=1))
        now=self.now+timedelta(seconds=1)
        i=self.intent('sell-2','SELL','.2','109.99',created_at=now)
        self.gateway.propose(i,q,now)
        self.gateway.dispatch(i)
        self.broker.fill(i.client_id,D('.2'),D('109.99'),fill_id='sell')
        self.gateway.reconcile(now=now)
        self.assertEqual(self.store.positions()['BTC-USD']['quantity'],D('.3'))
        self.assertEqual(self.store.positions()['BTC-USD']['basis'],D('30'))
        self.assertEqual(D(self.store.account()['realized']),D('1.998'))
        self.assertTrue(self.store.verify()['accounting_verified'])

    def test_sell_without_holdings_rejected(self):
        with self.assertRaisesRegex(RiskRejected,'Cannot sell'):
            self.gateway.propose(self.intent('sell','SELL','.1','99.99'),self.q(),self.now)

    def test_two_sells_cannot_reserve_same_units(self):
        self.buy_full()
        first=self.intent('sell1','SELL','.4','99.99')
        self.gateway.propose(first,self.q(),self.now)
        with self.assertRaisesRegex(RiskRejected,'inventory'):
            self.gateway.propose(self.intent('sell2','SELL','.2','99.99'),self.q(),self.now)

    def test_duplicate_id_exact_idempotent_conflicting_payload_rejected(self):
        i=self.intent();o=self.gateway.propose(i,self.q(),self.now)
        self.assertEqual(o,self.gateway.propose(i,self.q(),self.now))
        with self.assertRaisesRegex(IntegrityFailure,'Client id reused'):
            self.gateway.propose(self.intent(qty='.6'),self.q(),self.now)
        self.assertEqual(len(self.store.orders()),1)

    def test_no_direct_remote_dispatch_without_approved_intent(self):
        with self.assertRaisesRegex(IntegrityFailure,'risk governor'):
            self.gateway.dispatch(self.intent())
        self.assertEqual(self.broker.calls,0)

    def test_quote_from_future_rejected(self):
        with self.assertRaises(RiskRejected):
            self.gateway.propose(self.intent(),self.q(source_time=self.now+timedelta(seconds=1),received_time=self.now+timedelta(seconds=1)),self.now)

    def test_quote_stale(self):
        with self.assertRaises(RiskRejected):
            self.gateway.propose(self.intent(),self.q(source_time=self.now-timedelta(seconds=10),received_time=self.now-timedelta(seconds=10)),self.now)

    def test_quote_delayed(self):
        with self.assertRaises(RiskRejected):
            self.gateway.propose(self.intent(),self.q(source_time=self.now-timedelta(seconds=6),received_time=self.now),self.now)

    def test_spread_too_wide(self):
        with self.assertRaisesRegex(RiskRejected,'spread'):
            self.gateway.propose(self.intent(),self.q(bid=D('98')),self.now)

    def test_quote_wrong_asset(self):
        with self.assertRaises(RiskRejected):
            self.gateway.propose(self.intent(),self.q(symbol='ETH-USD'),self.now)

    def test_quote_insufficient_depth(self):
        with self.assertRaises(RiskRejected):
            self.gateway.propose(self.intent(),self.q(ask_size=D('.1')),self.now)

    def test_limit_not_market_executable(self):
        with self.assertRaises(RiskRejected):
            self.gateway.propose(self.intent(limit='99'),self.q(),self.now)

    def test_lot_violation(self):
        with self.assertRaises(RiskRejected):
            self.gateway.propose(self.intent(qty='.0005'),self.q(),self.now)

    def test_tick_violation(self):
        with self.assertRaises(RiskRejected):
            self.gateway.propose(self.intent(limit='100.001'),self.q(),self.now)

    def test_insufficient_cash_including_pending_reservations(self):
        risk=RiskLimits(max_order_notional=D('10000'),max_symbol_notional=D('10000'),max_gross_notional=D('10000'),cash_reserve=D('9500'))
        self.store.close();self.store=LedgerStore(Path(self.temp.name)/"limited.sqlite3",instruments=(self.inst,),limits=risk)
        self.gateway=SimulationGateway(self.store,self.broker)
        self.addCleanup(self.store.close)
        self.gateway.propose(self.intent('a',qty='2'),self.q(),self.now)
        self.gateway.propose(self.intent('b',qty='2'),self.q(),self.now)
        with self.assertRaisesRegex(RiskRejected,'cash'):
            self.gateway.propose(self.intent('c',qty='2'),self.q(),self.now)

    def test_order_cap(self):
        risk=RiskLimits(max_order_notional=D('15'))
        self.store.close();self.store=LedgerStore(Path(self.temp.name)/"limited.sqlite3",instruments=(self.inst,),limits=risk)
        self.gateway=SimulationGateway(self.store,self.broker)
        self.addCleanup(self.store.close)
        with self.assertRaisesRegex(RiskRejected,'notional'):
            self.gateway.propose(self.intent(),self.q(),self.now)

    def test_manual_emergency_halt(self):
        self.store.set_halt('manual red switch')
        with self.assertRaisesRegex(RiskRejected,'halt'):
            self.gateway.propose(self.intent(),self.q(),self.now)
        self.assertTrue(self.store.summary()['halted'])

    def test_unknown_broker_order_fails_reconciliation(self):
        i=self.intent()
        self.broker.submit(i)
        with self.assertRaisesRegex(IntegrityFailure,'Unrecognized'):
            self.gateway.reconcile(now=self.now)
        self.assertTrue(self.store.summary()['halted'])

    def test_broker_cash_divergence_triggers_halt(self):
        self.broker.cash-=D('20')
        with self.assertRaisesRegex(IntegrityFailure,'Cash mismatch'):
            self.gateway.reconcile(now=self.now)
        self.assertTrue(self.store.summary()['halted'])

    def test_unsolicited_remote_position_triggers_halt(self):
        self.broker.positions['BTC-USD']=D('1')
        with self.assertRaisesRegex(IntegrityFailure,'position mismatch'):
            self.gateway.reconcile(now=self.now)
        self.assertTrue(self.store.summary()['halted'])

    def test_mutated_financial_cash_detected(self):
        self.store.db.execute('UPDATE accounts SET cash="9000"')
        with self.assertRaises(IntegrityFailure):self.store.verify()

    def test_mutated_journal_detected(self):
        self.store.db.execute('UPDATE journal SET payload="{}" WHERE seq=1')
        with self.assertRaisesRegex(IntegrityFailure,'Journal'):
            self.store.verify()

    def test_mutated_positions_detected(self):
        self.buy_full()
        self.store.db.execute('UPDATE positions SET quantity="100" WHERE symbol="BTC-USD"')
        with self.assertRaises(IntegrityFailure):self.store.verify()

    def test_client_id_collision_fill_payload_detected(self):
        self.buy_full()
        from alpha_oto.production.broker import RemoteFill
        with self.assertRaises(IntegrityFailure):
            self.store.record_fill(RemoteFill('fill-buy','trade-1',D('.2'),D('100'),D('.02')),self.now)

    def test_sqlite_reopen_persists_reconciled_cash(self):
        self.buy_full()
        self.store.close()
        restored=LedgerStore(self.path,instruments=(self.inst,))
        self.store=restored
        self.addCleanup(restored.close)
        self.gateway=SimulationGateway(restored,self.broker)
        self.assertEqual(restored.positions()['BTC-USD']['quantity'],D('.5'))
        self.assertTrue(restored.verify()['accounting_verified'])

    def test_cross_currency_instrument_fails(self):
        with self.assertRaises(ValueError):
            LedgerStore(Path(self.temp.name)/'other.sqlite3',instruments=(Instrument('X','SIM','INR',D('1'),D('1'),D('10')),))

    def test_no_live_endpoint_or_broker_credentials(self):
        from alpha_oto.production import __all__
        self.assertNotIn('LiveBroker',__all__)
        self.assertNotIn('LiveOrder',__all__)

    def test_sell_fills_dont_hallucinate_profit(self):
        self.buy_full()
        q=self.q();i=self.intent('sell','SELL','.5','99.99')
        self.gateway.propose(i,q,self.now);self.gateway.dispatch(i)
        self.broker.fill(i.client_id,D('.5'),D('99.99'),fill_id='sell-fill')
        self.gateway.reconcile(now=self.now)
        self.assertEqual(self.store.positions()['BTC-USD']['quantity'],D(0))
        self.assertEqual(self.store.positions()['BTC-USD']['basis'],D(0))
        self.assertEqual(D(self.store.account()['realized']),D('-.005'))
        self.assertEqual(self.store.verify()['fills'],2)

    def test_unknown_blocks_new_orders_and_requires_resolution(self):
        i=self.intent();self.gateway.propose(i,self.q(),self.now)
        self.store.transition(i.client_id,'UNKNOWN')
        with self.assertRaisesRegex(RiskRejected,'quarantined'):
            self.gateway.propose(self.intent('second'),self.q(),self.now)
        with self.assertRaisesRegex(IntegrityFailure,'indeterminate'):
            self.gateway.reconcile(now=self.now)
        self.assertTrue(self.store.summary()['halted'])

    def test_risk_configuration_immutable_after_restart(self):
        with self.assertRaisesRegex(IntegrityFailure,'Risk configuration changed'):
            LedgerStore(self.path,instruments=(self.inst,),limits=RiskLimits(max_order_notional=D('2000')))

    def test_expired_intention_rejected(self):
        old=self.now-timedelta(seconds=20)
        with self.assertRaisesRegex(RiskRejected,'Expired'):
            self.gateway.propose(self.intent(created_at=old),self.q(),self.now)

    def test_future_received_quote_rejected(self):
        with self.assertRaisesRegex(RiskRejected,'not yet been received'):
            self.gateway.propose(self.intent(),self.q(received_time=self.now+timedelta(seconds=1)),self.now)

    def test_broker_duplicate_fill_rejected_before_mutating_balances(self):
        i=self.enter()
        self.broker.fill(i.client_id,D('.2'),D('100'),fill_id='unique')
        previous=self.broker.snapshot()
        with self.assertRaisesRegex(RuntimeError,'Duplicate fill'):
            self.broker.fill(i.client_id,D('.2'),D('100'),fill_id='unique')
        self.assertEqual(self.broker.cash,previous['cash'])
        self.assertEqual(self.broker.positions,previous['positions'])

    def test_health_reports_unresolved_and_detects_stale_position_mark(self):
        from alpha_oto.production.health import inspect
        self.assertEqual(inspect(self.store,now=self.now)['status'],'GREEN')
        self.buy_full()
        result=inspect(self.store,now=self.now+timedelta(seconds=20))
        self.assertIn('STALE_POSITION_MARK: BTC-USD',result['critical'])
        self.assertFalse(result['live_approved'])

    def test_automatic_drawdown_halt(self):
        self.buy_full()
        # 0.5 BTC at 100 on a 10,000 cash account cannot draw down 8%.
        # Reopen a distinct 1,000 USD account with 2 units to exercise it.
        other=Path(self.temp.name)/'drawdown.sqlite3'
        risk=RiskLimits(max_order_notional=D('1000'),max_gross_notional=D('1000'),
                        max_symbol_notional=D('1000'),cash_reserve=D('1'),max_peak_drawdown_fraction=D('.01'))
        store=LedgerStore(other,initial_cash=D('1000'),instruments=(self.inst,),limits=risk)
        self.addCleanup(store.close)
        broker=SimulatedBroker(cash=D('1000'))
        gate=SimulationGateway(store,broker)
        q=self.q();i=self.intent('heavy',qty='2')
        gate.propose(i,q,self.now);gate.dispatch(i)
        broker.fill(i.client_id,D('2'),D('100'),fill_id='heavy-fill')
        gate.reconcile(now=self.now)
        down_time=self.now+timedelta(seconds=1)
        down=Quote('BTC-USD',D('10'),D('10.01'),D('10'),D('10'),down_time,down_time)
        store.record_mark(down,down_time)
        self.assertTrue(store.summary()['halted'])

    def test_daily_net_loss_reset_uses_date_of_import(self):
        risk=RiskLimits(max_daily_realized_loss=D('.03'))
        path=Path(self.temp.name)/'daily.sqlite3'
        store=LedgerStore(path,instruments=(self.inst,),limits=risk)
        self.addCleanup(store.close)
        broker=SimulatedBroker();gate=SimulationGateway(store,broker)
        i=self.intent('daybuy')
        gate.propose(i,self.q(),self.now);gate.dispatch(i)
        broker.fill(i.client_id,D('.5'),D('100'),fill_id='db')
        gate.reconcile(now=self.now)
        j=self.intent('daysell','SELL','.5','99.99')
        gate.propose(j,self.q(),self.now);gate.dispatch(j)
        broker.fill(j.client_id,D('.5'),D('99.99'),fill_id='ds')
        gate.reconcile(now=self.now)
        with self.assertRaisesRegex(RiskRejected,'Daily realized'):
            gate.propose(self.intent('newbuy'),self.q(),self.now)
        tomorrow=self.now+timedelta(days=1)
        quote=Quote('BTC-USD',D('99.99'),D('100'),D('10'),D('10'),tomorrow,tomorrow)
        newer=self.intent('nextday',created_at=tomorrow)
        gate.propose(newer,quote,tomorrow)
        self.assertEqual(store.order('nextday')['state'],'CREATED')


    def test_multi_asset_unknown_pending_price_fails_closed(self):
        path=Path(self.temp.name)/'multi.sqlite3'
        eth=Instrument('ETH-USD','SIMULATED','USD',D('.01'),D('.001'),D('2'))
        store=LedgerStore(path,instruments=(self.inst,eth))
        self.addCleanup(store.close)
        broker=SimulatedBroker();gate=SimulationGateway(store,broker)
        now=self.now
        eq=Quote('ETH-USD',D('99.99'),D('100'),D('1'),D('1'),now,now)
        ei=OrderIntent('eth1','ETH-USD','BUY',D('.5'),D('100'),now,'test','fixed')
        gate.propose(ei,eq,now)
        later=now+timedelta(seconds=6)
        bq=Quote('BTC-USD',D('99.99'),D('100'),D('1'),D('1'),later,later)
        bi=self.intent('btc1',created_at=later)
        with self.assertRaisesRegex(RiskRejected,'Stale valuation'):
            gate.propose(bi,bq,later)
        self.assertEqual(len(store.orders()),1)

    def test_explicit_unapproved_instrument(self):
        path=Path(self.temp.name)/'not-authorized.sqlite3'
        store=LedgerStore(path,instruments=(instrument(authorized=False),))
        self.addCleanup(store.close)
        gate=SimulationGateway(store,SimulatedBroker())
        with self.assertRaisesRegex(RiskRejected,'not explicitly authorized'):
            gate.propose(self.intent(),self.q(),self.now)

    def test_unbalanced_postings_rejected(self):
        with self.assertRaises(IntegrityFailure):
            with self.store.transaction():
                self.store._postings('bad',[('A',D('1')),('B',D('-2'))])
        self.assertEqual(self.store.verify()['postings'],2)


if __name__=='__main__':unittest.main()
