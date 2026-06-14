"""devbot_eval —— devbot PR 评审的评测 harness。

对外暴露最常用的入口:样本加载(load_suite/load_all_suites)、评测执行
(run/compare)、metric 注册表(MetricRegistry/get_default_metrics)以及评审领域
类型(PRReviewInput/PRReviewOutput/RiskLevel/Finding/GroundTruth...)。

这些模块均不在导入期拉起重依赖(metric / 真 LLM judge 都是惰性导入),因此可在
顶层安全 re-export,不会成环。
"""
from __future__ import annotations

from devbot_eval.domain import (
    CriticResult,
    Finding,
    GroundTruth,
    PRReviewInput,
    PRReviewOutput,
    RiskLevel,
    cohen_kappa,
)
from devbot_eval.registry import MetricRegistry, get_default_metrics
from devbot_eval.runner import compare, run
from devbot_eval.sample import EvalSample, load_all_suites, load_suite

__all__ = [
    # 样本
    "EvalSample",
    "load_suite",
    "load_all_suites",
    # 执行
    "run",
    "compare",
    # metric 注册
    "MetricRegistry",
    "get_default_metrics",
    # 评审领域类型
    "PRReviewInput",
    "PRReviewOutput",
    "RiskLevel",
    "Finding",
    "CriticResult",
    "GroundTruth",
    "cohen_kappa",
]
