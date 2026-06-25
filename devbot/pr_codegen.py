"""PR 接地代码生成 —— 吃 (需求文档 + PR) 双输入,在 PR 现有代码上按需求补全/修改。

修正 codegen 旧形态的根因:旧版只吃一个 `task` 字符串、脱离上下文凭空写函数,所以鸡肋。
真实业务输入是【用户上传的 PR + 需求文档】:
  · PR(diff)        = 范围 + 上下文 + "改在哪"(现有类/函数/真实 API 都在里面)
  · 需求文档        = 要做成什么

    需求文档 + PR diff
       │ ① 解析 PR 现有代码(diff 新侧)当接地上下文 + codedoc 补相关符号
       ▼
    ② 生成: 按需求、在 PR 现有代码上补全/修改(复用真实符号,不重定义)
       │
       ▼
    ③ 沙箱验证: 整体真实执行(崩不崩 / 验收 check 过不过)—— 复用 complex_codegen.verify
       │
       ▼
    ④ 修复回路(≤2): 验证不过 → 把真实报错喂回重生成
       │
       ▼
    输出: {generated_code, patch(对 PR 现有代码的 diff), verified, ...}

LLM 可插拔(PRGenerator):生产 LlmPRGenerator(读需求+PR+codedoc 接地),测试注入确定性生成器。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .complex_codegen import verify, _strip_code

MAX_REPAIR = 2


# ── 从 PR unified diff 还原"新侧"代码(= PR 落下后的现有代码,当接地上下文) ──
def pr_new_side(diff: str) -> str:
    """取 diff 每个 hunk 的新侧:只在 @@ hunk 内取 + 与上下文行(去前缀),- 行与所有头/git 元信息丢弃。"""
    if not diff:
        return ""
    out: list[str] = []
    in_hunk = False
    for ln in diff.splitlines():
        if ln.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue                      # 头部噪声(diff/index/new file mode/--- +++ 等)一律跳过
        if ln.startswith("+"):
            out.append(ln[1:])
        elif ln.startswith("-"):
            continue
        elif ln.startswith(" ") or ln == "":
            out.append(ln[1:] if ln.startswith(" ") else ln)
        else:
            in_hunk = False               # 离开 hunk(下一个文件的头)
    return "\n".join(out).strip() + "\n"


@dataclass
class PRCodegenResult:
    requirement: str
    pr_context: str = ""
    generated_code: str = ""
    verified: bool = False
    verify_error: str = ""
    repair_rounds: int = 0
    patch: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "generated_code": self.generated_code,
            "patch": self.patch,
            "verified": self.verified,
            "verify_error": self.verify_error,
            "repair_rounds": self.repair_rounds,
            "note": self.note,
        }


class PRGenerator(Protocol):
    """按 需求 + PR 现有代码 生成完整更新后代码。生产包 LLM,测试注入确定性实现。"""

    def generate(self, requirement: str, pr_context: str, repair_hint: str = "") -> str: ...


class PRCodegen:
    def __init__(self, generator: PRGenerator, max_repair: int = MAX_REPAIR):
        self.gen = generator
        self.max_repair = max_repair

    def run(self, requirement: str, pr_diff: str, check_code: str = "") -> PRCodegenResult:
        res = PRCodegenResult(requirement=requirement)
        res.pr_context = pr_new_side(pr_diff)

        code = self._gen(requirement, res.pr_context)
        ok, err = self._verify(code, check_code)
        rounds = 0
        while not ok and rounds < self.max_repair:
            rounds += 1
            code = self._gen(requirement, res.pr_context, repair_hint="验证失败:\n" + err)
            ok, err = self._verify(code, check_code)
        res.generated_code, res.verified, res.verify_error, res.repair_rounds = code, ok, err, rounds
        res.patch = "".join(difflib.unified_diff(
            res.pr_context.splitlines(keepends=True), (code or "").splitlines(keepends=True),
            "a/pr_code.py", "b/pr_code.py"))
        res.note = ("生成并经沙箱验证通过" if ok else "仍未通过验证: " + err[:120])
        return res

    def _gen(self, requirement: str, pr_context: str, repair_hint: str = "") -> str:
        try:
            return self.gen.generate(requirement, pr_context, repair_hint=repair_hint) or ""
        except Exception as e:  # noqa: BLE001
            return "# generate failed: %s\n" % e

    def _verify(self, code: str, check_code: str) -> tuple[bool, str]:
        if not (code or "").strip():
            return False, "空代码"
        return verify(code, check_code) if check_code else verify(code, "import solution\n")


def build_pr_code(requirement: str, pr_diff: str, generator: PRGenerator,
                  check_code: str = "", max_repair: int = MAX_REPAIR) -> dict[str, Any]:
    """PR 接地代码生成入口:(需求文档 + PR diff) → 验证过的代码 + patch。"""
    return PRCodegen(generator, max_repair=max_repair).run(requirement, pr_diff, check_code).as_dict()


# ── 生产实现:包 LLM ──
class LlmPRGenerator:
    """生产生成器:读 需求文档 + PR 现有代码(+ 可选 codedoc 接地)生成完整更新后代码。"""

    def __init__(self, llm: Any, codedoc: Any = None, repo: str = ""):
        self.llm = llm
        self.codedoc = codedoc
        self.repo = repo

    def generate(self, requirement: str, pr_context: str, repair_hint: str = "") -> str:
        grounding = ""
        if self.codedoc is not None and self.repo:
            try:
                hits = self.codedoc.search(self.repo, requirement, top_k=4) or []
                grounding = "\n".join("- %s%s" % (h.get("qualified_name", ""), h.get("signature", ""))
                                      for h in hits[:4])
            except Exception:
                grounding = ""
        system = ("你是资深工程师。根据【需求文档】,在给定的【PR 现有代码】基础上补全/修改实现。"
                  "复用 PR 里已有的类/函数/真实 API,**不要重定义已存在的符号**。"
                  "输出**完整的更新后代码**(可被 import 测试),只输出 ```python 代码块。")
        user = "## 需求文档\n%s\n\n## PR 现有代码\n```python\n%s\n```" % (requirement, pr_context)
        if grounding:
            user += "\n\n## codedoc 检索到的真实 API(优先复用)\n" + grounding
        if repair_hint:
            user += "\n\n## 上次生成未通过验证,改正后重出完整代码\n" + repair_hint
        try:
            resp = self.llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model_key="codegen", max_tokens=1600, temperature=0.2)
            return _strip_code(getattr(resp, "text", "") or "")
        except Exception:
            return ""
