"""Explicit Coinbase gap re-download; never interpolate missing real candles."""
from __future__ import annotations
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from .data import GRANULARITIES, coinbase_candles, read_csv, write_csv
from .audit import audit_bars


def gap_intervals(bars, interval_seconds: int) -> list[tuple]:
    step = timedelta(seconds=interval_seconds)
    result = []
    for before, after in zip(bars,bars[1:]):
        at = before.timestamp + step
        while at < after.timestamp:
            result.append((at, at + step))
            at += step
    return result


def repair_coinbase(csv_path: str, *, interval_seconds: int = 3600,
                    max_missing: int = 250, overwrite: bool = False) -> dict:
    """Fetch *only* missing spans. Original input preserved unless overwrite.

    This is read-only public market data; do not mistake a missing Coinbase
    candle for a zero volume/flat-price interval.
    """
    if interval_seconds not in GRANULARITIES:
        raise ValueError(f"Coinbase does not support interval_seconds={interval_seconds}")
    if max_missing < 0:
        raise ValueError("max_missing must be non-negative")
    bars = read_csv(csv_path)
    gaps = gap_intervals(bars,interval_seconds)
    if len(gaps)>max_missing:
        raise ValueError(f"Refusing repair of {len(gaps)} missing intervals (max {max_missing})")
    symbol = bars[0].symbol
    if not symbol.endswith("-USD"):
        raise ValueError("Only explicitly supported Coinbase USD products")
    combined = {b.timestamp:b for b in bars}
    before_hash = sha256(Path(csv_path).read_bytes()).hexdigest()
    missing_times = {start for start, _ in gaps}
    recovered_times = set()
    for start,end in gaps:
        # Enlarge around boundary due to occasional exclusive endpoint behavior.
        nearby = coinbase_candles(symbol, start-timedelta(seconds=interval_seconds),
                                  end+timedelta(seconds=interval_seconds),interval_seconds,
                                  allow_empty=True)
        for candle in nearby:
            if candle.timestamp in combined:
                old=combined[candle.timestamp]
                # Preserve original candle instead of silently overwriting it.
                if old!=candle:
                    raise ValueError("Conflicting candles from source; manual investigation required")
            elif candle.timestamp in missing_times:
                # Do not extend the original requested period or invent bars.
                combined[candle.timestamp]=candle
                recovered_times.add(candle.timestamp)
    result_bars=[combined[t] for t in sorted(combined)]
    target=Path(csv_path) if overwrite else Path(csv_path).with_name(Path(csv_path).stem+"_repaired.csv")
    write_csv(target,result_bars)
    report=audit_bars(result_bars,interval_seconds=interval_seconds,continuous=True,file_path=target)
    report["original_data_sha256"]=before_hash
    unresolved = sorted(missing_times - recovered_times)
    report["intervals_refetched"]=len(gaps)
    report["recovered_intervals"]=len(recovered_times)
    report["unresolved_intervals"]=len(unresolved)
    report["unresolved_timestamps_utc"]=[t.isoformat() for t in unresolved]
    report["repair_status"] = ("NO_GAPS" if not gaps else
        "COMPLETE" if not unresolved else "PARTIAL_SOURCE_UNAVAILABLE")
    report["note_on_unresolved"] = (
        "Coinbase supplied no verified candle for these times. They remain gaps; "
        "never forward-fill or synthesize trading prices. Missing-window ML samples "
        "must remain excluded.")
    report["output_csv"]=str(target)
    Path(str(target)+".repair.json").write_text(json.dumps(report,indent=2)+"\n")
    return report
