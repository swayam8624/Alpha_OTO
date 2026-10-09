"""Three-way chronological agent tournament. No live capital promotion."""
from __future__ import annotations
from dataclasses import dataclass
from .backtest import ExecutionSettings, Result, simulate
from .data import Bar
from .ml import MLAgent, train_logistic, training_samples
from .strategies import Breakout, MeanReversion, Trend


@dataclass(frozen=True)
class Tournament:
    train_end: int
    validation_end: int
    selected: str | None
    validation: tuple[dict, ...]
    untouched_test: dict | None
    benchmark_test: dict
    status: str = "RESEARCH_ONLY_NOT_LIVE_APPROVED"

    def to_dict(self) -> dict:
        return {"train_end": self.train_end, "validation_end": self.validation_end,
                "selected": self.selected, "validation": list(self.validation),
                "untouched_test": self.untouched_test, "benchmark_test": self.benchmark_test,
                "status": self.status}


@dataclass(frozen=True)
class AlwaysLong:
    name: str = "buy_and_hold_benchmark"
    def decide(self, history) -> bool:
        return True


def run_tournament(bars: list[Bar], config: ExecutionSettings = ExecutionSettings()) -> Tournament:
    """Select with validation ONLY; query test partition only AFTER selection.

    Validation outcome is optimistically selected across candidates. Test is an
    untouched diagnostic, not a claim of out-of-sample significance. Users must
    collect new forward observations to establish replicability.
    """
    n = len(bars)
    if n < 160:
        raise ValueError("At least 160 time-ordered bars needed for tournament")
    train_end, val_end = int(n*0.6), int(n*0.8)
    samples = list(training_samples(bars, begin=20, end=train_end,
                                    cost_bps=config.side_fee_bps + config.side_slippage_bps))
    trained = train_logistic(samples)
    candidates = [Trend(5,20,"trend_5_20"), Trend(10,30,"trend_10_30"),
                  MeanReversion(20,-1.25,"revert_20"), Breakout(20,"breakout_20"),
                  MLAgent(trained,0.55,"trained_local_logistic")]
    scored = []
    for a in candidates:
        result = simulate(bars,a,config,evaluation_begin=train_end,evaluation_end=val_end)
        # Penalize drawdown and complexity; require at least two closed trades.
        score = result.net_return - 1.5*result.max_drawdown if result.round_trips >= 2 else -1e9
        scored.append((score,a,result))
    scored.sort(key=lambda x:x[0], reverse=True)
    # Eligible only when validation performance is positive under costs.
    score, winner, best = scored[0]
    eligible = winner if score > 0 else None
    final_result = simulate(bars,eligible,config,evaluation_begin=val_end) if eligible else None
    benchmark = simulate(bars,AlwaysLong(),config,evaluation_begin=val_end)
    validation = tuple({"agent":a.name,"score":None if sc == -1e9 else round(sc,7),
                        **r.summary()} for sc,a,r in scored)
    return Tournament(train_end, val_end, eligible.name if eligible else None,
                      validation, final_result.summary() if final_result else None,
                      benchmark.summary())
