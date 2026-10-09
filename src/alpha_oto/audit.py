"""Point-in-time, offline OHLCV data-quality audit.

Data quality is separate from strategy performance. Passing an audit does not
establish that a market is accessible, trading is legal, or a signal is good.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from .data import Bar, validate_series


def audit_bars(bars: list[Bar], *, interval_seconds: int,
               continuous: bool = True, now: datetime | None = None,
               file_path: str | Path | None = None) -> dict:
    """Report missing/unaligned bars. \`continuous\` applies to 24/7 instruments.

    Bars are labelled by START time. A bar that started at 14:00 with a 1h
    interval is only complete at 15:00. Incomplete newest bars are rejected.
    For session-based assets we report irregularities but deliberately DO NOT
    label overnight/weekend closures as missing candles: a real calendar is
    needed to assert coverage.
    """
    validate_series(bars)
    if interval_seconds <= 0 or interval_seconds > 86400:
        raise ValueError("interval_seconds must be between 1 and 86400")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    timestamps = [int(b.timestamp.timestamp()) for b in bars]
    diffs = [b - a for a, b in zip(timestamps, timestamps[1:])]
    misaligned = sum(ts % interval_seconds != 0 for ts in timestamps)
    irregular = sum(delta % interval_seconds != 0 for delta in diffs)
    missing = sum(max(0, delta // interval_seconds - 1) for delta in diffs if delta % interval_seconds == 0)
    expected = ((timestamps[-1] - timestamps[0]) // interval_seconds) + 1
    complete = timestamps[-1] + interval_seconds <= int(now.timestamp())
    continuous_coverage = round(len(bars) / expected, 8) if continuous and not irregular else None
    issues = []
    if misaligned:
        issues.append("BAR_START_NOT_ALIGNED_TO_INTERVAL")
    if irregular:
        issues.append("NON_INTEGER_TIME_STEP")
    if not complete:
        issues.append("LATEST_BAR_NOT_YET_COMPLETE_OR_FROM_FUTURE")
    if continuous and missing:
        issues.append("GAPS_IN_CONTINUOUS_MARKET")
    if not continuous:
        issues.append("SESSION_CALENDAR_NOT_VALIDATED")
    report = {
        "symbol": bars[0].symbol,
        "start": bars[0].timestamp.isoformat(),
        "last_bar_start": bars[-1].timestamp.isoformat(),
        "observed_at": now.isoformat(),
        "interval_seconds": interval_seconds,
        "continuous_market": continuous,
        "observed_bars": len(bars),
        "expected_bars_between_first_and_last": expected if continuous else None,
        "missing_intervals_between_first_and_last": missing if continuous else None,
        "coverage_between_first_and_last": continuous_coverage,
        "noninteger_steps": irregular,
        "misaligned_bars": misaligned,
        "zero_volume_bars": sum(b.volume == 0 for b in bars),
        "latest_bar_complete": complete,
        "issues": issues,
        "quality_pass": not issues,
        "trade_authorized": False,
        "note": "Audit is NOT a market calendar, liquidity test, signal validation or permission to trade.",
    }
    if file_path is not None:
        report["source_file_sha256"] = sha256(Path(file_path).read_bytes()).hexdigest()
    return report
