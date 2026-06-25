"""auto-fix —— 把 codegen 从"独立生成"改造成评审的「自动修复建议」引擎。

定位:让 review 从"挑刺"升级到"能动手"。评审 Critic 报了 finding 之后:

    finding(哪行什么问题) + 原始代码
        │ ① propose_fix: 生成修复补丁(接地在原代码 + finding 上)
        ▼
    ② 沙箱真实验证: 补丁不崩 + 回归检查通过(复用 complex_codegen.verify)
        │
        ▼
    ③ 修复回路(≤N): 验证不过 → 把真实报错喂回重修
        │
        ▼
    ④ 只有"验证通过"才输出 GitHub suggested change;**没验证过就只报问题、绝不贴补丁**

核心价值(Cursor/IDE 助手替代不了的):评审 bot 处在 PR 流程里,能把"发现的问题"直接
变成"**沙箱验证过、点一下就能采纳**"的 suggested change——贴出来的补丁保证真跑过、不会更崩。

LLM 可插拔(FixGenerator):生产包 LLM,测试注入确定性修复,使整条逻辑可复现、可单测。
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Protocol

from .complex_codegen import verify, _strip_code   # 复用真实执行验证 + 代码块抽取

MAX_REPAIR = 2


@dataclass
class Finding:
    """评审产出的一条问题。"""
    message: str
    file: str = "solution.py"
    line: int = 0
    severity: int = 5
    critic: str = ""


@dataclass
class FixResult:
    finding: Finding
    original_code: str
    fixed_code: str = ""
    verified: bool = False
    verify_error: str = ""
    repair_rounds: int = 0
    suggestion: str = ""        # GitHub ```suggestion 块(仅验证通过才有)
    diff: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding": self.finding.message,
            "verified": self.verified,
            "verify_error": self.verify_error,
            "repair_rounds": self.repair_rounds,
            "fixed_code": self.fixed_code,
            "suggestion": self.suggestion,
            "diff": self.diff,
            "note": self.note,
            "actionable": bool(self.suggestion),   # 能不能贴 suggested change
        }


class FixGenerator(Protocol):
    """修复生成器接口:生产实现包 LLM,测试实现注入确定性补丁。"""

    def propose_fix(self, finding: Finding, original_code: str,
                    repair_hint: str = "") -> str: ...


def _suggestion_block(fixed_code: str) -> str:
    """GitHub PR 的 ```suggestion 块:reviewer 点一下即可采纳。"""
    return "```suggestion\n" + fixed_code.rstrip("\n") + "\n```"


def _unified_diff(orig: str, fixed: str, path: str = "solution.py") -> str:
    return "".join(difflib.unified_diff(
        orig.splitlines(keepends=True), fixed.splitlines(keepends=True),
        "a/" + path, "b/" + path))


class AutoFix:
    def __init__(self, generator: FixGenerator, max_repair: int = MAX_REPAIR):
        self.gen = generator
        self.max_repair = max_repair

    def run(self, finding: Finding, original_code: str, check_code: str = "") -> FixResult:
        res = FixResult(finding=finding, original_code=original_code)

        # ① 生成修复
        fixed = self._propose(finding, original_code)
        # ② 沙箱真实验证
        ok, err = verify(fixed, check_code) if check_code else (bool(fixed), "")
        # ③ 修复回路:验证不过,把真实报错喂回重修
        rounds = 0
        while not ok and rounds < self.max_repair:
            rounds += 1
            fixed = self._propose(finding, original_code, repair_hint="修复后仍失败:\n" + err)
            ok, err = verify(fixed, check_code) if check_code else (bool(fixed), "")
        res.fixed_code, res.verified, res.verify_error, res.repair_rounds = fixed, ok, err, rounds

        # ④ 只有验证通过才产出 suggested change;否则只报问题、不贴补丁
        if ok:
            res.suggestion = _suggestion_block(fixed)
            res.diff = _unified_diff(original_code, fixed, finding.file)
            res.note = "已产出沙箱验证过的修复补丁(可贴 suggested change)"
        else:
            res.note = "未能产出可验证修复 → 只回报问题,不贴未验证补丁(避免误导)"
        return res

    def _propose(self, finding: Finding, original_code: str, repair_hint: str = "") -> str:
        try:
            return self.gen.propose_fix(finding, original_code, repair_hint=repair_hint) or ""
        except Exception as e:  # noqa: BLE001
            return original_code   # 生成失败:退回原码(等于"没改"),绝不输出半成品


def auto_fix(finding: Finding, original_code: str, generator: FixGenerator,
             check_code: str = "", max_repair: int = MAX_REPAIR) -> dict[str, Any]:
    """auto-fix 入口:评审 finding → 验证过的修复补丁(或诚实地不给)。"""
    return AutoFix(generator, max_repair=max_repair).run(finding, original_code, check_code).as_dict()


# ────────────────────────── 生产实现:包 LLM ──────────────────────────
class LlmFixGenerator:
    """生产修复生成器:用 LLM 针对 finding 修代码。

    llm 需有 ``chat(messages, model_key=..., max_tokens=..., temperature=...) -> resp(.text)``
    (与 devbot LlmClient 一致)。只改必要处,输出完整修复后代码。
    """

    def __init__(self, llm: Any, codedoc: Any = None):
        self.llm = llm
        self.codedoc = codedoc

    def propose_fix(self, finding: Finding, original_code: str, repair_hint: str = "") -> str:
        system = ("你是资深工程师。针对评审指出的问题修复代码,只改必要处、保持其余不变、"
                  "不要改变对外行为。只输出 ```python 代码块(完整的修复后代码)。")
        user = "问题: %s\n位置: %s:%s\n\n原始代码:\n```python\n%s\n```" % (
            finding.message, finding.file, finding.line, original_code)
        if repair_hint:
            user += "\n\n# 上次修复仍未通过验证,改正后重出完整代码\n" + repair_hint
        try:
            resp = self.llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model_key="codegen", max_tokens=1200, temperature=0.1)
            return _strip_code(getattr(resp, "text", "") or "") or original_code
        except Exception:
            return original_code
