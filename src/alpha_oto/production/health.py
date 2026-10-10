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
