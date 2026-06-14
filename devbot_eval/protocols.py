"""被测系统(Evaluator)与裁判(Judge)协议 —— 框架与实现解耦,可换 mock / 真。"""
from __future__ import annotations

from typing import Protocol

from devbot_eval.domain import PRReviewOutput
from devbot_eval.sample import EvalSample


class Evaluator(Protocol):
    """对一个样本产出 PRReviewOutput(MockEvaluator 确定性 / FunctionEvaluator 包真 review_pr)。"""
    def evaluate(self, sample: EvalSample) -> PRReviewOutput: ...


class Judge(Protocol):
    """对 (样本, 输出) 打 relevance/actionability/novelty 0-5(Deterministic / LlmBacked)。"""
    def judge(self, sample: EvalSample, output: PRReviewOutput) -> dict: ...
