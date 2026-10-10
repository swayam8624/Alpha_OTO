"""Fail-closed broker emulator coordination. NO live connector exists.

Remote orders can be accepted before a client receives a response. We persist
SUBMITTING first, and refuse blind retries after any indeterminate submission.
Broker client IDs remain stable and all fills are imported exactly once.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from .contracts import D, IntegrityFailure, OrderIntent, Quote, UnknownSubmission, utc
from .store import LedgerStore, OPEN_STATES, TERMINAL_STATES
from .broker import AcceptedButTimedOut, SimulatedBroker


class SimulationGateway:
    def __init__(self,store:LedgerStore,broker:SimulatedBroker):
        if broker.currency!=store.currency:
            raise ValueError('Broker and ledger currency mismatch')
        self.store=store
        self.broker=broker

    def propose(self,intent:OrderIntent,quote:Quote,now:datetime):
        self.store.record_mark(quote,now)
        return self.store.create_intent(intent,quote,now)

    def dispatch(self,intent:OrderIntent):
        """Idempotent within a running broker simulator; restart-safe unknown state.

        Even a zero-response network failure never authorizes a second submit.
        Reconciliation is required to resolve broker ambiguity.
        """
        local=self.store.order(intent.client_id)
        if local is None:raise IntegrityFailure('Order must pass risk governor first')
        if local['state'] in TERMINAL_STATES:
            return local
        if local['state'] in ('ACKNOWLEDGED','PARTIAL'):
            return local
        try:
            remote=self.broker.lookup(intent.client_id)
        except Exception as exc:
            self.store.transition(intent.client_id,'UNKNOWN')
            raise UnknownSubmission('Broker lookup unavailable; order quarantined') from exc
        if remote is not None:
            self.reconcile()
            return self.store.order(intent.client_id)
        if local['state'] != 'CREATED':
            # It might have reached a different broker or still be in flight.
            # A missing lookup alone does not prove the prior submission failed.
            self.store.transition(intent.client_id,'UNKNOWN')
            raise UnknownSubmission('Indeterminate previous submission; no blind retry')
        self.store.transition(intent.client_id,'SUBMITTING')
        try:
            remote=self.broker.submit(intent)
        except AcceptedButTimedOut as exc:
            self.store.transition(intent.client_id,'UNKNOWN')
            raise UnknownSubmission('Broker may have accepted the order') from exc
        except Exception as exc:
            self.store.transition(intent.client_id,'UNKNOWN')
            raise UnknownSubmission('Unexpected broker failure; reconcile before anything else') from exc
        self._sync_order(remote)
        return self.store.order(intent.client_id)

    def _sync_order(self,remote):
        ident=remote['intent'].client_id
        local=self.store.order(ident)
        if local is None:
            raise IntegrityFailure('Unrecognized broker order; immediate operator investigation required')
        expected=(local['symbol'],local['side'],D(local['quantity']),D(local['limit_px']))
        actual=(remote['intent'].symbol,remote['intent'].side,remote['intent'].quantity,remote['intent'].limit)
        if expected!=actual:
            self.store.set_halt('Remote order mismatch')
            raise IntegrityFailure('Remote order disagrees with journal')
        self.store.bind_broker_id(ident,remote['remote_id'])
        state=remote['state']
        if state=='FILLED':
            # Fill accounting happens from broker-confirmed fill IDs first.
            if D(self.store.order(ident)['filled'])!=remote['filled']:
                raise IntegrityFailure('Broker reports filled order without corresponding fills')
        if state in ('ACKNOWLEDGED','REJECTED','CANCELLED'):
            if local['state']!=state and local['state'] not in TERMINAL_STATES:
                self.store.transition(ident,state,remote['remote_id'])
        elif state=='PARTIAL':
            if D(self.store.order(ident)['filled'])!=remote['filled']:
                raise IntegrityFailure('Broker partial quantities not reconciled')
        elif state!='FILLED':
            raise IntegrityFailure('Unrecognized broker state')

    def reconcile(self,*,now:datetime|None=None):
        now=utc(now or datetime.now(timezone.utc))
        try:
            remote=self.broker.snapshot()
        except Exception as exc:
            self.store.set_halt('Broker snapshot unavailable')
            raise UnknownSubmission('Cannot reconcile: broker unavailable') from exc
        if remote['currency']!=self.store.currency:
            self.store.set_halt('Wrong broker account currency')
            raise IntegrityFailure('Currency mismatch')
        try:
            # Prefer fill-ID based financial updates. Import after verifying all
            # recognized order IDs; any unsolicited order is a critical anomaly.
            for cid in remote['orders']:
                if self.store.order(cid) is None:
                    raise IntegrityFailure('Unrecognized broker order')
            for fill in remote['fills']:
                self.store.record_fill(fill,now)
            for cid,order in remote['orders'].items():
                self._sync_order(order)
            for local in self.store.orders():
                if local['state'] in ('SUBMITTING','UNKNOWN') and local['client_id'] not in remote['orders']:
                    raise IntegrityFailure('Locally indeterminate order absent from broker snapshot; manual resolution required')
            local_cash=D(self.store.account()['cash'])
            if local_cash!=remote['cash']:
                raise IntegrityFailure(f'Cash mismatch: local={local_cash} broker={remote["cash"]}')
            for symbol,pos in self.store.positions().items():
                if pos['quantity']!=remote['positions'].get(symbol,Decimal(0)):
                    raise IntegrityFailure('Broker/local position mismatch for '+symbol)
            if set(remote['positions'])-set(self.store.instruments):
                raise IntegrityFailure('Unknown broker-held asset')
            evidence=self.store.verify()
            return {'status':'BROKER_EMULATOR_RECONCILED','mode':'SIMULATOR_ONLY',
                    'remote_orders':len(remote['orders']),'remote_fills':len(remote['fills']),
                    'cash':str(local_cash),'evidence':evidence}
        except (IntegrityFailure,ValueError,KeyError,RuntimeError) as exc:
            self.store.set_halt('Reconciliation divergence: '+str(exc))
            raise

    def cancel(self,client_id:str):
        order=self.store.order(client_id)
        if order is None:raise IntegrityFailure('Unknown local order')
        if order['state'] in TERMINAL_STATES:return order
        # Broker query-before-cancel; no local assumption about a remote state.
        remote=self.broker.lookup(client_id)
        if remote is None:
            self.store.transition(client_id,'UNKNOWN')
            raise UnknownSubmission('Cannot cancel order with unknown broker state')
        self.broker.cancel(client_id)
        self.reconcile()
        return self.store.order(client_id)
