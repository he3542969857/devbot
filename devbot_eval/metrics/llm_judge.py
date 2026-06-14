"""基于裁判(Judge)的质量指标 —— relevance/actionability/novelty 三维聚合。

``LlmJudgeMetric`` 拿一个 ``Judge`` 给每条 (sample, prediction) 打三维分(各
0-5),每条的 composite = 三维均值,整体 value = 所有 composite 的均值再除 5,
**归一化到 [0,1]**(便于和其它 metric 同尺度对比)。breakdown 给出三维各自的
均值(仍是 0-5 原尺度,直观)。

裁判通过构造函数注入:CI 默认 ``DeterministicJudge``(可复现、不调 LLM);
``--real`` 时 runner 传入 ``LlmBackedJudge`` 换成真 LLM 评审。
"""
from __future__ import annotations

from devbot_eval.domain import PRReviewOutput
from devbot_eval.judges import DeterministicJudge
from devbot_eval.metrics.base import MetricResult
from devbot_eval.protocols import Judge
from devbot_eval.sample import EvalSample

_DIMS = ("relevance", "actionability", "novelty")


class LlmJudgeMetric:
    """三维裁判指标。value = 平均 composite / 5,落在 [0,1]。"""

    def __init__(self, judge: Judge | None = None):
        self.name = "llm_judge"
        self.judge: Judge = judge or DeterministicJudge()

    def evaluate(self, samples: list[EvalSample],
                 predictions: list[PRReviewOutput]) -> MetricResult:
        n = min(len(samples), len(predictions))
        if n == 0:
            return MetricResult(name=self.name, value=0.0,
                                breakdown={d: 0.0 for d in _DIMS},
                                extras={"n": 0, "judge": getattr(self.judge, "name", type(self.judge).__name__)})

        dim_sums = {d: 0.0 for d in _DIMS}
        composites: list[float] = []
        per_sample: list[dict] = []

        for sample, pred in zip(samples[:n], predictions[:n]):
            scores = self.judge.judge(sample, pred)
            vals = {d: float(scores.get(d, 0.0)) for d in _DIMS}
            for d in _DIMS:
                dim_sums[d] += vals[d]
            composite = sum(vals.values()) / len(_DIMS)
            composites.append(composite)
            per_sample.append({"pr_id": sample.pr_id, "composite": round(composite, 4), **vals})

        dim_means = {d: round(dim_sums[d] / n, 4) for d in _DIMS}
        mean_composite = sum(composites) / n
        value = round(mean_composite / 5.0, 4)  # 归一化到 [0,1]

        return MetricResult(
            name=self.name,
            value=value,
            breakdown=dim_means,
            extras={
                "n": n,
                "mean_composite_0_5": round(mean_composite, 4),
                "judge": getattr(self.judge, "name", type(self.judge).__name__),
                "per_sample": per_sample,
            },
        )
