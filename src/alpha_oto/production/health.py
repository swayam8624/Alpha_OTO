"""Read-only local health audit. Not a redundant production watchdog."""
from __future__ import annotations
from datetime import datetime,timezone
from pathlib import Path
from shutil import disk_usage
from .contracts import D, IntegrityFailure, utc
from .store import LedgerStore


def inspect(store:LedgerStore, *, now:datetime|None=None, min_free_bytes:int=50_000_000)->dict:
    now=utc(now or datetime.now(timezone.utc))
    errors=[]; warnings=[]; evidence={}
    try:
        evidence=store.verify()
    except Exception as exc:
        errors.append('LEDGER_INTEGRITY_FAILURE: '+str(exc))
    try:
        if store._meta('halted')=='1':errors.append('RISK_HALT_ACTIVE')
        ambiguous=[x['client_id'] for x in store.orders() if x['state'] in ('UNKNOWN','SUBMITTING')]
        if ambiguous:errors.append('UNRESOLVED_ORDER_SUBMISSION: '+','.join(ambiguous))
        unsubmitted=[x['client_id'] for x in store.orders() if x['state']=='CREATED']
        if unsubmitted:warnings.append('UNSUBMITTED_LOCAL_INTENTS: '+','.join(unsubmitted))
        for symbol,p in store.positions().items():
            if p['quantity']:
                mark=store.db.execute('SELECT time FROM marks WHERE symbol=?',(symbol,)).fetchone()
                if not mark or abs((now-datetime.fromisoformat(mark['time'])).total_seconds())>store.limits.max_quote_age_seconds:
                    errors.append('STALE_POSITION_MARK: '+symbol)
        free=disk_usage(store.path.parent).free
        if free<min_free_bytes:errors.append('LOW_DISK_SPACE')
    except Exception as exc:
        errors.append('HEALTH_INSPECTION_FAILED: '+str(exc))
    return {'mode':'SIMULATED_BROKER_ONLY','status':'RED' if errors else 'YELLOW' if warnings else 'GREEN',
            'safe_for_new_simulated_orders':not errors,'live_approved':False,
            'critical':errors,'warnings':warnings,'financial_integrity':evidence,
            'evaluated_at':now.isoformat(),
            'note':'Local self-check only. No independent watchdog, external alerting, real broker, or live authorization.'}


def inspect_durable_pair(store:LedgerStore, broker, *, now:datetime|None=None)->dict:
    """Read-only comparison of TWO independent simulated SQLite authorities.

    Does not reconcile or change either database: differences are reported so
    recovery tooling can reconcile without obscuring the initial discrepancy.
    A green report is a simulation invariant, NEVER a live trading approval.
    """
    issues=[]
    evidence={}
    try:
        evidence['local_ledger']=store.verify()
    except Exception as exc:
        issues.append('LOCAL_LEDGER_INTEGRITY:'+str(exc))
    try:
        evidence['remote_broker']=broker.verify()
        remote=broker.snapshot()
        acct=store.account()
        if remote['currency']!=store.currency or remote['cash']!=D(acct['cash']):
            issues.append('CASH_OR_CURRENCY_DIVERGENCE')
        positions=store.positions()
        for symbol in set(positions) | set(remote['positions']):
            loc=positions.get(symbol,{}).get('quantity',D('0'))
            rem=remote['positions'].get(symbol,D('0'))
            if loc!=rem:
                issues.append('POSITION_DIVERGENCE:'+symbol)
        local_orders={o['client_id']:o for o in store.orders()}
        for cid, remote_order in remote['orders'].items():
            local=local_orders.get(cid)
            if local is None:
                issues.append('UNRECOGNIZED_REMOTE_ORDER:'+cid)
            elif (local['symbol']!=remote_order['intent'].symbol or
                  local['side']!=remote_order['intent'].side or
                  D(local['quantity'])!=remote_order['intent'].quantity or
                  D(local['limit_px'])!=remote_order['intent'].limit):
                issues.append('ORDER_PAYLOAD_DIVERGENCE:'+cid)
            elif local['state']!=remote_order['state'] or D(local['filled'])!=remote_order['filled']:
                issues.append('ORDER_STATE_DIVERGENCE:'+cid)
        for cid in local_orders:
            if cid not in remote['orders']:
                issues.append('LOCAL_ONLY_ORDER:'+cid)
        local_fills={r['fill_id']:(r['client_id'],D(r['quantity']),D(r['price']),D(r['fee']))
                     for r in store.db.execute('SELECT fill_id, client_id, quantity, price, fee FROM fills')}
        remote_fills={f.id:(f.client_id,f.quantity,f.price,f.fee) for f in remote['fills']}
        for fid in sorted(set(local_fills) | set(remote_fills)):
            if local_fills.get(fid)!=remote_fills.get(fid):
                issues.append('FILL_JOURNAL_DIVERGENCE:'+fid)
        # A halted account must never be described as permitted for new risk.
        if store._meta('halted')=='1':
            issues.append('LOCAL_RISK_HALT')
    except Exception as exc:
        issues.append('READ_ONLY_BROKER_INSPECTION_FAILED:'+str(exc))
    return {'mode':'TWO_SQLITE_SIMULATOR_ONLY','status':'RED' if issues else 'GREEN',
            'live_approved':False,'safe_for_new_simulated_orders':not issues,
            'issues':issues,'evidence':evidence,
            'note':'Read-only comparison only. No broker account or real executable quotes.'}
