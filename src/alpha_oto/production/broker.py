"""Deterministic broker emulator with durable-ID behavior and chaos controls.

Not a broker integration. Explicitly mimics accepted-but-timeout, partial fills,
rejections, duplicate callbacks, cancellations, and broker position truth.
"""
from __future__ import annotations
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from .contracts import D, OrderIntent, UnknownSubmission


class AcceptedButTimedOut(TimeoutError):
    pass


@dataclass(frozen=True)
class RemoteFill:
    id: str
    client_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal


class SimulatedBroker:
    """IN-MEMORY only; state is deliberately separate from SQLite local books."""
    def __init__(self, cash: Decimal = Decimal('10000'), currency: str='USD'):
        self.cash=D(cash)
        self.currency=currency
        self.positions:dict[str,Decimal]={}
        self.orders:dict[str,dict]={}
        self.fills:OrderedDict[str,RemoteFill]=OrderedDict()
        self.accept_then_timeout=False
        self.reject_next=False
        self.lookup_unavailable=False
        self.calls=0

    def lookup(self, client_id: str):
        if self.lookup_unavailable:
            raise ConnectionError('Broker lookup unavailable')
        return self.orders.get(client_id)

    def submit(self, intent: OrderIntent):
        self.calls+=1
        if intent.client_id in self.orders:
            existing=self.orders[intent.client_id]
            if existing['intent']!=intent:
                raise RuntimeError('Broker client id reused with different payload')
            return existing
        status='REJECTED' if self.reject_next else 'ACKNOWLEDGED'
        self.reject_next=False
        remote={'intent':intent,'state':status,'filled':Decimal(0),
                'remote_id':'SIM-'+intent.client_id}
        self.orders[intent.client_id]=remote
        if self.accept_then_timeout:
            self.accept_then_timeout=False
            raise AcceptedButTimedOut('Acceptance persisted remotely; response lost')
        return remote

    def fill(self, client_id: str, quantity, price, *, fee_bps=10, fill_id=None):
        remote=self.orders[client_id]
        if remote['state'] not in ('ACKNOWLEDGED','PARTIAL'):
            raise RuntimeError('Broker order cannot fill in current state')
        fid=fill_id or f'SIM-FILL-{len(self.fills)+1}'
        if fid in self.fills:
            raise RuntimeError('Duplicate fill identifier')
        intent=remote['intent']; qty=D(quantity); px=D(price)
        fee=qty*px*D(fee_bps)/Decimal(10000)
        if qty<=0 or px<=0 or D(fee_bps)<0 or qty % Decimal('0.00000001'):
            raise ValueError('Invalid broker fill')
        if remote['filled']+qty>intent.quantity:
            raise RuntimeError('Fill exceeds intended quantity')
        if (intent.side=='BUY' and px>intent.limit) or (intent.side=='SELL' and px<intent.limit):
            raise RuntimeError('Limit order violated')
        amount=qty*px
        if intent.side=='BUY':
            if self.cash<amount+fee:
                raise RuntimeError('Broker insufficient funds')
            self.cash-=amount+fee
            self.positions[intent.symbol]=self.positions.get(intent.symbol,Decimal(0))+qty
        else:
            if self.positions.get(intent.symbol,Decimal(0))<qty:
                raise RuntimeError('Broker cannot short')
            self.cash+=amount-fee
            self.positions[intent.symbol]-=qty
        result=RemoteFill(fid,client_id,qty,px,fee)
        self.fills[fid]=result
        remote['filled']+=qty
        remote['state']='FILLED' if remote['filled']==intent.quantity else 'PARTIAL'
        return result

    def cancel(self, client_id:str):
        remote=self.orders[client_id]
        if remote['state'] in ('ACKNOWLEDGED','PARTIAL'):
            remote['state']='CANCELLED'
        return remote

    def snapshot(self):
        return {'currency':self.currency,'cash':self.cash,
                'positions':dict(self.positions),'orders':dict(self.orders),
                'fills':list(self.fills.values())}
