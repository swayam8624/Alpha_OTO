"""Read-only agent heartbeat and opportunity watcher; never places orders.

Allows a user-managed service to run forever without sending API keys or
orders. Re-reads local data on each cycle; user must independently refresh it.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Iterable
from .data import read_csv
from .strategies import Breakout, MeanReversion, Trend


STRATEGIES = (Trend(5,20,"trend_5_20"),Trend(10,30,"trend_10_30"),
              MeanReversion(20,-1.25,"revert_20"),Breakout(20,"breakout_20"))


def inspect_file(path: str | Path, *, now: datetime | None = None,
                 max_age_hours: float = 48.0) -> dict:
    """One local snapshot; returns diagnostics, not orders or return promises."""
    if max_age_hours <= 0:
        raise ValueError("max_age_hours must be positive")
    bars = read_csv(path)
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Current time needs timezone")
    as_of=bars[-1].timestamp
    age=(now-as_of).total_seconds()/3600
    eligible=0 <= age <= max_age_hours and len(bars)>=31
    votes={s.name: bool(s.decide(tuple(bars))) if eligible else False for s in STRATEGIES}
    return {"observed_at":now.isoformat(),"symbol":bars[-1].symbol,
            "latest_candle":as_of.isoformat(),"data_age_hours":round(age,4),
            "data_current":eligible,"signals":votes,"signal_count":sum(votes.values()),
            "trade_authorized":False,"reason":"RESEARCH_ONLY_NO_ORDER_API" if eligible
            else "STALE_FUTURE_OR_INSUFFICIENT_DATA"}


def append_jsonl(path: str | Path, record: dict) -> None:
    p=Path(path)
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("a",encoding="utf-8") as f:
        f.write(json.dumps(record,sort_keys=True)+"\n")


def watch(paths: Iterable[str], *, interval_seconds: float=60.0,
          out: str="artifacts/watch.jsonl", once: bool=False,
          max_age_hours: float=48.0) -> None:
    if interval_seconds < 30:
        raise ValueError("Minimum refresh interval 30s to avoid needless CPU usage")
    paths=list(paths)
    if not paths:
        raise ValueError("At least one local CSV path required")
    last_signature: set[tuple] = set()
    while True:
        for path in paths:
            try:
                report=inspect_file(path,max_age_hours=max_age_hours)
                # Idempotent journal: write a new record only when candle or
                # eligibility/signals change. No noise every second.
                signature=(path,report["latest_candle"],report["data_current"],
                           tuple(report["signals"].items()))
                if signature not in last_signature:
                    append_jsonl(out,report)
                    print(json.dumps(report,sort_keys=True),flush=True)
                    last_signature={s for s in last_signature if s[0]!=path}
                    last_signature.add(signature)
            except (OSError,ValueError) as exc:
                # Fail closed; a missing data file may not produce a buy signal.
                warning={"observed_at":datetime.now(timezone.utc).isoformat(),
                         "source":str(path),"trade_authorized":False,
                         "error":str(exc)[:300]}
                append_jsonl(out,warning)
                print(json.dumps(warning),flush=True)
        if once:
            break
        time.sleep(interval_seconds)
