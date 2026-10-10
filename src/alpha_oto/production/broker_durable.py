"""Durable OFFLINE broker emulator with a separate SQLite authority.

This component never connects to a real broker.  Each remote mutation is
committed atomically before the simulated broker returns its acknowledgement.
An accepted-but-lost response is consequently recoverable after BOTH Python
processes restart.  All financial values are Decimal, serialized as strings.

The remote database intentionally lives apart from the client order ledger to
reproduce the reconciliation boundary of two independent systems.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .broker import AcceptedButTimedOut, RemoteFill, SimulatedBroker
from .contracts import D, IntegrityFailure, OrderIntent
from .store import canonical


def _hash(value: str) -> str:
    return sha256(value.encode('utf-8')).hexdigest()


def _intent_json(intent: OrderIntent) -> dict:
    return {
        'client_id': intent.client_id, 'symbol': intent.symbol,
        'side': intent.side, 'quantity': str(intent.quantity),
        'limit': str(intent.limit), 'created_at': intent.created_at.isoformat(),
        'strategy': intent.strategy, 'model_version': intent.model_version,
    }


def _encode(broker: SimulatedBroker) -> dict:
    return {
        'currency': broker.currency, 'cash': str(broker.cash),
        'positions': {k: str(v) for k, v in sorted(broker.positions.items())},
        'orders': {cid: {
            'intent': _intent_json(order['intent']), 'state': order['state'],
            'filled': str(order['filled']), 'remote_id': order['remote_id'],
        } for cid, order in sorted(broker.orders.items())},
        'fills': [{
            'id': f.id, 'client_id': f.client_id,
            'quantity': str(f.quantity), 'price': str(f.price), 'fee': str(f.fee),
        } for f in broker.fills.values()],
        'calls': broker.calls,
    }


def _decode(data: dict) -> SimulatedBroker:
    b = SimulatedBroker(D(data['cash']), data['currency'])
    b.positions = {key: D(value) for key, value in data['positions'].items()}
    b.orders = {
        cid: {
            'intent': OrderIntent(**{
                **order['intent'],
                'quantity': D(order['intent']['quantity']),
                'limit': D(order['intent']['limit']),
                'created_at': datetime.fromisoformat(order['intent']['created_at']),
            }),
            'state': order['state'], 'filled': D(order['filled']),
            'remote_id': order['remote_id'],
        }
        for cid, order in data['orders'].items()
    }
    b.fills = OrderedDict(
        (f['id'], RemoteFill(f['id'], f['client_id'], D(f['quantity']),
                             D(f['price']), D(f['fee'])))
        for f in data['fills']
    )
    b.calls = int(data['calls'])
    return b


def _financial_invariants(b: SimulatedBroker, initial_cash: Decimal) -> None:
    """Independently recompute remote cash, positions and filled quantities."""
    from .store import TERMINAL_STATES
    cash = initial_cash
    units: dict[str, Decimal] = {}
    filled: dict[str, Decimal] = {cid: Decimal(0) for cid in b.orders}
    seen = set()
    if b.calls < 0 or b.cash < 0:
        raise IntegrityFailure('Invalid remote call count/cash')
    for cid, row in b.orders.items():
        intent = row['intent']
        if cid != intent.client_id or row['remote_id'] != 'SIM-' + cid:
            raise IntegrityFailure('Remote ID or order identity corrupted')
        if row['state'] not in {'ACKNOWLEDGED','REJECTED','PARTIAL','FILLED','CANCELLED'}:
            raise IntegrityFailure('Unknown persisted remote state')
        if row['filled'] < 0 or row['filled'] > intent.quantity:
            raise IntegrityFailure('Impossible persisted filled quantity')
    for f in b.fills.values():
        if f.id in seen or f.client_id not in b.orders:
            raise IntegrityFailure('Duplicate or orphan remote fill')
        seen.add(f.id)
        order = b.orders[f.client_id]['intent']
        if f.quantity <= 0 or f.price <= 0 or f.fee < 0:
            raise IntegrityFailure('Invalid persisted fill values')
        if (order.side == 'BUY' and f.price > order.limit) or (order.side == 'SELL' and f.price < order.limit):
            raise IntegrityFailure('Persisted fill violates limit')
        filled[f.client_id] += f.quantity
        amount = f.quantity * f.price
        if order.side == 'BUY':
            cash -= amount + f.fee
            units[order.symbol] = units.get(order.symbol, Decimal(0)) + f.quantity
        else:
            cash += amount - f.fee
            units[order.symbol] = units.get(order.symbol, Decimal(0)) - f.quantity
            if units[order.symbol] < 0:
                raise IntegrityFailure('Remote books contain a short position')
        if cash < 0:
            raise IntegrityFailure('Remote ledger went negative')
    if cash != b.cash:
        raise IntegrityFailure('Remote cash disagrees with authenticated fill replay')
    for symbol in set(units) | set(b.positions):
        if units.get(symbol,Decimal(0)) != b.positions.get(symbol,Decimal(0)):
            raise IntegrityFailure('Remote holdings disagree with fill replay')
    for cid, row in b.orders.items():
        amount = filled[cid]
        if amount != row['filled']:
            raise IntegrityFailure('Remote order cumulative fill mismatch')
        if (row['state']=='FILLED') != (amount==row['intent'].quantity and amount>0):
            raise IntegrityFailure('Remote final fill state mismatch')
        if row['state']=='PARTIAL' and not 0 < amount < row['intent'].quantity:
            raise IntegrityFailure('Remote partial state mismatch')
        if row['state']=='ACKNOWLEDGED' and amount:
            raise IntegrityFailure('Acknowledged order contains fills')
        if row['state']=='REJECTED' and amount:
            raise IntegrityFailure('Rejected order contains fills')
    if b.calls < len(b.orders):
        raise IntegrityFailure('Remote submit count below order count')


class DurableSimulatedBroker:
    """Crash-resilient *simulation* with SQLite as remote source of truth.

    A durable broker cannot authorize or route real trades. Fault switches
    affect only one instance; confirmed account records remain persistent.
    """
    def __init__(self, path, *, cash=Decimal('10000'), currency='USD'):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.accept_then_timeout=False
        self.reject_next=False
        self.lookup_unavailable=False
        self._initial_cash=D(cash)
        self.currency=currency
        try:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK(id=1), currency TEXT NOT NULL,
                    initial_cash TEXT NOT NULL, state TEXT NOT NULL,
                    state_hash TEXT NOT NULL, sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, operation TEXT NOT NULL,
                    state_hash TEXT NOT NULL, previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL
                );
            ''')
            with self._transaction():
                row=self.db.execute('SELECT * FROM account WHERE id=1').fetchone()
                if row is None:
                    b=SimulatedBroker(self._initial_cash,currency)
                    state=canonical(_encode(b))
                    digest=_hash(state)
                    genesis=_hash('GENESIS|' + digest)
                    self.db.execute('INSERT INTO account VALUES (1,?,?,?,?,?)',
                                    (currency,str(self._initial_cash),state,digest,0))
                    self.db.execute('INSERT INTO events VALUES (0,?,?,?,?)',
                                    ('GENESIS',digest,'GENESIS',genesis))
                self._load()
        except Exception:
            self.close()
            raise

    @contextmanager
    def _transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def _load(self) -> SimulatedBroker:
        row=self.db.execute('SELECT * FROM account WHERE id=1').fetchone()
        if row is None or row['currency']!=self.currency or D(row['initial_cash'])!=self._initial_cash:
            raise IntegrityFailure('Remote simulated account identity or initial funds changed')
        if _hash(row['state']) != row['state_hash']:
            raise IntegrityFailure('Remote simulation state digest mismatch')
        events=self.db.execute('SELECT * FROM events ORDER BY sequence').fetchall()
        if len(events)!=row['sequence']+1:
            raise IntegrityFailure('Remote simulation event count mismatch')
        prior='GENESIS'
        for i,e in enumerate(events):
            if e['sequence']!=i or e['previous_hash']!=prior:
                raise IntegrityFailure('Remote simulation event-chain mismatch')
            digest=_hash(f'{prior}|{i}|{e["operation"]}|{e["state_hash"]}') if i else _hash('GENESIS|'+e['state_hash'])
            if digest!=e['event_hash']:
                raise IntegrityFailure('Remote simulation event hash mismatch')
            prior=digest
        if events[-1]['state_hash']!=row['state_hash']:
            raise IntegrityFailure('Remote simulation state not bound to last event')
        try:
            raw=json.loads(row['state'])
            broker=_decode(raw)
            _financial_invariants(broker,self._initial_cash)
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
            raise IntegrityFailure('Invalid persisted broker snapshot') from exc
        return broker

    def _commit(self, b: SimulatedBroker, operation: str) -> None:
        _financial_invariants(b,self._initial_cash)
        content=canonical(_encode(b)); digest=_hash(content)
        row=self.db.execute('SELECT sequence FROM account WHERE id=1').fetchone()
        i=row['sequence']+1
        prev=self.db.execute('SELECT event_hash FROM events WHERE sequence=?',(i-1,)).fetchone()['event_hash']
        event_hash=_hash(f'{prev}|{i}|{operation}|{digest}')
        self.db.execute('UPDATE account SET state=?,state_hash=?,sequence=? WHERE id=1',
                        (content,digest,i))
        self.db.execute('INSERT INTO events VALUES (?,?,?,?,?)',
                        (i,operation,digest,prev,event_hash))

    def _mutate(self, operation:str, method:str, *args, **kwargs):
        pending_timeout=None
        result=None
        with self._transaction():
            broker=self._load()
            broker.accept_then_timeout=self.accept_then_timeout
            broker.reject_next=self.reject_next
            try:
                result=getattr(broker,method)(*args,**kwargs)
            except AcceptedButTimedOut as e:
                pending_timeout=e
            self.accept_then_timeout=broker.accept_then_timeout
            self.reject_next=broker.reject_next
            self._commit(broker,operation)
        if pending_timeout is not None:
            raise pending_timeout
        return result

    @property
    def calls(self):
        return self._load().calls

    def lookup(self,client_id):
        if self.lookup_unavailable:
            raise ConnectionError('Broker lookup unavailable')
        return self._load().lookup(client_id)

    def submit(self,intent:OrderIntent):
        return self._mutate('SUBMIT:'+intent.client_id,'submit',intent)

    def fill(self,client_id,quantity,price,*,fee_bps=10,fill_id=None):
        return self._mutate('FILL:'+client_id,'fill',client_id,quantity,price,
                            fee_bps=fee_bps,fill_id=fill_id)

    def cancel(self,client_id):
        return self._mutate('CANCEL:'+client_id,'cancel',client_id)

    def snapshot(self):
        return self._load().snapshot()

    def verify(self) -> dict:
        b=self._load()
        row=self.db.execute('SELECT sequence FROM account WHERE id=1').fetchone()
        return {'mode':'DURABLE_SIMULATED_BROKER_ONLY',
                'account_currency':b.currency,'cash':str(b.cash),
                'orders':len(b.orders),'fills':len(b.fills),
                'remote_journal_events':row['sequence']+1,
                'verified':True,'live_approved':False}

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.close()
