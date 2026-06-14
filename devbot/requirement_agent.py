"""需求分析 Agent —— 把一段需求文本拆解为子任务 / 影响范围 / 验收标准 / 风险。

单次 LLM 调用(model_key="requirement"),让模型按固定 JSON 形态输出:
``{subtasks:[{id,title,effort}], impact_modules:[...], acceptance_criteria:[...],
   risk_notes:[...], estimated_effort:"S|M|L"}``。

设计要点:
- system prompt 必须含 "requirement" 与 "subtask" 两词 —— 既描述任务,也让 mock provider
  命中 llm.py 里的 requirement/subtask 分支返回结构化样例。
- LLM 输出健壮解析:剥 ```json``` 代码围栏、再尝试裸 JSON,解析失败回退到安全默认 dict,
  保证调用方拿到的永远是形态稳定的结果(不会因模型抽风而抛异常)。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .llm import LlmClient

# ── 输出契约的固定键 & 合法档位 ──────────────────────────────────────
_EFFORT_VALUES = {"S", "M", "L"}
_DEFAULT_EFFORT = "M"

_SYSTEM_PROMPT = (
    "You are a requirement analysis assistant for a software engineering team. "
    "Given a free-form requirement description, decompose it into actionable work: "
    "break it into subtasks, identify impacted modules, derive acceptance criteria, "
    "flag risks, and estimate overall effort.\n\n"
    "Each subtask must have a short id, a clear title and an effort estimate. "
    "Respond with EXACTLY this JSON (no prose, no markdown outside the code fence):\n"
    "```json\n"
    "{\n"
    '  "subtasks": [{"id": "T1", "title": "<what>", "effort": "S|M|L"}],\n'
    '  "impact_modules": ["<module>"],\n'
    '  "acceptance_criteria": ["<verifiable condition>"],\n'
    '  "risk_notes": ["<risk or unknown>"],\n'
    '  "estimated_effort": "S|M|L"\n'
    "}\n"
    "```\n"
    "Keep subtasks atomic and acceptance criteria objectively testable."
)


def _safe_default(text: str) -> dict[str, Any]:
    """解析失败时的安全回退 —— 形态与正常输出完全一致,内容标注为待人工细化。"""
    snippet = (text or "").strip().splitlines()[0][:120] if text else ""
    return {
        "subtasks": [{"id": "T1", "title": "Clarify and decompose requirement",
                      "effort": _DEFAULT_EFFORT}],
        "impact_modules": [],
        "acceptance_criteria": ["Requirement is fully specified and reviewed"],
        "risk_notes": ["Automatic decomposition failed; manual analysis required"]
        + ([f"raw: {snippet}"] if snippet else []),
        "estimated_effort": _DEFAULT_EFFORT,
    }


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 文本里抽出 JSON 对象:先试 ```json``` 围栏,再试裸 {...}。失败抛 ValueError。"""
    if not text:
        raise ValueError("empty LLM response")
    for pat in (r"```(?:json)?\s*(\{.*?\})\s*```", r"(\{.*\})"):
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("no JSON object found in LLM response")


def _as_str_list(value: Any) -> list[str]:
    """把任意值规整成字符串列表(模型偶尔返回单个字符串或夹杂非串元素)。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                if item.strip():
                    out.append(item)
            elif item is not None:
                out.append(str(item))
        return out
    return [str(value)]


def _norm_effort(value: Any) -> str:
    """把 effort 规整到 S|M|L,无法识别时回退默认档。"""
    if isinstance(value, str):
        v = value.strip().upper()
        if v in _EFFORT_VALUES:
            return v
        # 容忍 "Small"/"Medium"/"Large" 之类
        first = v[:1]
        if first in _EFFORT_VALUES:
            return first
    return _DEFAULT_EFFORT


def _norm_subtasks(value: Any) -> list[dict[str, Any]]:
    """规整子任务列表:补全 id/title/effort 三键,过滤空标题项。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(value, start=1):
        if isinstance(raw, dict):
            title = str(raw.get("title", "")).strip()
            if not title:
                continue
            out.append({
                "id": str(raw.get("id") or f"T{i}"),
                "title": title,
                "effort": _norm_effort(raw.get("effort")),
            })
        elif isinstance(raw, str) and raw.strip():
            out.append({"id": f"T{i}", "title": raw.strip(), "effort": _DEFAULT_EFFORT})
    return out


def _normalize(parsed: dict[str, Any], raw_text: str) -> dict[str, Any]:
    """把解析出的 dict 规整成稳定形态;若关键字段全空则退回安全默认。"""
    result = {
        "subtasks": _norm_subtasks(parsed.get("subtasks")),
        "impact_modules": _as_str_list(parsed.get("impact_modules")),
        "acceptance_criteria": _as_str_list(parsed.get("acceptance_criteria")),
        "risk_notes": _as_str_list(parsed.get("risk_notes")),
        "estimated_effort": _norm_effort(parsed.get("estimated_effort")),
    }
    if not result["subtasks"] and not result["acceptance_criteria"]:
        # 模型返回了 JSON 但内容空洞 —— 视为失败,给安全回退
        return _safe_default(raw_text)
    return result


def analyze_requirement(text: str, llm: LlmClient | None = None) -> dict:
    """分析需求文本,返回拆解结果 dict。

    参数:
        text: 自由格式的需求描述。
        llm: 可选 LlmClient(默认按 settings 新建,支持 mock / openai)。

    返回固定形态 dict:
        ``{subtasks, impact_modules, acceptance_criteria, risk_notes, estimated_effort}``
    解析失败永不抛出,回退到安全默认结构。
    """
    text = (text or "").strip()
    if not text:
        return _safe_default("")

    llm = llm or LlmClient()
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Requirement:\n{text}"},
    ]
    try:
        resp = llm.chat(messages, model_key="requirement", max_tokens=1024, temperature=0.2)
    except Exception:
        return _safe_default("")

    try:
        parsed = _extract_json(resp.text)
    except ValueError:
        return _safe_default(resp.text)
    return _normalize(parsed, resp.text)


class RequirementAgent:
    """需求分析 Agent 的薄封装 —— 复用同一个 LlmClient 跨多次调用。"""

    def __init__(self, llm: LlmClient | None = None):
        self.llm = llm or LlmClient()

    def analyze(self, text: str) -> dict:
        """分析单条需求文本,委托给模块级 analyze_requirement。"""
        return analyze_requirement(text, llm=self.llm)


__all__ = ["analyze_requirement", "RequirementAgent"]
