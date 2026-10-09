"""Long/cash strategies, acting only on the history passed to them."""
from __future__ import annotations
from dataclasses import dataclass
from math import sqrt
from typing import Protocol, Sequence
from .data import Bar


class Strategy(Protocol):
    name: str
    def decide(self, history: Sequence[Bar]) -> bool: ...


@dataclass(frozen=True)
class Trend:
    fast: int = 10
    slow: int = 30
    name: str = "trend"

    def decide(self, history: Sequence[Bar]) -> bool:
        if self.fast < 2 or self.slow <= self.fast or len(history) < self.slow:
            return False
        closes = [b.close for b in history[-self.slow:]]
        return sum(closes[-self.fast:]) / self.fast > sum(closes) / self.slow


@dataclass(frozen=True)
class MeanReversion:
    lookback: int = 20
    entry_z: float = -1.25
    name: str = "mean_reversion"

    def decide(self, history: Sequence[Bar]) -> bool:
        if self.lookback < 3 or len(history) < self.lookback:
            return False
        closes = [b.close for b in history[-self.lookback:]]
        mu = sum(closes) / len(closes)
        sigma = sqrt(sum((v-mu)**2 for v in closes) / len(closes))
        return sigma > 0 and (closes[-1] - mu) / sigma < self.entry_z


@dataclass(frozen=True)
class Breakout:
    lookback: int = 20
    name: str = "breakout"

    def decide(self, history: Sequence[Bar]) -> bool:
        if self.lookback < 2 or len(history) < self.lookback + 1:
            return False
        return history[-1].close > max(b.high for b in history[-self.lookback-1:-1])


def feature_vector(history: Sequence[Bar]) -> tuple[float, ...]:
    """All features available at most recent COMPLETED candle close."""
    if len(history) < 21:
        raise ValueError("At least 21 completed bars are required")
    c = [b.close for b in history[-21:]]
    ret = [(c[i]/c[i-1] - 1) for i in range(1, len(c))]
    mu = sum(ret)/len(ret)
    vol = sqrt(sum((r-mu)**2 for r in ret)/len(ret))
    vols = [b.volume for b in history[-21:]]
    vavg = sum(vols[:-1])/20
    return (c[-1]/c[-2]-1, c[-1]/c[-6]-1, c[-1]/c[-21]-1,
            sum(c[-5:])/5 / (sum(c)/21) - 1, vol,
            min(vols[-1]/vavg, 100.0) if vavg else 0.0)
