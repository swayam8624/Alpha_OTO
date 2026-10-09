"""Auditable, deterministic long/cash quantitative signal families.

All methods observe *completed* OHLCV bars only. All scores are uncalibrated
relative ranking signals, NOT profit predictions. They do not place orders.
Research uses one market calendar per experimental cohort (24/7 crypto for v0.4).
"""
from __future__ import annotations
from dataclasses import dataclass
from math import log, sqrt, isfinite
from statistics import mean, pstdev
from typing import Mapping, Sequence
from .data import Bar


def _closes(bars: Sequence[Bar], n: int) -> list[float]:
    return [b.close for b in bars[-n:]]


def log_returns(bars: Sequence[Bar], n: int) -> list[float]:
    if len(bars) < n + 1:
        raise ValueError('Not enough completed candles')
    c = _closes(bars, n + 1)
    return [log(c[i]/c[i-1]) for i in range(1, len(c))]


def realized_vol(bars: Sequence[Bar], n: int = 48) -> float:
    return max(pstdev(log_returns(bars, n)), 1e-6)


def _zscore(vals: list[float]) -> float:
    sigma = pstdev(vals)
    return 0.0 if sigma < 1e-12 else (vals[-1] - mean(vals))/sigma


@dataclass(frozen=True)
class QuantAgent:
    """No arbitrary source code execution; predeclared parameterized agents."""
    kind: str
    lookback: int = 48
    slow: int = 168

    def __post_init__(self):
        if self.kind not in {'trend', 'momentum', 'reversal', 'breakout',
                             'vol_adjusted_momentum', 'defensive'}:
            raise ValueError(f'Unsupported strategy: {self.kind}')
        if self.lookback < 8 or self.slow < self.lookback + 1:
            raise ValueError('Invalid lookback or slow period')

    @property
    def name(self) -> str:
        return f'{self.kind}_fast{self.lookback}_slow{self.slow}'

    def score(self, history: Sequence[Bar]) -> float:
        """Positive: candidate long; zero/negative: remain cash.

        Scores use only history ending at the previous completed candle.
        No shorting, derivatives, leverage, broker access or forecasts assumed.
        """
        if len(history) < self.slow + 1:
            return 0.0
        closes = _closes(history, self.slow + 1)
        fast = self.lookback
        recent = closes[-fast:]
        r = closes[-1] / closes[-1-fast] - 1
        slow_r = closes[-1] / closes[0] - 1
        fast_ma = mean(recent)
        slow_ma = mean(closes[-self.slow:])
        vol = realized_vol(history, fast)
        if self.kind == 'trend':
            return max(0.0, (fast_ma / slow_ma - 1)) if r > 0 else 0.0
        if self.kind == 'momentum':
            return max(0.0, (r + 0.5*slow_r)) if fast_ma > slow_ma else 0.0
        if self.kind == 'vol_adjusted_momentum':
            return max(0.0, r / max(vol*sqrt(fast), 1e-6)) if slow_r > 0 else 0.0
        if self.kind == 'breakout':
            prev_hi = max(b.high for b in history[-fast-1:-1])
            return max(0.0, history[-1].close / prev_hi - 1)
        if self.kind == 'reversal':
            z = _zscore(recent)
            return max(0.0, -z - 1.5) if slow_r > -0.25 else 0.0
        if self.kind == 'defensive':
            # Positive score only when upward regime + relatively subdued vol.
            return max(0.0, r / max(vol*sqrt(fast),1e-6)) if vol < .025 and slow_r > 0 else 0.0
        raise AssertionError('unreachable')


def agents_default() -> tuple[QuantAgent, ...]:
    return tuple(QuantAgent(k, f, s) for k, f, s in [
        ('trend',24,120), ('trend',48,168), ('momentum',24,120),
        ('momentum',48,168), ('vol_adjusted_momentum',48,168),
        ('reversal',24,120), ('breakout',24,120), ('breakout',48,168),
        ('defensive',48,168)])


def allocate_scores(scores: Mapping[str,float], volatilities: Mapping[str,float],
                    *, gross_limit: float = .85, per_asset_limit: float = .45,
                    volatility_target: float = .01, strength_floor: float = 0.0) -> dict[str,float]:
    """Fractional long-only risk allocation, with cash as the unallocated residual.

    Signals and risk budget are separate: signal determines direction/priority,
    inverse volatility determines relative size, and projected independent-risk
    volatility target throttles gross exposure. This is NOT a covariance model.
    """
    if not 0 < gross_limit <= 1 or not 0 < per_asset_limit <= 1 or volatility_target <= 0:
        raise ValueError('Invalid allocation constraints')
    if not isfinite(strength_floor) or strength_floor < 0:
        raise ValueError('Invalid signal threshold')
    items = {}
    for symbol, signal in scores.items():
        v = volatilities.get(symbol)
        if not isfinite(signal) or v is None or not isfinite(v) or v <= 0:
            raise ValueError('Invalid score/volatility')
        if signal > strength_floor:
            items[symbol] = (signal, v)
    if not items:
        return {symbol: 0.0 for symbol in scores}
    # Rank priority is signal strength, bounded so one extreme score cannot
    # completely monopolize selection. Relative risk weights inverse-vol.
    raw = {symbol: max(.1, min(3.0, signal)) / v
           for symbol,(signal,v) in items.items()}
    total = sum(raw.values())
    weights = {s: min(per_asset_limit, gross_limit*raw[s]/total) for s in raw}
    # Sum capped naturally by independent concentration constraints.
    # Conservative portfolio-vol proxy upper-bounds risk using sum(w_i * sigma_i).
    vol_bound = sum(weights[s]*items[s][1] for s in weights)
    if vol_bound > volatility_target:
        factor = volatility_target / vol_bound
        weights = {s:w*factor for s,w in weights.items()}
    return {symbol: weights.get(symbol, 0.0) for symbol in scores}
