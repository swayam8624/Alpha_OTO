"""Crash-consistent SQLite order/event/account ledger for *simulated* orders.

Every fill and its financial postings commit atomically in one transaction.
A chained event journal provides tamper evidence, not a cryptographic signature.
This module cannot connect to any exchange or transmit an order.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .contracts import D, IntegrityFailure, Instrument, OrderIntent, Quote, RiskLimits, RiskRejected, utc

OPEN_STATES=('CREATED','SUBMITTING','UNKNOWN','ACKNOWLEDGED','PARTIAL')
TERMINAL_STATES=('FILLED','REJECTED','CANCELLED')
STATES=OPEN_STATES+TERMINAL_STATES
TRANSITIONS={
    'CREATED':{'SUBMITTING','UNKNOWN','ACKNOWLEDGED','REJECTED'},
    'SUBMITTING':{'UNKNOWN','ACKNOWLEDGED','PARTIAL','FILLED','REJECTED'},
    'UNKNOWN':{'ACKNOWLEDGED','PARTIAL','FILLED','REJECTED','CANCELLED'},
    'ACKNOWLEDGED':{'PARTIAL','FILLED','REJECTED','CANCELLED','UNKNOWN'},
    'PARTIAL':{'PARTIAL','FILLED','CANCELLED','UNKNOWN'},
    'FILLED':set(), 'REJECTED':set(), 'CANCELLED':set(),
}


def canonical(obj)->str:
    return json.dumps(obj,sort_keys=True,separators=(',',':'),default=str,allow_nan=False)


class LedgerStore:
    def __init__(self, path, *, initial_cash=Decimal('10000'), currency='USD',
                 instruments:tuple[Instrument,...], limits:RiskLimits=RiskLimits()):
        if not instruments or len({i.symbol for i in instruments}) != len(instruments):
            raise ValueError('Unique nonempty instrument list required')
        if any(i.currency!=currency for i in instruments) or D(initial_cash)<=0:
            raise ValueError('Account currencies must match and initial cash >0')
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(str(self.path),isolation_level=None,timeout=5)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.currency=currency
        self.instruments={i.symbol:i for i in instruments}
        self.limits=limits
        try:
            self._schema()
            limits_signature=sha256(canonical(asdict(self.limits)).encode()).hexdigest()
            with self.transaction():
                orig=self.db.execute('SELECT value FROM meta WHERE key="currency"').fetchone()
                if orig:
                    if orig['value']!=currency:
                        raise IntegrityFailure('Currency mismatch on existing account')
                    if self._meta('limits_signature')!=limits_signature:
                        raise IntegrityFailure('Risk configuration changed; requires a new authorized account version')
                    if self._meta('initial_cash')!=str(D(initial_cash)):
                        raise IntegrityFailure('Initial balance changed on existing account')
                    for i in instruments:
                        stored=self.db.execute('SELECT * FROM instruments WHERE symbol=?',(i.symbol,)).fetchone()
                        if stored is None or (stored['venue'],stored['currency'],stored['tick'],stored['lot'],stored['max_units'],stored['authorized'])!=(i.venue,i.currency,str(i.tick),str(i.lot),str(i.max_units),int(i.authorized)):
                            raise IntegrityFailure('Instrument registry altered since account initialization')
                    if len(self.instruments)!=self.db.execute('SELECT count(*) FROM instruments').fetchone()[0]:
                        raise IntegrityFailure('Missing/mismatched instrument registry')
                else:
                    self.db.execute('INSERT INTO meta VALUES (?,?)',('limits_signature',limits_signature))
                    self.db.execute('INSERT INTO meta VALUES (?,?)',('currency',currency))
                    self.db.execute('INSERT INTO meta VALUES (?,?)',('initial_cash',str(D(initial_cash))))
                    self.db.execute('INSERT INTO meta VALUES (?,?)',('halted','0'))
                    self.db.execute('INSERT INTO meta VALUES (?,?)',('peak_equity',str(D(initial_cash))))
                    for i in instruments:
                        self.db.execute('INSERT INTO instruments VALUES (?,?,?,?,?,?,?)',
                                        (i.symbol,i.venue,i.currency,str(i.tick),str(i.lot),str(i.max_units),int(i.authorized)))
                        self.db.execute('INSERT INTO positions VALUES (?,?,?)',(i.symbol,'0','0'))
                    self.db.execute('INSERT INTO accounts VALUES (?,?,?,?)',(currency,str(D(initial_cash)),'0','0'))
                    self._postings('initial-capital',[(f'CASH:{currency}',D(initial_cash)),
                                                        ('EXTERNAL_CAPITAL',-D(initial_cash))])
                    self._event('ACCOUNT_OPENED',None,{'currency':currency,'initial_cash':str(D(initial_cash)),
                                                       'instruments':sorted(self.instruments)})

            self.verify()
        except BaseException:
            self.db.close()
            raise

    def close(self): self.db.close()

    def _schema(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS instruments(symbol TEXT PRIMARY KEY,venue TEXT NOT NULL,currency TEXT NOT NULL,
            tick TEXT NOT NULL,lot TEXT NOT NULL,max_units TEXT NOT NULL,authorized INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS accounts(currency TEXT PRIMARY KEY,cash TEXT NOT NULL,
            realized TEXT NOT NULL,fees TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY,quantity TEXT NOT NULL,basis TEXT NOT NULL,
            FOREIGN KEY(symbol) REFERENCES instruments(symbol));
        CREATE TABLE IF NOT EXISTS orders(client_id TEXT PRIMARY KEY,symbol TEXT NOT NULL,side TEXT NOT NULL,
            quantity TEXT NOT NULL,limit_px TEXT NOT NULL,filled TEXT NOT NULL,state TEXT NOT NULL,
            remote_id TEXT,created_at TEXT NOT NULL,strategy TEXT NOT NULL,model_version TEXT NOT NULL,
            FOREIGN KEY(symbol) REFERENCES instruments(symbol));
        CREATE TABLE IF NOT EXISTS fills(fill_id TEXT PRIMARY KEY,client_id TEXT NOT NULL,
            quantity TEXT NOT NULL,price TEXT NOT NULL,fee TEXT NOT NULL,
            observed_at TEXT NOT NULL, realized TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES orders(client_id));
        CREATE TABLE IF NOT EXISTS postings(id INTEGER PRIMARY KEY AUTOINCREMENT,group_id TEXT NOT NULL,
            account TEXT NOT NULL,amount TEXT NOT NULL,currency TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS journal(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,
            client_id TEXT,payload TEXT NOT NULL,prev_hash TEXT NOT NULL,hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS marks(symbol TEXT PRIMARY KEY,price TEXT NOT NULL,time TEXT NOT NULL);
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _meta(self,key):
        x=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        if x is None:raise IntegrityFailure('Missing account metadata')
        return x['value']

    def _event(self,kind,client_id,payload):
        last=self.db.execute('SELECT seq,hash FROM journal ORDER BY seq DESC LIMIT 1').fetchone()
        prev=last['hash'] if last else 'GENESIS'
        record={'kind':kind,'client_id':client_id,'payload':payload,'prev_hash':prev}
        digest=sha256(canonical(record).encode()).hexdigest()
        self.db.execute('INSERT INTO journal(kind,client_id,payload,prev_hash,hash) VALUES (?,?,?,?,?)',
                        (kind,client_id,canonical(payload),prev,digest))

    def _postings(self,group,entries):
        if sum((amount for _,amount in entries),Decimal(0))!=0:
            raise IntegrityFailure('Unbalanced financial postings')
        for name,amount in entries:
            self.db.execute('INSERT INTO postings(group_id,account,amount,currency) VALUES (?,?,?,?)',
                            (group,name,str(amount),self.currency))

    def verify(self):
        prev='GENESIS'
        for row in self.db.execute('SELECT * FROM journal ORDER BY seq'):
            expected=sha256(canonical({'kind':row['kind'],'client_id':row['client_id'],
                'payload':json.loads(row['payload']),'prev_hash':prev}).encode()).hexdigest()
            if expected!=row['hash'] or row['prev_hash']!=prev:
                raise IntegrityFailure('Journal chain modified')
            prev=row['hash']
        groups={}
        accounts={}
        for row in self.db.execute('SELECT * FROM postings'):
            amount=D(row['amount']);groups[row['group_id']]=groups.get(row['group_id'],Decimal(0))+amount
            accounts[row['account']]=accounts.get(row['account'],Decimal(0))+amount
        if any(v!=0 for v in groups.values()):
            raise IntegrityFailure('Financial ledger does not balance')
        cash=D(self.account()['cash'])
        if cash!=accounts.get(f'CASH:{self.currency}',Decimal(0)):
            raise IntegrityFailure('Cash diverged from ledger')
        for row in self.db.execute('SELECT * FROM positions'):
            symbol=row['symbol']
            if D(row['basis'])!=accounts.get(f'POSITION_COST:{symbol}',Decimal(0)):
                raise IntegrityFailure('Position book diverged from cost ledger')
            quantity=sum((D(f['quantity'])*(1 if o['side']=='BUY' else -1)
                          for f in self.db.execute('SELECT * FROM fills WHERE client_id IN (SELECT client_id FROM orders WHERE symbol=?)',(symbol,))
                          for o in [self.order(f['client_id'])]),Decimal(0))
            if quantity!=D(row['quantity']) or quantity<0:
                raise IntegrityFailure('Position quantity diverged from fills')
        for row in self.db.execute('SELECT * FROM orders'):
            executed=sum((D(f['quantity']) for f in self.db.execute('SELECT quantity FROM fills WHERE client_id=?',(row['client_id'],))),Decimal(0))
            if executed!=D(row['filled']) or executed>D(row['quantity']) or row['state'] not in STATES:
                raise IntegrityFailure('Order state/fill mismatch')
        if -accounts.get('REALIZED_PNL',Decimal(0))!=D(self.account()['realized']):
            raise IntegrityFailure('Realized P&L mismatch')
        if accounts.get('FEES',Decimal(0))!=D(self.account()['fees']):
            raise IntegrityFailure('Fees mismatch')
        return {'journal_events':self.db.execute('SELECT COUNT(*) FROM journal').fetchone()[0],
                'postings':self.db.execute('SELECT COUNT(*) FROM postings').fetchone()[0],
                'fills':self.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0],
                'accounting_verified':True}

    def account(self):
        return dict(self.db.execute('SELECT * FROM accounts WHERE currency=?',(self.currency,)).fetchone())

    def positions(self):
        return {r['symbol']:{'quantity':D(r['quantity']),'basis':D(r['basis'])}
                for r in self.db.execute('SELECT * FROM positions')}

    def order(self,client_id):
        row=self.db.execute('SELECT * FROM orders WHERE client_id=?',(client_id,)).fetchone()
        return dict(row) if row else None

    def orders(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM orders ORDER BY created_at,client_id')]

    def set_halt(self,reason):
        with self.transaction():
            self.db.execute('UPDATE meta SET value="1" WHERE key="halted"')
            self._event('EMERGENCY_HALT',None,{'reason':str(reason)})

    def _reserve(self):
        bought={s:Decimal(0) for s in self.instruments}
        sold={s:Decimal(0) for s in self.instruments}
        reserved=Decimal(0)
        for row in self.db.execute('SELECT * FROM orders'):
            if row['state'] not in OPEN_STATES:continue
            left=D(row['quantity'])-D(row['filled'])
            if row['side']=='BUY':
                bought[row['symbol']]+=left
                reserved+=left*D(row['limit_px'])*(Decimal(1)+self.limits.fee_reserve_bps/Decimal(10000))
            else:sold[row['symbol']]+=left
        return bought,sold,reserved

    def _risk(self,intent:OrderIntent,quote:Quote,now:datetime):
        if self._meta('halted')=='1':raise RiskRejected('Independent emergency risk halt')
        if self.db.execute('SELECT count(*) FROM orders WHERE state IN ("SUBMITTING","UNKNOWN")').fetchone()[0]:
            raise RiskRejected('Unresolved broker submission; account quarantined')
        instrument=self.instruments.get(intent.symbol)
        if instrument is None or not instrument.authorized:
            raise RiskRejected('Instrument not explicitly authorized')
        if quote.symbol!=intent.symbol:raise RiskRejected('Quote symbol mismatch')
        if now<utc(intent.created_at):raise RiskRejected('Intent timestamp from future')
        if (now-utc(intent.created_at)).total_seconds()>self.limits.max_intent_age_seconds:
            raise RiskRejected('Expired trading intention')
        if utc(quote.received_time)>now:
            raise RiskRejected('Quote has not yet been received')
        age=(now-utc(quote.source_time)).total_seconds()
        lag=(utc(quote.received_time)-utc(quote.source_time)).total_seconds()
        if age<0 or age>self.limits.max_quote_age_seconds or lag>self.limits.max_receive_delay_seconds:
            raise RiskRejected('Stale/future/excess-latency quote')
        if (quote.ask-quote.bid)/quote.ask*Decimal(10000)>self.limits.max_spread_bps:
            raise RiskRejected('Market spread too wide')
        if intent.quantity%instrument.lot or intent.limit%instrument.tick or intent.quantity>instrument.max_units:
            raise RiskRejected('Invalid lot size, tick, or order units')
        if intent.side=='BUY':
            if quote.ask_size<intent.quantity or quote.ask>intent.limit:
                raise RiskRejected('No executable buying depth inside limit')
        else:
            if quote.bid_size<intent.quantity or quote.bid<intent.limit:
                raise RiskRejected('No executable selling depth inside limit')
        requested=intent.limit*intent.quantity
        if requested>self.limits.max_order_notional:raise RiskRejected('Order notional cap')
        current=self.positions(); buyp,sellp,reserved=self._reserve()
        acct=self.account()
        if intent.side=='BUY':
            required=requested*(Decimal(1)+self.limits.fee_reserve_bps/Decimal(10000))
            if D(acct['cash'])-reserved-required<self.limits.cash_reserve:
                raise RiskRejected('Insufficient unreserved cash')
            new_qty=current[intent.symbol]['quantity']+buyp[intent.symbol]+intent.quantity
            if new_qty*intent.limit>self.limits.max_symbol_notional:
                raise RiskRejected('Per-symbol exposure cap')
        else:
            if current[intent.symbol]['quantity']-sellp[intent.symbol]<intent.quantity:
                raise RiskRejected('Cannot sell absent/unreserved inventory')
        count=self.db.execute('SELECT COUNT(*) FROM orders WHERE state IN ("CREATED","SUBMITTING","UNKNOWN","ACKNOWLEDGED","PARTIAL")').fetchone()[0]
        if count>=self.limits.max_open_orders:raise RiskRejected('Too many unresolved orders')
        day=utc(now).date().isoformat()
        daily=self.db.execute('SELECT count(*) FROM orders WHERE substr(created_at,1,10)=?',(day,)).fetchone()[0]
        if daily>=self.limits.max_daily_orders:raise RiskRejected('Daily order-rate limit')
        start=D(self._meta('initial_cash'))
        today=now.date().isoformat()
        today_net=sum((D(r['realized'])-D(r['fee'])
            for r in self.db.execute('SELECT realized,fee FROM fills WHERE substr(observed_at,1,10)=?',(today,))),Decimal(0))
        if intent.side=='BUY' and today_net<-self.limits.max_daily_realized_loss:
            raise RiskRejected('Daily realized net loss stop')
        # Any held asset must have a current mark; otherwise portfolio NAV is
        # unknown and more risk is forbidden. Sell-only may be allowed only with
        # explicit operator review (not here).
        nav=D(acct['cash'])
        for sym,p in current.items():
            if p['quantity']==0:continue
            mark=self.db.execute('SELECT * FROM marks WHERE symbol=?',(sym,)).fetchone()
            if sym==intent.symbol:
                px=quote.bid
            elif mark and 0 <= (now-datetime.fromisoformat(mark['time'])).total_seconds()<=self.limits.max_quote_age_seconds:
                px=D(mark['price'])
            else:raise RiskRejected('Missing current mark for existing holdings')
            nav+=p['quantity']*px
        if intent.side=='BUY' and nav < D(self._meta('peak_equity'))*(1-self.limits.max_peak_drawdown_fraction):
            raise RiskRejected('Drawdown risk halt')
        def current_price(sym):
            if sym==intent.symbol:
                return quote.ask
            mark=self.db.execute('SELECT * FROM marks WHERE symbol=?',(sym,)).fetchone()
            if mark is None:
                raise RiskRejected('Missing valuation for reserved exposure: '+sym)
            delta=(now-datetime.fromisoformat(mark['time'])).total_seconds()
            if not 0<=delta<=self.limits.max_quote_age_seconds:
                raise RiskRejected('Stale valuation for reserved exposure: '+sym)
            return D(mark['price'])
        gross=sum((p['quantity']*current_price(sym) for sym,p in current.items() if p['quantity']),Decimal(0))
        buy_exposure=sum((q*current_price(s) for s,q in buyp.items() if q),Decimal(0))
        if intent.side=='BUY' and gross+buy_exposure+requested>self.limits.max_gross_notional:
            raise RiskRejected('Gross exposure cap')

    def record_mark(self,quote:Quote,now:datetime):
        now=utc(now)
        if quote.symbol not in self.instruments:raise RiskRejected('Unknown mark symbol')
        if (now-utc(quote.source_time)).total_seconds()>self.limits.max_quote_age_seconds or quote.source_time>now:
            raise RiskRejected('Cannot record stale mark')
        with self.transaction():
            self.db.execute('INSERT INTO marks(symbol,price,time) VALUES (?,?,?) ON CONFLICT(symbol) DO UPDATE SET price=excluded.price,time=excluded.time',
                            (quote.symbol,str(quote.bid),now.isoformat()))
            nav=D(self.account()['cash'])
            poses=self.positions()
            fresh=True
            for sym,p in poses.items():
                if p['quantity']==0:continue
                row=self.db.execute('SELECT * FROM marks WHERE symbol=?',(sym,)).fetchone()
                if row is None or (now-datetime.fromisoformat(row['time'])).total_seconds()>self.limits.max_quote_age_seconds:
                    fresh=False;break
                nav+=p['quantity']*D(row['price'])
            if fresh and nav>D(self._meta('peak_equity')):
                self.db.execute('UPDATE meta SET value=? WHERE key="peak_equity"',(str(nav),))
            if fresh and nav<D(self._meta('peak_equity'))*(1-self.limits.max_peak_drawdown_fraction):
                self.db.execute('UPDATE meta SET value="1" WHERE key="halted"')
                self._event('AUTOMATIC_DRAWDOWN_HALT',None,{'nav':str(nav),'peak':self._meta('peak_equity')})

    def create_intent(self,intent:OrderIntent,quote:Quote,now:datetime):
        now=utc(now)
        with self.transaction():
            old=self.order(intent.client_id)
            if old:
                if (old['symbol'],old['side'],D(old['quantity']),D(old['limit_px']),
                    old['created_at'],old['strategy'],old['model_version'])!=(intent.symbol,intent.side,intent.quantity,intent.limit,utc(intent.created_at).isoformat(),intent.strategy,intent.model_version):
                    raise IntegrityFailure('Client id reused with different order payload')
                return old
            self._risk(intent,quote,now)
            self.db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (intent.client_id,intent.symbol,intent.side,str(intent.quantity),str(intent.limit),'0',
                 'CREATED',None,utc(intent.created_at).isoformat(),intent.strategy,intent.model_version))
            self._event('ORDER_INTENT_ACCEPTED',intent.client_id,{'symbol':intent.symbol,'side':intent.side,
                'quantity':str(intent.quantity),'limit':str(intent.limit),'strategy':intent.strategy,
                'model':intent.model_version,'quote_time':utc(quote.source_time).isoformat()})
            return self.order(intent.client_id)

    def transition(self,client_id,state,remote_id=None):
        if state not in STATES:raise ValueError('Bad transition state')
        with self.transaction():
            prior=self.order(client_id)
            if prior is None:raise IntegrityFailure('Unknown order ID')
            if state==prior['state']:
                if remote_id and prior['remote_id'] not in (None,remote_id):
                    raise IntegrityFailure('Broker ID conflict')
                return prior
            if state not in TRANSITIONS[prior['state']]:
                raise IntegrityFailure(f"Illegal order transition: {prior['state']} -> {state}")
            self.db.execute('UPDATE orders SET state=?,remote_id=COALESCE(?,remote_id) WHERE client_id=?',
                            (state,remote_id,client_id))
            self._event('ORDER_STATE',client_id,{'before':prior['state'],'after':state,'remote_id':remote_id})
            return self.order(client_id)

    def bind_broker_id(self,client_id:str,remote_id:str):
        if not remote_id:raise ValueError('Missing broker order identity')
        with self.transaction():
            row=self.order(client_id)
            if row is None:raise IntegrityFailure('Unrecognized broker identity')
            if row['remote_id'] not in (None,remote_id):
                raise IntegrityFailure('Conflicting broker order identity')
            if row['remote_id'] is None:
                self.db.execute('UPDATE orders SET remote_id=? WHERE client_id=?',(remote_id,client_id))
                self._event('BROKER_ID_BOUND',client_id,{'remote_id':remote_id})

    def record_fill(self,fill,now:datetime):
        utc(now)
        with self.transaction():
            old=self.db.execute('SELECT * FROM fills WHERE fill_id=?',(fill.id,)).fetchone()
            if old:
                if (old['client_id'],D(old['quantity']),D(old['price']),D(old['fee']))!=(fill.client_id,fill.quantity,fill.price,fill.fee):
                    raise IntegrityFailure('Duplicate fill id has different financial data')
                return False
            order=self.order(fill.client_id)
            if order is None or order['state'] in TERMINAL_STATES:
                raise IntegrityFailure('Fill for nonexistent/terminal order')
            qty,price,fee=fill.quantity,fill.price,fill.fee
            if qty<=0 or price<=0 or fee<0 or D(order['filled'])+qty>D(order['quantity']):
                raise IntegrityFailure('Invalid or excessive fill')
            inst=self.instruments[order['symbol']]
            if qty%inst.lot or price%inst.tick:
                raise IntegrityFailure('Off-grid broker fill')
            if (order['side']=='BUY' and price>D(order['limit_px'])) or (order['side']=='SELL' and price<D(order['limit_px'])):
                raise IntegrityFailure('Broker fill breaches limit')
            position=self.positions()[order['symbol']]
            acct=self.account();amount=qty*price
            if order['side']=='BUY':
                if D(acct['cash'])<amount+fee:raise IntegrityFailure('Insufficient cash at actual fill')
                new_cash=D(acct['cash'])-amount-fee
                new_qty=position['quantity']+qty
                new_basis=position['basis']+amount
                realized=D(acct['realized'])
                postings=[(f'POSITION_COST:{order["symbol"]}',amount),('FEES',fee),
                          (f'CASH:{self.currency}',-amount-fee)]
            else:
                if qty>position['quantity']:raise IntegrityFailure('Short sale prohibited')
                cost=position['basis']*qty/position['quantity']
                proceeds=amount-fee
                new_cash=D(acct['cash'])+proceeds
                new_qty=position['quantity']-qty
                new_basis=position['basis']-cost if new_qty else Decimal(0)
                realized=D(acct['realized'])+amount-cost
                postings=[(f'CASH:{self.currency}',proceeds),('FEES',fee),
                          (f'POSITION_COST:{order["symbol"]}',-cost),('REALIZED_PNL',-(amount-cost))]
            self._postings('fill:'+fill.id,postings)
            self.db.execute('UPDATE accounts SET cash=?,realized=?,fees=? WHERE currency=?',
                            (str(new_cash),str(realized),str(D(acct['fees'])+fee),self.currency))
            self.db.execute('UPDATE positions SET quantity=?,basis=? WHERE symbol=?',
                            (str(new_qty),str(new_basis),order['symbol']))
            sale_gross=(amount-cost) if order['side']=='SELL' else Decimal(0)
            self.db.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?)',
                            (fill.id,fill.client_id,str(qty),str(price),str(fee),utc(now).isoformat(),str(sale_gross)))
            total=D(order['filled'])+qty
            state='FILLED' if total==D(order['quantity']) else 'PARTIAL'
            self.db.execute('UPDATE orders SET filled=?,state=? WHERE client_id=?',
                            (str(total),state,fill.client_id))
            self._event('BROKER_FILL',fill.client_id,{'fill_id':fill.id,'qty':str(qty),
                          'px':str(price),'fee':str(fee),'state':state})
            return True

    def summary(self):
        acct=self.account();self.verify()
        return {'mode':'SIMULATOR_ONLY_LIVE_ORDERS_IMPOSSIBLE','cash':acct['cash'],
                'currency':self.currency,'realized_gross':acct['realized'],'fees':acct['fees'],
                'peak_equity':self._meta('peak_equity'),'halted':self._meta('halted')=='1',
                'positions':{s:{k:str(v) for k,v in p.items()} for s,p in self.positions().items()},
                'orders':self.orders(),'journal_events':self.verify()['journal_events']}
