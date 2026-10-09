"""Locally trained logistic classifier using only Python's standard library.

This baseline is deliberately interpretable: no paid inference, GPU or cloud
service. Prediction is NOT calibrated as a real-world trade probability.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import exp
from .data import Bar
from .strategies import feature_vector


def training_samples(bars: list[Bar], *, begin: int, end: int, cost_bps: float = 0.0):
    """Training sample at t predicts open[t+2] / open[t+1], minus round-trip cost.

    `end` is EXCLUSIVE for the index of the last known candle. Samples include
    only targets whose final observation index t+2 is < end. This is an explicit
    embargo against labels leaking from validation/future partitions.
    """
    if begin < 20 or end > len(bars) or begin >= end or cost_bps < 0:
        raise ValueError("Invalid training interval/cost")
    for t in range(begin, end - 2):
        x = feature_vector(bars[:t+1])
        realized = bars[t+2].open/bars[t+1].open - 1 - 2*cost_bps/10000
        yield x, int(realized > 0)


@dataclass(frozen=True)
class LogisticModel:
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    weights: tuple[float, ...]
    bias: float

    def predict(self, features: tuple[float, ...]) -> float:
        if len(features) != len(self.weights):
            raise ValueError("Feature dimension mismatch")
        z = self.bias + sum(w*max(-8,min(8,(x-m)/s)) for w,x,m,s in zip(self.weights,features,self.mean,self.scale))
        return 1/(1+exp(-max(-40, min(40, z))))

    def to_dict(self) -> dict:
        return {"mean": list(self.mean), "scale": list(self.scale),
                "weights": list(self.weights), "bias": self.bias, "type": "local_logistic_v1"}

    @classmethod
    def from_dict(cls, obj: dict) -> "LogisticModel":
        if obj.get("type") != "local_logistic_v1":
            raise ValueError("Unsupported model version")
        return cls(tuple(map(float, obj["mean"])), tuple(map(float, obj["scale"])),
                   tuple(map(float, obj["weights"])), float(obj["bias"]))


def train_logistic(samples, *, epochs: int = 150, learning_rate: float = 0.05,
                   regularization: float = 0.005) -> LogisticModel:
    """Deterministic batch-gradient descent with train-only normalization."""
    samples = list(samples)
    if len(samples) < 30:
        raise ValueError("At least 30 training observations required")
    dim = len(samples[0][0])
    if any(len(x) != dim for x,_ in samples):
        raise ValueError("Inconsistent feature sizes")
    means = tuple(sum(x[j] for x,_ in samples)/len(samples) for j in range(dim))
    scales = tuple(max(1e-9, (sum((x[j]-means[j])**2 for x,_ in samples)/len(samples))**0.5) for j in range(dim))
    standardized = [(tuple(max(-8, min(8, (x[j]-means[j])/scales[j])) for j in range(dim)), label)
                    for x, label in samples]
    weights = [0.0]*dim
    bias = 0.0
    for _ in range(epochs):
        gradient = [regularization*w for w in weights]
        grad_bias = 0.0
        for x,y in standardized:
            z = bias + sum(w*v for w,v in zip(weights,x))
            pred = 1/(1+exp(-max(-40, min(40, z))))
            delta = (pred-y)/len(standardized)
            for j in range(dim):
                gradient[j] += delta*x[j]
            grad_bias += delta
        for j in range(dim):
            weights[j] -= learning_rate*gradient[j]
        bias -= learning_rate*grad_bias
    return LogisticModel(means, scales, tuple(weights), bias)


@dataclass(frozen=True)
class MLAgent:
    model: LogisticModel
    threshold: float = 0.55
    name: str = "local_ml_logistic"

    def decide(self, history) -> bool:
        return len(history) >= 21 and self.model.predict(feature_vector(history)) >= self.threshold
