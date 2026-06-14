"""Metric 协议 + 结果类型。每个 metric 拿 (samples 含 ground_truth, predictions) 算一个分。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from devbot_eval.domain import PRReviewOutput
from devbot_eval.sample import EvalSample


@dataclass
class MetricResult:
    name: str
    value: float
    breakdown: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)


class Metric(Protocol):
    name: str

    def evaluate(self, samples: list[EvalSample],
                 predictions: list[PRReviewOutput]) -> MetricResult: ...
