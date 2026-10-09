"""Local-first, *research-only* chronological ML training and evaluation.

No brokers, order endpoints, cloud inference, unreviewed model promotion, or
future-derived inputs. Every label is embargoed at train/evaluation boundaries.
Missing OHLCV intervals invalidate features/labels spanning the gap; they are
not silently padded, forward-filled, or treated as ordinary market inactivity.

The OHLCV simulator is purposefully simplified and does not prove fillability.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import log, sqrt, isfinite
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

from .data import Bar, read_csv, validate_series
from .audit import audit_bars

FEATURE_NAMES = (
    "return_1", "return_2", "return_4", "return_8", "return_24",
    "return_48", "volatility_6", "volatility_24", "volatility_48",
    "candle_body", "candle_range", "close_in_range", "atr_fraction_14",
    "volume_log_ratio", "volume_change", "rsi_14", "ma_ratio_8_24",
    "ma_ratio_24_48", "breakout_24", "distance_from_high_48",
    "distance_from_low_48", "trend_consistency_24", "drawdown_48",
)
LOOKBACK = 49  # 49 candles -> 48 completed close-to-close returns


@dataclass(frozen=True)
class Observation:
    # An observation can be used for inference only after decision candle closes.
    decision_idx: int
    entry_idx: int
    exit_idx: int
    x: tuple[float, ...]
    target: int
    net_return: float


def _mean(xs) -> float:
    return sum(xs) / len(xs)


def feature_vector_at(bars: list[Bar], index: int) -> tuple[float, ...]:
    """Use bars[index-48:index+1], never looking at following candles."""
    if index < LOOKBACK - 1 or index >= len(bars):
        raise ValueError("Not enough history for features")
    recent = bars[index - LOOKBACK + 1:index + 1]
    c = [b.close for b in recent]
    r = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
    vol = lambda n: sqrt(_mean([(v - _mean(r[-n:]))**2 for v in r[-n:]]))
    latest = recent[-1]
    width = max(latest.high - latest.low, 1e-12)
    ranges = [(b.high - b.low) / b.close for b in recent[-14:]]
    prior_volume = _mean([b.volume for b in recent[-25:-1]])
    ups = sum(max(0., v) for v in r[-14:])
    downs = sum(max(0., -v) for v in r[-14:])
    rsi = 50. if ups + downs == 0 else 100 * ups / (ups + downs)
    hi48, lo48 = max(b.high for b in recent), min(b.low for b in recent)
    hi24 = max(b.high for b in recent[-25:-1])
    positive_days = sum(v > 0 for v in r[-24:]) / 24
    feats = (
        *(c[-1] / c[-1-n] - 1 for n in (1, 2, 4, 8, 24, 48)),
        vol(6), vol(24), vol(48),
        latest.close / latest.open - 1,
        (latest.high - latest.low) / latest.close,
        (latest.close - latest.low) / width,
        _mean(ranges),
        log1p_safe(latest.volume) - log1p_safe(prior_volume),
        log1p_safe(latest.volume) - log1p_safe(recent[-2].volume),
        rsi / 100,
        _mean(c[-8:]) / _mean(c[-24:]) - 1,
        _mean(c[-24:]) / _mean(c[-48:]) - 1,
        latest.close / hi24 - 1,
        latest.close / hi48 - 1,
        latest.close / lo48 - 1,
        positive_days,
        latest.close / max(c) - 1,
    )
    if len(feats) != len(FEATURE_NAMES) or not all(isfinite(x) for x in feats):
        raise ValueError("Invalid feature row")
    return tuple(float(x) for x in feats)


def log1p_safe(x: float) -> float:
    from math import log1p
    return log1p(max(0., x))


def _contiguous(bars: list[Bar], from_idx: int, to_idx: int,
                interval_seconds: int) -> bool:
    """Indices inclusive; elapsed-time check also catches misaligned candles."""
    if to_idx >= len(bars) or from_idx < 0 or to_idx < from_idx:
        return False
    first = bars[from_idx].timestamp
    last = bars[to_idx].timestamp
    if int((last - first).total_seconds()) != (to_idx - from_idx) * interval_seconds:
        return False
    return all(int((bars[i].timestamp - bars[i - 1].timestamp).total_seconds()) == interval_seconds
               for i in range(from_idx + 1, to_idx + 1))


def make_observations(bars: list[Bar], *, horizon: int,
                      interval_seconds: int, side_cost_bps: float) -> tuple[list[Observation], dict]:
    validate_series(bars)
    if horizon < 1 or horizon > 168 or interval_seconds <= 0 or side_cost_bps < 0 or side_cost_bps >= 5000:
        raise ValueError("Invalid horizon, interval or side cost")
    cost = side_cost_bps / 10_000.
    observations = []
    skipped = 0
    # OHLCV timestamps represent beginning of their respective bars.
    # At t's close a signal is evaluated; theoretical next-open fill is t+1.
    for t in range(LOOKBACK - 1, len(bars) - horizon - 1):
        entry = t + 1
        exit_ = entry + horizon
        # Every feature AND its entire outcome window must be contiguous.
        if not _contiguous(bars, t - LOOKBACK + 1, exit_, interval_seconds):
            skipped += 1
            continue
        x = feature_vector_at(bars, t)
        net = bars[exit_].open * (1 - cost) / (bars[entry].open * (1 + cost)) - 1
        observations.append(Observation(t, entry, exit_, x, int(net > 0), net))
    return observations, {"valid_observations": len(observations),
                          "excluded_for_missing_intervals": skipped,
                          "feature_lookback": LOOKBACK,
                          "feature_count": len(FEATURE_NAMES)}


def build_estimator(name: str, *, seed: int = 42):
    """Dependencies are imported only for research commands, not the core CLI."""
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    if name == "logistic":
        return Pipeline([("scale", StandardScaler()),
                         ("model", LogisticRegression(C=.25, max_iter=500, random_state=seed))])
    if name == "histgb":
        return HistGradientBoostingClassifier(max_iter=90, max_leaf_nodes=9,
                                              learning_rate=.05, l2_regularization=10.,
                                              min_samples_leaf=40, random_state=seed)
    if name == "forest":
        return RandomForestClassifier(n_estimators=120, max_depth=5,
                                      min_samples_leaf=30, random_state=seed,
                                      n_jobs=2)
    if name == "lightgbm":
        from lightgbm import LGBMClassifier
        return LGBMClassifier(n_estimators=100, max_depth=5, num_leaves=11,
                              learning_rate=.04, min_child_samples=40,
                              reg_lambda=10, random_state=seed,
                              n_jobs=2, verbosity=-1)
    if name == "xgboost":
        from xgboost import XGBClassifier
        return XGBClassifier(n_estimators=100, max_depth=3, learning_rate=.05,
                             reg_lambda=10, subsample=.85, colsample_bytree=.9,
                             tree_method="hist", random_state=seed, n_jobs=2)
    raise ValueError(f"Unknown model {name!r}")


def train_one(name: str, train: list[Observation]):
    if len(train) < 120:
        raise ValueError("Insufficient training observations")
    unique = set(r.target for r in train)
    if len(unique) < 2:
        raise ValueError("Training must contain both outcomes")
    model = build_estimator(name)
    model.fit([r.x for r in train], [r.target for r in train])
    return model


def score_probs(model, observations: list[Observation]) -> list[float]:
    if not observations:
        return []
    # Always select the actual P(target==1) column; no ordering assumptions.
    class_1_col = list(model.classes_).index(1)
    return [float(x[class_1_col]) for x in model.predict_proba([r.x for r in observations])]


def evaluate_signals(bars: list[Bar], rows: list[Observation],
                     probs: list[float], *, threshold: float, position_fraction: float = .25) -> dict:
    """Non-overlapping long-only horizon trades, next-open fills, modeled costs.

    Metrics are evaluated on marked wealth at selected execution points, NOT a
    full broker simulator. There is no liquidity, spread/queue, market impact,
    financing, capital-tax, stop-loss or mark-to-market intratrade stress model.
    Drawdown here is realized-equity drawdown, and can understate actual risk.
    """
    if len(rows) != len(probs) or not 0 < position_fraction <= 1 or not 0 <= threshold <= 1:
        raise ValueError("Invalid signals or risk fraction")
    wealth = 1.
    peak = 1.
    max_dd = 0.
    trade_returns = []
    last_exit = -1
    for row, p in zip(rows, probs):
        if not 0 <= p <= 1 or not isfinite(p):
            raise ValueError("Invalid probability")
        if p < threshold or row.entry_idx < last_exit:
            continue
        realized = position_fraction * row.net_return
        wealth *= (1 + realized)
        peak = max(peak, wealth)
        max_dd = max(max_dd, 1 - wealth / peak)
        last_exit = row.exit_idx
        trade_returns.append(realized)
    gross_total = wealth - 1
    return {"net_return": float(gross_total), "realized_drawdown_lower_bound": float(max_dd),
            "round_trips": len(trade_returns),
            "win_fraction": sum(x > 0 for x in trade_returns) / len(trade_returns) if trade_returns else None,
            "compounded_strategy_return": gross_total,
            "risk_note": "Simplified non-overlapping next-open execution; drawdown does not include intratrade excursions"}


def buyhold_reference(bars: list[Bar], start: int, end: int, side_cost_bps: float,
                      position_fraction: float = .25) -> dict:
    if start < 0 or end >= len(bars) or end <= start:
        raise ValueError("Invalid benchmark boundaries")
    cost = side_cost_bps / 10000.
    ret = bars[end].open * (1-cost) / (bars[start].open * (1+cost)) - 1
    return {"net_return": position_fraction * ret,
            "note": "Long-and-hold fractionally allocated benchmark, including side costs"}


def _folds(bars_len: int):
    # Expanding train ranges: 0-44%, 0-56%, 0-68%; disjoint val windows
    return [(int(.44*bars_len), int(.56*bars_len)),
            (int(.56*bars_len), int(.68*bars_len)),
            (int(.68*bars_len), int(.80*bars_len))]


def _extract(rows: list[Observation], start: int, end: int, *, training: bool) -> list[Observation]:
    # Training purges samples whose LABEL EXIT overlaps the next split.
    if training:
        return [r for r in rows if r.exit_idx < end]
    return [r for r in rows if r.decision_idx >= start and r.exit_idx < end]


def research(csv_path: str | Path, output_directory: str | Path,
             *, horizons: tuple[int,...] = (1,4,12),
             models: tuple[str,...] = ("logistic","histgb","forest"),
             thresholds: tuple[float,...] = (.55,.60),
             interval_seconds: int = 3600, side_cost_bps: float = 25.,
             min_round_trips: int = 8, position_fraction: float = .25,
             seed: int = 42) -> dict:
    """Evaluate folds for selection; final 20% evaluated only after selection.

    No favorable strategy is guaranteed. A research artifact is saved even
    when the promotion gate rejects every candidate. Models are saved under
    an explicit NOT_LIVE_APPROVED flag and cannot send orders.
    """
    if seed != 42:
        raise ValueError("Only deterministic seed 42 currently supported")
    if not horizons or not models or len(set(horizons)) != len(horizons) or any(not 0 < x < 1 for x in thresholds):
        raise ValueError("Bad research configuration")
    if not 0 < position_fraction <= 1 or min_round_trips < 1:
        raise ValueError("Invalid risk parameters")
    bars = read_csv(csv_path)
    if len(bars) < 500:
        raise ValueError("At least 500 candles required for chronological research")
    audit = audit_bars(bars, interval_seconds=interval_seconds, continuous=True)
    blocked = set(audit["issues"]) - {"GAPS_IN_CONTINUOUS_MARKET"}
    if blocked:
        raise ValueError(f"Unsafe dataset; audit blocked: {sorted(blocked)}")
    from sklearn import __version__ as sklearn_version
    import joblib
    data_hash = sha256(Path(csv_path).read_bytes()).hexdigest()
    out = Path(output_directory)
    out.mkdir(parents=True,exist_ok=True)
    test_start = int(.8 * len(bars))
    folds = _folds(len(bars))
    all_results: list[dict] = []
    best_configuration = None
    best_score = float('-inf')
    by_horizon = {}
    for horizon in horizons:
        if horizon < 1 or horizon > 168:
            raise ValueError("Horizon out of bounds")
        rows, info = make_observations(bars,horizon=horizon,
                                       interval_seconds=interval_seconds,
                                       side_cost_bps=side_cost_bps)
        by_horizon[str(horizon)] = info
        for name in models:
            # Prepare the fold prediction ONCE, score different thresholds on the
            # same out-of-training results. No holdout labels read during selection.
            fold_predictions = []
            for train_end, val_end in folds:
                training = _extract(rows, 0, train_end, training=True)
                validation = _extract(rows, train_end, val_end, training=False)
                if len(training) < 120 or len(validation) < 20:
                    raise ValueError("Insufficient observations after gap/label embargo")
                estimator = train_one(name,training)
                fold_predictions.append((validation, score_probs(estimator,validation),
                                         train_end,val_end))
            for threshold in thresholds:
                performances = []
                for val, probs, train_end, val_end in fold_predictions:
                    res = evaluate_signals(bars,val,probs,threshold=threshold,
                                           position_fraction=position_fraction)
                    res["benchmark"] = buyhold_reference(
                        bars,train_end+1,val_end-1,side_cost_bps,position_fraction)
                    res["train_end_index"] = train_end
                    res["validation_end_index"] = val_end
                    performances.append(res)
                total_trades = sum(x["round_trips"] for x in performances)
                # Compounded independently per evaluation fold with equal starting
                # funds, and penalized for realized-only drawdown.
                growth = 1.
                for x in performances:
                    growth *= (1 + x["net_return"])
                net_growth = growth - 1
                mean_drawdown = mean(x["realized_drawdown_lower_bound"] for x in performances)
                valid_folds = sum(x["net_return"] > 0 for x in performances)
                score = net_growth - 1.5 * mean_drawdown
                eligible = (total_trades >= min_round_trips and net_growth > 0
                            and valid_folds >= 2 and score > 0)
                summary = {"model":name,"horizon_bars":horizon,
                           "probability_threshold":threshold, "score":round(score,8),
                           "compounded_validation_return":round(net_growth,8),
                           "validation_positive_folds":valid_folds,
                           "validation_trade_count":total_trades,
                           "eligible_for_further_shadow_research":eligible,
                           "folds": performances}
                all_results.append(summary)
                if score > best_score:
                    best_score = score
                    best_configuration = summary
    if best_configuration is None:
        raise ValueError("No configurations were evaluated")
    selected = best_configuration
    h = selected["horizon_bars"]
    rows, _ = make_observations(bars,horizon=h,interval_seconds=interval_seconds,
                               side_cost_bps=side_cost_bps)
    # Retrain from scratch, using ONLY observations whose outcomes are known
    # before the first holdout decision. No holdout fitting/calibration.
    final_training = _extract(rows,0,test_start,training=True)
    model = train_one(selected["model"],final_training)
    holdout = _extract(rows,test_start,len(bars),training=False)
    if len(holdout) < 25:
        raise ValueError("Insufficient clean held-out observations")
    final_probs = score_probs(model,holdout)
    test_report = evaluate_signals(bars,holdout,final_probs,
                                   threshold=selected["probability_threshold"],
                                   position_fraction=position_fraction)
    test_report["benchmark"] = buyhold_reference(bars,test_start+1,len(bars)-1,
                                                  side_cost_bps,position_fraction)
    test_report["count_scored"] = len(holdout)
    # Classification diagnostics; these are *not* probabilities of profit.
    from sklearn.metrics import brier_score_loss, roc_auc_score, accuracy_score
    y = [r.target for r in holdout]
    test_report["brier_score"] = float(brier_score_loss(y,final_probs))
    test_report["accuracy_at_half"] = float(accuracy_score(y,[int(p>=.5) for p in final_probs]))
    test_report["auc"] = float(roc_auc_score(y,final_probs)) if len(set(y))>1 else None
    import sklearn
    model_path = out/"research_model.joblib"
    joblib.dump(model,model_path)
    metadata = {
        "status":"RESEARCH_ONLY_NOT_LIVE_APPROVED",
        "never_interpret_as_order_signal":True,
        "model_file": model_path.name,
        "model_file_sha256":sha256(model_path.read_bytes()).hexdigest(),
        "training_data_sha256":data_hash,
        "symbol":bars[0].symbol,"trained_through_bar":bars[final_training[-1].exit_idx].timestamp.isoformat(),
        "train_label_ends_before_test": final_training[-1].exit_idx < test_start,
        "test_started":bars[test_start].timestamp.isoformat(),
        "already_evaluated_through_bar":bars[-1].timestamp.isoformat(),
        "features":list(FEATURE_NAMES),
        "horizon_bars":h,"interval_seconds":interval_seconds,
        "side_cost_bps":side_cost_bps,
        "probability_threshold":selected["probability_threshold"],
        "model":selected["model"],
        "scikit_learn_version":sklearn_version,
        "warning":"joblib uses pickle. Load only artifacts you personally generated and trust."
    }
    (out/"model_manifest.json").write_text(json.dumps(metadata,indent=2)+"\n")
    report = {
        "status":"RESEARCH_ONLY_NOT_LIVE_APPROVED",
        "data_file":str(csv_path),"data_sha256":data_hash,
        "bar_count":len(bars),"symbol":bars[0].symbol,
        "interval_seconds":interval_seconds,
        "side_cost_bps":side_cost_bps,
        "folds":folds,
        "test_start_index":test_start,
        "feature_name_count":len(FEATURE_NAMES),
        "data_quality_by_horizon":by_horizon,
        "source_audit":audit,
        "selected_research_configuration": {k:v for k,v in selected.items() if k!="folds"},
        "shadow_candidate_only": bool(selected["eligible_for_further_shadow_research"]),
        "holdout":test_report,
        "candidate_validation": sorted(all_results,key=lambda x:x["score"],reverse=True),
        "interpretation":"One final holdout is a diagnostic; repeated tuning on the same holdout invalidates its untouched status. No strategy is live-approved.",
        "limitations":["No executable quote data","No taxes or crypto VDA loss-setoff accounting",
                       "No exchange outages, liquidity queues or order book","Drawdown is realized at exits only",
                       "No automatic gap filling", "Model probability is not a calibrated chance of a profitable trade"],
    }
    (out/"research_report.json").write_text(json.dumps(report,indent=2,allow_nan=False)+"\n")
    return report


def score_saved_model(csv_path: str|Path, model_directory: str|Path) -> dict:
    """Read-only inference for a brand-new completed OHLCV bar; no order route.

    Refuse an already-seen observation to prevent presenting in-sample outputs
    as a forward prediction. Remember training is NOT live approved.
    """
    import joblib
    folder = Path(model_directory)
    metadata = json.loads((folder/"model_manifest.json").read_text())
    model_path = folder / metadata["model_file"]
    if sha256(model_path.read_bytes()).hexdigest() != metadata["model_file_sha256"]:
        raise ValueError("Local model checksum mismatch")
    if tuple(metadata["features"]) != FEATURE_NAMES:
        raise ValueError("Feature schema mismatch")
    bars = read_csv(csv_path)
    if bars[0].symbol != metadata["symbol"]:
        raise ValueError("Symbol mismatch")
    idx = len(bars)-1
    interval_seconds = int(metadata["interval_seconds"])
    if not _contiguous(bars,idx-LOOKBACK+1,idx,interval_seconds):
        raise ValueError("Recent OHLCV window has missing intervals")
    last = bars[-1].timestamp
    if last.isoformat() <= metadata["already_evaluated_through_bar"]:
        raise ValueError("Bar already included in training/holdout research; fetch fresh data before forward inference")
    if last + timedelta(seconds=interval_seconds) > datetime.now(timezone.utc):
        raise ValueError("Latest bar is unfinished")
    # Loading pickle-family files is only safe for locally generated trusted data.
    estimator = joblib.load(model_path)
    probability = score_probs(estimator,[Observation(idx,idx,idx,
                           feature_vector_at(bars,idx),0,0.)])[0]
    return {"symbol":bars[0].symbol,"latest_completed_bar":last.isoformat(),
            "estimated_p_positive_after_modeled_cost":probability,
            "research_threshold":metadata["probability_threshold"],
            "horizon_bars":metadata["horizon_bars"],
            "status":"RESEARCH_ONLY_UNCALIBRATED_NOT_TRADE_AUTHORIZATION"}
