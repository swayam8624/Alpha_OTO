"""Strict OHLCV ingestion and optional public, read-only Coinbase history.

No API credentials, trading endpoints, private account APIs or remote LLMs exist.
The public feed is a research convenience, not a licensed commercial data feed.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

UTC = timezone.utc
REQUIRED = ("timestamp", "symbol", "open", "high", "low", "close", "volume")
GRANULARITIES = {60, 300, 900, 3600, 21600, 86400}


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        import math
        prices = (self.open, self.high, self.low, self.close)
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("OHLCV timestamps must include a UTC offset")
        if not self.symbol or any(not math.isfinite(x) or x <= 0 for x in prices):
            raise ValueError("Invalid symbol/price")
        if not math.isfinite(self.volume) or self.volume < 0:
            raise ValueError("Invalid volume")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("Inconsistent OHLC prices")


def validate_series(bars: list[Bar]) -> list[Bar]:
    if not bars:
        raise ValueError("Empty OHLCV series")
    symbol = bars[0].symbol
    for prev, curr in zip(bars, bars[1:]):
        if curr.symbol != symbol:
            raise ValueError("One symbol per series; use separate experiments for other assets")
        if curr.timestamp <= prev.timestamp:
            raise ValueError("OHLCV must be strictly increasing in time; duplicate timestamps rejected")
    return bars


def read_csv(path: str | Path) -> list[Bar]:
    with Path(path).open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not set(REQUIRED).issubset(reader.fieldnames or []):
            raise ValueError(f"Missing OHLCV columns: {REQUIRED}")
        bars = []
        for r in reader:
            timestamp = datetime.fromisoformat(r["timestamp"])
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("CSV timestamp must include timezone offset")
            bars.append(Bar(timestamp.astimezone(UTC), r["symbol"],
                            *[float(r[k]) for k in REQUIRED[2:]]))
    return validate_series(bars)


def write_csv(path: str | Path, bars: list[Bar]) -> None:
    validate_series(bars)
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(REQUIRED)
        for b in bars:
            writer.writerow((b.timestamp.isoformat(), b.symbol, b.open, b.high, b.low, b.close, b.volume))


def coinbase_candles(product: str, start: datetime, end: datetime, granularity: int = 3600) -> list[Bar]:
    """Fetch Coinbase Exchange public historical candles (at most 300 intervals/request).

    Access is read-only. Check the current endpoint terms before retaining or
    using downloaded market data. No inference about executable fills is valid
    from OHLC candles. This function is NOT used by offline unit tests.
    """
    if granularity not in GRANULARITIES:
        raise ValueError(f"Unsupported granularity: {granularity}")
    if start.tzinfo is None or end.tzinfo is None or not start < end:
        raise ValueError("Require valid timezone-aware start < end")
    if not product or not all(c.isalnum() or c in "-_" for c in product):
        raise ValueError("Invalid Coinbase product identifier")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    by_ts: dict[datetime, Bar] = {}
    cursor = start
    # 299 intervals avoids the API's documented 300-candle maximum.
    while cursor < end:
        nxt = min(cursor + timedelta(seconds=299 * granularity), end)
        params = urlencode({"start": cursor.isoformat(), "end": nxt.isoformat(), "granularity": granularity})
        url = f"https://api.exchange.coinbase.com/products/{quote(product)}/candles?{params}"
        request = Request(url, headers={"User-Agent": "AlphaOTO-research/0.1", "Accept": "application/json"})
        with urlopen(request, timeout=15) as reply:
            rows = json.load(reply)
        if not isinstance(rows, list):
            raise RuntimeError(f"Coinbase candle response error: {str(rows)[:250]}")
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            stamp, low, high, opn, close, vol = row[:6]
            ts = datetime.fromtimestamp(int(stamp), UTC)
            if start <= ts < end:
                by_ts[ts] = Bar(ts, product, float(opn), float(high), float(low), float(close), float(vol))
        cursor = nxt
    return validate_series([by_ts[k] for k in sorted(by_ts)])
