"""Bounded, read-only Coinbase market-data updater. Does not rewrite history."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import tempfile
from hashlib import sha256
from .data import read_csv, write_csv, coinbase_candles


def update_coinbase(csv_path:str, *, now=None, interval_seconds:int=3600) -> dict:
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None: raise ValueError('Timezone aware clock required')
    now=now.astimezone(timezone.utc)
    path=Path(csv_path)
    old=read_csv(path)
    if interval_seconds not in (300,900,3600,21600,86400):
        raise ValueError('Unsupported interval for bounded update')
    symbol=old[0].symbol
    if not symbol.endswith('-USD'):raise ValueError('Only explicitly supported Coinbase USD products')
    # Request overlap for a consistency check. Too-old files need a separately
    # audited range backfill rather than silently skipping days of observations.
    latest=old[-1].timestamp
    start=latest-timedelta(seconds=interval_seconds)
    end=datetime.fromtimestamp(int(now.timestamp()//interval_seconds)*interval_seconds, timezone.utc)
    if end<=latest+timedelta(seconds=interval_seconds):
        return {'status':'NO_NEW_COMPLETED_CANDLES','old_bars':len(old),'new_bars':0,'output':str(path)}
    if (end-start).total_seconds()>299*interval_seconds:
        raise ValueError('Update interval exceeds 299 bars; run a controlled backfill before forward monitoring')
    newly=coinbase_candles(symbol,start,end,interval_seconds,allow_empty=True)
    by_ts={b.timestamp:b for b in old}
    conflicts=[]; additions=0
    for b in newly:
        if b.timestamp in by_ts:
            if by_ts[b.timestamp]!=b:conflicts.append(b.timestamp.isoformat())
        elif b.timestamp>=latest and b.timestamp+timedelta(seconds=interval_seconds)<=now:
            by_ts[b.timestamp]=b;additions+=1
    if conflicts:raise ValueError('Coinbase revised an already stored candle: '+str(conflicts[:5]))
    if not additions:
        return {'status':'NO_NEW_COMPLETED_CANDLES','old_bars':len(old),'new_bars':0,'output':str(path)}
    fd,tmp=tempfile.mkstemp(prefix='.candles_',suffix='.csv',dir=path.parent)
    os.close(fd)
    try:
        write_csv(tmp,[by_ts[t] for t in sorted(by_ts)])
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return {'status':'UPDATED_RESEARCH_DATA_ONLY','old_bars':len(old),'new_bars':additions,
            'output':str(path),'sha256':sha256(path.read_bytes()).hexdigest(),
            'note':'Gaps are not interpolated, older source rows are never overwritten.'}
