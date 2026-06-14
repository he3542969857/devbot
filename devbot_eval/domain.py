"""devbot 领域类型 —— review_agent 与 eval harness 共用的契约。

Finding / CriticResult / PRReviewInput / PRReviewOutput / RiskLevel
+ GroundTruth(评测真值)+ cohen_kappa(标注一致性)。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def from_score(cls, score: float) -> "RiskLevel":
        if score >= 80:
            return cls.CRITICAL
        if score >= 60:
            return cls.HIGH
        if score >= 30:
            return cls.MEDIUM
        return cls.LOW


@dataclass
class Finding:
    file: str = ""
    line: Optional[int] = None
    severity: str = "info"          # info | warn | error
    message: str = ""
    critic: str = ""

    def key(self, line_tol: int = 0) -> tuple:
        return (self.file, self.line, self.severity)


@dataclass
class CriticResult:
    critic: str
    risk_score: int = 50
    confidence: float = 0.5
    findings: list[Finding] = field(default_factory=list)
    suggestion: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    error: Optional[str] = None


@dataclass
class PRReviewInput:
    pr_id: str
    diff: str = ""
    impact_files: list[str] = field(default_factory=list)
    title: str = ""
    description: str = ""
    language: str = "python"


@dataclass
class PRReviewOutput:
    pr_id: str
    risk_score: int
    risk_level: RiskLevel
    critics: list[CriticResult] = field(default_factory=list)
    summary: str = ""
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0

    @property
    def findings(self) -> list[Finding]:
        """产品 / metric 读的是各 critic 的扁平 findings(已去重)。"""
        out: list[Finding] = []
        for c in self.critics:
            out.extend(c.findings)
        return out


@dataclass
class GroundTruth:
    """评测真值:一个 PR 的期望风险等级 + 期望 finding 位置。"""
    pr_id: str
    expected_risk_level: RiskLevel
    expected_findings: list[Finding] = field(default_factory=list)
    notes: str = ""


def cohen_kappa(labels_a: list[Any], labels_b: list[Any]) -> float:
    """两标注者一致性 κ(meta-eval 校验 judge / 标注用)。"""
    if not labels_a or len(labels_a) != len(labels_b):
        return 0.0
    n = len(labels_a)
    cats = set(labels_a) | set(labels_b)
    po = sum(1 for x, y in zip(labels_a, labels_b) if x == y) / n
    pe = 0.0
    for c in cats:
        pa = sum(1 for x in labels_a if x == c) / n
        pb = sum(1 for y in labels_b if y == c) / n
        pe += pa * pb
    if pe >= 1.0:
        return 1.0
    return (po - pe) / (1 - pe)
