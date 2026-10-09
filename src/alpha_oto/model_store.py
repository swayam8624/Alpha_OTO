"""Immutable provenance for locally trained models; no paid service required."""
from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from .data import Bar, validate_series
from .ml import LogisticModel, train_logistic, training_samples
from .strategies import feature_vector

MODEL_FORMAT="alpha_oto_local_ml_v1"


def build_artifact(bars: list[Bar], *, train_fraction: float=.60,
                   total_side_cost_bps: float=25.0) -> dict:
    validate_series(bars)
    if len(bars)<160 or not .5 <= train_fraction <= .75:
        raise ValueError("Need >=160 bars and train fraction between 0.5 and 0.75")
    end=int(len(bars)*train_fraction)
    samples=list(training_samples(bars,begin=20,end=end,cost_bps=total_side_cost_bps))
    model=train_logistic(samples)
    # Digest includes every training bar value to detect accidental retraining
    # or mutated history; no secret material or provider tokens are persisted.
    basis="\n".join(f"{b.timestamp.isoformat()},{b.open},{b.high},{b.low},{b.close},{b.volume}" for b in bars[:end])
    return {"format":MODEL_FORMAT,"model":model.to_dict(),
            "trained_through":bars[end-1].timestamp.isoformat(),
            "trained_candles":end,"train_samples":len(samples),
            "training_data_sha256":sha256(basis.encode()).hexdigest(),
            "target":"next_open_to_following_open_positive_net_round_trip_cost",
            "assumed_side_cost_bps":total_side_cost_bps,
            "status":"UNCALIBRATED_RESEARCH_MODEL_NOT_LIVE_APPROVED"}


def save_artifact(path: str | Path, artifact: dict) -> None:
    dest=Path(path)
    dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists():
        raise FileExistsError("Model artifacts are immutable: choose a new output filename")
    dest.write_text(json.dumps(artifact,sort_keys=True,indent=2)+"\n",encoding="utf-8")


def load_artifact(path: str | Path) -> dict:
    content=json.loads(Path(path).read_text(encoding="utf-8"))
    if content.get("format")!=MODEL_FORMAT:
        raise ValueError("Unsupported or missing model format")
    LogisticModel.from_dict(content["model"])
    return content


def score_unseen(bars: list[Bar], artifact: dict) -> dict:
    validate_series(bars)
    if len(bars)<21:
        raise ValueError("Not enough bars for features")
    t=datetime.fromisoformat(artifact["trained_through"])
    if t.tzinfo is None or bars[-1].timestamp <= t:
        raise ValueError("Refusing to describe training-period bars as unseen")
    model=LogisticModel.from_dict(artifact["model"])
    p=model.predict(feature_vector(bars))
    return {"as_of":bars[-1].timestamp.isoformat(),"symbol":bars[-1].symbol,
            "uncalibrated_direction_score":round(p,8),
            "status":"UNVALIDATED_RESEARCH_SCORE_NOT_AN_EXECUTABLE_PROBABILITY",
            "trade_authorized":False}
