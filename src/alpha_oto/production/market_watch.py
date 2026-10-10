"""Read-only external risk-daemon watchdog for local SIMULATION.

An unreachable/failed authority is a critical finding, never approval. The
monitor does not start services, unlock quarantines, issue broker orders, or
reconfigure risk. Its nonzero exit code can be used by launchd/systemd alerts.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import sys

from .risk_service import RiskClient
from .store import canonical


def inspect_authority(client: RiskClient, *, disk_path='.', min_free_bytes=128*1024*1024,
                      now: datetime | None = None) -> dict:
    if not isinstance(min_free_bytes,int) or min_free_bytes < 0:
        raise ValueError('Invalid disk safety threshold')
    checked = now or datetime.now(timezone.utc)
    reasons=[];data=None
    try:
        data=client.health()
        if not isinstance(data,dict):
            raise ValueError('Invalid risk health envelope')
        if data.get('status')!='SIMULATION_RISK_SERVICE_OK':
            reasons.append('RISK_PROCESS_NOT_GREEN:'+str(data.get('status')))
        if data.get('safe_for_new_simulated_intents') is not True:
            reasons.append('RISK_SERVICE_REJECTS_NEW_SIMULATION')
        if data.get('live_approved') is not False:
            reasons.append('UNEXPECTED_LIVE_APPROVAL_FIELD')
        feed=data.get('feed') or {}
        if not feed.get('verified') or not feed.get('quotes'):
            reasons.append('NO_VERIFIED_QUOTES')
        for quote in feed.get('quotes') or []:
            if quote.get('quarantined'):
                reasons.append('FEED_QUARANTINED:'+str(quote.get('symbol')))
    except Exception as exc:
        reasons.append('RISK_UNREACHABLE_OR_UNVERIFIABLE:'+type(exc).__name__)
    try:
        free=shutil.disk_usage(disk_path).free
        if free < min_free_bytes:
            reasons.append('DISK_BELOW_RESERVED_FREE_SPACE')
    except OSError:
        free=None
        reasons.append('DISK_METRICS_UNAVAILABLE')
    return {'checked_at_utc':checked.astimezone(timezone.utc).isoformat(),
            'status':'GREEN' if not reasons else 'CRITICAL',
            'issues':sorted(set(reasons)),'disk_free_bytes':free,
            'risk_status':data.get('status') if isinstance(data,dict) else None,
            'mode':'READ_ONLY_SIMULATOR_WATCHDOG','live_trading_enabled':False}


def append_watch_audit(path,report):
    dest=Path(path);dest.parent.mkdir(parents=True,exist_ok=True)
    encoded=(canonical(report)+'\n').encode()
    fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    try:
        sent=0
        while sent<len(encoded):
            step=os.write(fd,encoded[sent:])
            if step<=0:raise OSError('Watchdog audit write failed')
            sent+=step
        os.fsync(fd)
    finally:os.close(fd)


def main(argv=None):
    p=argparse.ArgumentParser(description='Independent READ-ONLY simulated risk watchdog')
    p.add_argument('--socket',required=True)
    p.add_argument('--disk-path',default='.')
    p.add_argument('--min-free-mb',type=int,default=128)
    p.add_argument('--audit-file',default='artifacts/production/market_watch.jsonl')
    args=p.parse_args(argv)
    result=inspect_authority(RiskClient(args.socket),disk_path=args.disk_path,
                             min_free_bytes=args.min_free_mb*1024*1024)
    append_watch_audit(args.audit_file,result)
    print(json.dumps(result,indent=2,sort_keys=True))
    return 0 if result['status']=='GREEN' else 2


if __name__=='__main__':sys.exit(main())
