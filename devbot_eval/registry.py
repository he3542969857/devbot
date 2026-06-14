"""Metric 注册表 —— 把 7 个默认 metric 实例汇成一处,供 runner / cli 取用。

简历"七个指标"对应这里的 7 个 metric 实例(注意不要与手册"7 层架构"混淆):
finding_f1 / risk_accuracy / ece / llm_judge / cost_latency / length_degradation /
prompt_injection。导入做成惰性,避免 metric 模块反向依赖本文件时成环。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查期引入,运行时惰性导入避免成环
    from devbot_eval.metrics.base import Metric


def _build_defaults() -> list:
    """实例化 7 个默认 metric。各 metric 类由其它模块写入固定位置,这里按名导入。"""
    from devbot_eval.metrics.hard import (
        FindingF1Metric,
        RiskAccuracyMetric,
        CostLatencyMetric,
        LengthDegradationMetric,
        PromptInjectionMetric,
    )
    from devbot_eval.metrics.calibration import EceMetric
    from devbot_eval.metrics.llm_judge import LlmJudgeMetric

    return [
        FindingF1Metric(),
        RiskAccuracyMetric(),
        EceMetric(),
        LlmJudgeMetric(),
        CostLatencyMetric(),
        LengthDegradationMetric(),
        PromptInjectionMetric(),
    ]


class MetricRegistry:
    """默认 metric 集合的容器。

    DEFAULTS 是惰性属性(首次访问才实例化),避免在模块导入时就拉起所有
    metric 依赖(含可能很重的 llm_judge)。``names`` / ``get`` 便于 cli 选取。
    """

    _cache: list | None = None

    @property
    def DEFAULTS(self) -> list:  # noqa: N802 —— 契约要求这个名字
        cls = type(self)
        if cls._cache is None:
            cls._cache = _build_defaults()
        return cls._cache

    def names(self) -> list[str]:
        return [m.name for m in self.DEFAULTS]

    def get(self, name: str):
        for m in self.DEFAULTS:
            if m.name == name:
                return m
        raise KeyError(f"unknown metric: {name!r}")

    def __iter__(self):
        return iter(self.DEFAULTS)


def get_default_metrics() -> list:
    """返回一份全新的 7 个默认 metric 实例(每次新建,互不共享状态)。"""
    return _build_defaults()
