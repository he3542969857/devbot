"""裁判(Judge)实现 —— 给一条 review 输出在三维上打 0-5 分。

三维:relevance(findings 是否切题 / 命中真缺陷)、actionability(建议是否可
落地)、novelty(是否给出非显而易见的洞见)。两种实现满足 ``protocols.Judge``:

- ``DeterministicJudge``:hash + 启发式,对着 ground truth 打分,完全可复现、
  不调 LLM。用于 CI。
- ``LlmBackedJudge``:调真 LLM 对着 review 输出打分,**绝不看 ground truth**
  (避免泄漏真值、保证它评的是输出本身的质量);带稳健 JSON 解析与兜底。
"""
from __future__ import annotations

import hashlib
import json
import re

from devbot_eval.domain import Finding, PRReviewOutput, RiskLevel
from devbot_eval.sample import EvalSample


def _clamp5(x) -> float:
    """夹到 [0,5]。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v < 0:
        return 0.0
    if v > 5:
        return 5.0
    return round(v, 3)


def _stable_unit(*parts: str) -> float:
    """对若干字符串做稳定 hash,映射到 [0,1)。用于给确定性打分加一点抖动。"""
    h = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return (int(h[:8], 16) % 1000) / 1000.0


class DeterministicJudge:
    """确定性裁判:对着 ground truth 用启发式打 relevance/actionability/novelty。

    可复现(同输入同分),不调 LLM。打分逻辑:

    - relevance:预测 finding 命中真值 finding 位置的比例(file 命中 + 行号
      容忍 ±2),无真值则看预测 findings 是否非空 + 风险等级是否合理。
    - actionability:有多少 critic 给了非空 suggestion + finding 是否带行号
      (带行号才好定位修复)。
    - novelty:findings 跨文件 / 跨 critic 的多样性(都挤在一处则新意低),
      再叠一点稳定 hash 抖动避免常数。
    """

    name = "deterministic"

    def judge(self, sample: EvalSample, output: PRReviewOutput) -> dict:
        gt = sample.ground_truth
        preds = output.findings

        relevance = self._relevance(gt.expected_findings if gt else [],
                                    gt.expected_risk_level if gt else None,
                                    preds, output.risk_level)
        actionability = self._actionability(output)
        novelty = self._novelty(sample.pr_id, preds)

        return {
            "relevance": _clamp5(relevance),
            "actionability": _clamp5(actionability),
            "novelty": _clamp5(novelty),
        }

    # ── 三维 ────────────────────────────────────────────────────────────
    def _relevance(self, expected: list[Finding], expected_level,
                   preds: list[Finding], pred_level: RiskLevel) -> float:
        if expected:
            matched = 0
            for ef in expected:
                if any(self._loc_match(ef, pf) for pf in preds):
                    matched += 1
            recall = matched / len(expected)
            base = 5.0 * recall
            # 风险等级对齐再给 0.5 的小奖励 / 惩罚。
            if expected_level is not None:
                if pred_level == expected_level:
                    base = min(5.0, base + 0.5)
                elif self._level_gap(pred_level, expected_level) >= 2:
                    base = max(0.0, base - 0.5)
            return base
        # 无真值:有 findings 即认为基本切题,数量适中给高分。
        if not preds:
            return 1.5
        n = len(preds)
        if 1 <= n <= 6:
            return 4.0
        return 3.0  # 过多 findings,疑似过报,切题度打折

    def _actionability(self, output: PRReviewOutput) -> float:
        critics = output.critics or []
        if not critics:
            return 1.0
        with_sugg = sum(1 for c in critics if (c.suggestion or "").strip())
        sugg_ratio = with_sugg / len(critics)
        preds = output.findings
        if preds:
            with_line = sum(1 for f in preds if f.line is not None)
            line_ratio = with_line / len(preds)
        else:
            line_ratio = 0.0
        # suggestion 占 3 分,行号定位占 2 分。
        return 3.0 * sugg_ratio + 2.0 * line_ratio

    def _novelty(self, pr_id: str, preds: list[Finding]) -> float:
        if not preds:
            return 1.0
        files = {f.file for f in preds if f.file}
        critics = {f.critic for f in preds if f.critic}
        lines = {f.line for f in preds if f.line is not None}
        # 多样性:跨文件 / 跨 critic / 跨行越多越"有新意"(不是一处反复报)。
        diversity = (len(files) + len(critics) + min(len(lines), 4)) / 11.0
        base = 1.5 + 3.0 * min(1.0, diversity)
        jitter = (_stable_unit(pr_id) - 0.5) * 0.4  # ±0.2 稳定抖动
        return base + jitter

    # ── 工具 ───────────────────────────────────────────────────────────
    @staticmethod
    def _loc_match(a: Finding, b: Finding, line_tol: int = 2) -> bool:
        if (a.file or "") != (b.file or ""):
            # 容忍 basename 匹配(diff header 可能带 a/ b/ 前缀)
            if not a.file or not b.file:
                return False
            if a.file.split("/")[-1] != b.file.split("/")[-1]:
                return False
        if a.line is None or b.line is None:
            return a.line is None and b.line is None
        try:
            return abs(int(a.line) - int(b.line)) <= line_tol
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _level_gap(a: RiskLevel, b: RiskLevel) -> int:
        order = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}
        return abs(order.get(a, 0) - order.get(b, 0))


class LlmBackedJudge:
    """真 LLM 裁判:调 LlmClient 对 review 输出打三维分,**不看 ground truth**。

    只把"被评的 review 输出"喂给模型(PR 标题 + 各 critic 的 findings/建议),
    让它当一个独立评审专家给 relevance/actionability/novelty 0-5。带稳健 JSON
    解析:抽 ```json``` 块或第一个 {...};解析失败 / 字段缺失时退回中性分 2.5,
    保证管道不崩(mock provider 的 else 分支返回非 JSON 时即走兜底)。
    """

    name = "llm_judge"

    _SYSTEM = (
        "You are an impartial senior code-review quality judge. You will be shown a PR "
        "title and an automated reviewer's output (its findings and suggestions). Score "
        "the OUTPUT on three axes, each an integer 0-5:\n"
        "- relevance: do the findings address real, on-topic issues in this PR?\n"
        "- actionability: are the suggestions concrete enough for a developer to act on?\n"
        "- novelty: do they surface non-obvious insight beyond trivial nits?\n"
        "You do NOT have the ground truth; judge the output on its own merits.\n"
        "Respond with EXACTLY this JSON and nothing else:\n"
        '{"relevance": <0-5>, "actionability": <0-5>, "novelty": <0-5>}'
    )

    def __init__(self, llm=None, model_key: str = "default"):
        self._llm = llm
        self._model_key = model_key

    def _client(self):
        if self._llm is None:
            # 惰性建客户端:仅 --real 路径才真的需要 LLM。
            from devbot.llm import LlmClient
            self._llm = LlmClient()
        return self._llm

    def judge(self, sample: EvalSample, output: PRReviewOutput) -> dict:
        user_msg = self._render(sample, output)
        try:
            resp = self._client().chat(
                [{"role": "system", "content": self._SYSTEM},
                 {"role": "user", "content": user_msg}],
                model_key=self._model_key, max_tokens=120, temperature=0.0,
            )
            parsed = self._parse(resp.text)
        except Exception:
            parsed = {}
        return {
            "relevance": _clamp5(parsed.get("relevance", 2.5)),
            "actionability": _clamp5(parsed.get("actionability", 2.5)),
            "novelty": _clamp5(parsed.get("novelty", 2.5)),
        }

    # ── 渲染被评内容(绝不含 ground truth) ──────────────────────────────
    @staticmethod
    def _render(sample: EvalSample, output: PRReviewOutput) -> str:
        lines = [f"PR Title: {sample.title or sample.pr_id}",
                 f"Language: {sample.language}",
                 f"Overall risk: {output.risk_score} ({output.risk_level.value})",
                 "Reviewer findings:"]
        preds = output.findings
        if not preds:
            lines.append("  (no findings)")
        for f in preds[:30]:
            loc = f"{f.file}:{f.line}" if f.line is not None else (f.file or "?")
            lines.append(f"  - [{f.critic or '?'}/{f.severity}] {loc} — {f.message}")
        sugg = [c.suggestion.strip() for c in (output.critics or []) if (c.suggestion or "").strip()]
        if sugg:
            lines.append("Suggestions:")
            for s in sugg[:8]:
                lines.append(f"  - {s}")
        return "\n".join(lines)

    # ── 稳健 JSON 解析 ──────────────────────────────────────────────────
    @staticmethod
    def _parse(text: str) -> dict:
        if not text:
            return {}
        for pat in (r"```json\s*(\{.*?\})\s*```", r"(\{.*\})"):
            m = re.search(pat, text, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(1))
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    continue
        return {}
