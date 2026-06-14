"""失败回环 —— 把每个失败的 (sample, prediction) 归到 5 类失败模式。

5 类:
- prompt_issue : prompt 没约束住模型(典型:对抗集注入标记诱导模型偏题)
- data_issue   : 样本本身缺真值 / diff 为空 / 标注残缺,评测无从判对错
- model_error  : 模型输出空 / 乱码 / 全部 critic 报错,产出不可用
- edge_case    : 边界 suite 的样本(空 diff、超长、奇异语言)本就难
- ambiguous    : 真值标注里写了"歧义/有争议",对错本身不确定

用法:classify_failures(samples, predictions, metric_results) -> list[{pr_id, klass, reason}]。
只对"失败"的样本归类(失败 = 风险档位判错,或被 metric breakdown 标红)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from devbot_eval.domain import PRReviewOutput, RiskLevel
from devbot_eval.metrics.base import MetricResult
from devbot_eval.sample import EvalSample

# 5 类失败标签
PROMPT_ISSUE = "prompt_issue"
DATA_ISSUE = "data_issue"
MODEL_ERROR = "model_error"
EDGE_CASE = "edge_case"
AMBIGUOUS = "ambiguous"

FAILURE_CLASSES = [PROMPT_ISSUE, DATA_ISSUE, MODEL_ERROR, EDGE_CASE, AMBIGUOUS]

# 对抗集里常见的注入诱导标记(命中即判 prompt_issue)
_INJECTION_MARKERS = (
    "ignore previous", "ignore all previous", "disregard the above",
    "you are now", "system prompt", "jailbreak", "do not report",
    "mark this as safe", "approve this pr", "override your instructions",
    "as an ai", "respond with risk_score 0",
)

# 真值 notes 里表示歧义的词
_AMBIGUOUS_MARKERS = ("ambiguous", "debatable", "disputed", "subjective",
                      "unclear", "judgment call", "borderline", "歧义", "有争议")


@dataclass
class FailureRecord:
    """单条失败归类结果(等价于返回的 dict,便于程序内传递)。"""
    pr_id: str
    klass: str
    reason: str

    def as_dict(self) -> dict:
        return {"pr_id": self.pr_id, "klass": self.klass, "reason": self.reason}


def _output_is_garbled(pred: PRReviewOutput) -> bool:
    """模型层失败:所有 critic 都报错,或全程零产出(空 summary + 无 critic + 无 finding)。"""
    if pred is None:
        return True
    critics = pred.critics or []
    if critics and all((c.error is not None) for c in critics):
        return True
    if not critics and not pred.findings and not (pred.summary or "").strip():
        return True
    return False


def _has_injection(sample: EvalSample) -> bool:
    blob = ((sample.diff or "") + " " + (sample.title or "")).lower()
    return any(m in blob for m in _INJECTION_MARKERS)


def _is_ambiguous(sample: EvalSample) -> bool:
    gt = sample.ground_truth
    if gt is None:
        return False
    notes = (gt.notes or "").lower()
    return any(m in notes for m in _AMBIGUOUS_MARKERS)


def _data_incomplete(sample: EvalSample) -> bool:
    """数据层失败:diff 空,或没有真值(无从判对错)。"""
    if not (sample.diff or "").strip():
        return True
    if sample.ground_truth is None:
        return True
    return False


def _level_mismatch(sample: EvalSample, pred: PRReviewOutput) -> bool:
    gt = sample.ground_truth
    if gt is None or pred is None:
        return False
    return gt.expected_risk_level != pred.risk_level


def _flagged_by_metrics(pr_id: str, metric_results: Optional[list[MetricResult]]) -> bool:
    """metric 的 breakdown[pr_id] 若给出明确"错"信号(False / 0 / 'fail'),也算失败。"""
    if not metric_results:
        return False
    for mr in metric_results:
        bd = getattr(mr, "breakdown", None) or {}
        if pr_id not in bd:
            continue
        v = bd[pr_id]
        if isinstance(v, bool):
            if v is False:
                return True
        elif isinstance(v, (int, float)):
            if v <= 0.0:
                return True
        elif isinstance(v, str):
            if v.lower() in ("fail", "wrong", "miss", "false"):
                return True
        elif isinstance(v, dict):
            if v.get("correct") is False or v.get("hit") is False:
                return True
    return False


def _classify_one(sample: EvalSample, pred: PRReviewOutput) -> tuple[str, str]:
    """对单个已判定为"失败"的样本套用启发式规则,返回 (klass, reason)。规则有优先级。"""
    # 1. 模型层:输出乱码 / 全 critic 报错 —— 最先排除,不可用产出无从谈别的
    if _output_is_garbled(pred):
        errs = [c.error for c in (pred.critics or []) if c.error]
        if errs:
            return MODEL_ERROR, f"all critics errored: {errs[0][:80]}"
        return MODEL_ERROR, "empty/garbled model output (no critics, no findings)"

    # 2. 真值歧义:标注本身说有争议 —— 错也不能怪系统
    if _is_ambiguous(sample):
        return AMBIGUOUS, f"ground-truth notes marked ambiguous: {sample.ground_truth.notes[:80]}"

    # 3. 注入标记:对抗集 prompt 没约束住 —— prompt 工程问题
    if _has_injection(sample):
        return PROMPT_ISSUE, "injection / instruction-override marker present in diff or title"

    # 4. 数据层:diff 空或缺真值 —— 评测输入残缺
    if _data_incomplete(sample):
        if not (sample.diff or "").strip():
            return DATA_ISSUE, "empty diff — nothing to review"
        return DATA_ISSUE, "no ground truth attached — cannot judge correctness"

    # 5. edge suite:边界样本本就难
    if (sample.suite or "").lower() == "edge":
        return EDGE_CASE, "belongs to edge suite (boundary input by design)"

    # 6. 兜底:有真值、非边界、模型有产出却判错 —— 多半 prompt 没引导到位
    return PROMPT_ISSUE, "risk level mispredicted on a well-formed regression sample"


class FailureLoop:
    """失败回环器 —— 收集失败样本、归类、给出每类聚合统计,驱动后续 prompt/data 修复。"""

    def __init__(self, samples: list[EvalSample], predictions: list[PRReviewOutput],
                 metric_results: Optional[list[MetricResult]] = None):
        self.samples = samples
        self.predictions = predictions
        self.metric_results = metric_results or []
        self._pred_by_id = {p.pr_id: p for p in predictions if p is not None}

    def _is_failure(self, sample: EvalSample, pred: Optional[PRReviewOutput]) -> bool:
        """判定一个样本是否"失败"(才需要归类)。"""
        if pred is None:
            return True
        if _output_is_garbled(pred):
            return True
        if _level_mismatch(sample, pred):
            return True
        if _flagged_by_metrics(sample.pr_id, self.metric_results):
            return True
        return False

    def classify(self) -> list[FailureRecord]:
        out: list[FailureRecord] = []
        for s in self.samples:
            pred = self._pred_by_id.get(s.pr_id)
            if not self._is_failure(s, pred):
                continue
            if pred is None:
                out.append(FailureRecord(s.pr_id, MODEL_ERROR, "no prediction produced for sample"))
                continue
            klass, reason = _classify_one(s, pred)
            out.append(FailureRecord(s.pr_id, klass, reason))
        return out

    def summary(self) -> dict[str, int]:
        """每类失败计数,便于 dashboard / 优先级排序。"""
        counts = {k: 0 for k in FAILURE_CLASSES}
        for rec in self.classify():
            counts[rec.klass] = counts.get(rec.klass, 0) + 1
        return counts


def classify_failures(samples: list[EvalSample], predictions: list[PRReviewOutput],
                      metric_results: Optional[list[MetricResult]] = None
                      ) -> list[dict]:
    """把失败样本归到 5 类,返回 [{pr_id, klass, reason}]。模块级便捷入口。"""
    loop = FailureLoop(samples, predictions, metric_results)
    return [rec.as_dict() for rec in loop.classify()]
