"""Deterministic fabricated candles for software smoke tests, NEVER performance evidence."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from random import Random
from .data import Bar


def synthetic_bars(length: int = 480, *, symbol: str = "SYNTHETIC-USD", seed: int = 12042) -> list[Bar]:
    if length < 160:
        raise ValueError("Need at least 160 artificial candles for a tournament")
    rng = Random(seed)
    stamp = datetime(2025,1,1,tzinfo=timezone.utc)
    price=100.0
    bars=[]
    for t in range(length):
        open_price = price
        direction=(.0014 if t % 100 < 40 else -.0011 if t % 100 < 67 else 0)
        price=max(.1, price*(1+direction+rng.uniform(-.015,.015)))
        bars.append(Bar(stamp+timedelta(hours=t),symbol,open_price,
                        max(price,open_price)*1.0008,
                        min(price,open_price)*.9992,
                        price,1000+rng.random()*500))
    return bars
