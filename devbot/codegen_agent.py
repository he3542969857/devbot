"""代码生成 Agent —— 检索接地 → 接地生成 → 确定性语法检查 → **沙箱执行验证** → 修复回路 → 终审。

闭环(对齐 codedoc 强项 + 真正可运行验证):
1. ``retrieve``:codedoc 语义+全文检索找相似实现 / 相关符号,对 top-K 取**真实函数体**当 API 参考;
   任务提到文件名再 ``get_impact`` 拿集成点。喂真实签名,不让 LLM 编 API。codedoc 不可用优雅降级。
2. ``generate``:接地生成。
3. ``_syntax_check``:**确定性**校验(Python ``ast.parse`` / JSON ``json.loads``),零成本、不经 LLM。
4. ``_exec_check``:**沙箱实跑**——降权子进程(rlimit CPU/内存/文件/进程数 + 超时杀进程组 + 隔离临时目录 +
   剥离环境)里 import/执行生成代码;再复用 testgen 生成测试、跑 pytest(best-effort)。
5. ``repair`` 回路:语法**或执行**失败,把错误喂回去重生成(``MAX_REPAIR`` 轮)。
6. ``validate``:LLM 终审。

安全边界:这是 **rlimit 子进程沙箱**(防 fork 炸弹 / 跑飞 / 占满磁盘 / 泄露密钥),
**不做网络命名空间隔离**——真正强隔离要 container/nsjail。可用 ``DEVBOT_CODEGEN_EXEC=0`` 关闭执行。
全程 robust JSON + 安全 fallback,任何异常都返回结构完整 dict、绝不抛给调用方。
"""
from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import sandbox
from .config import get_settings
from .llm import LlmClient

try:  # pragma: no cover
    from .codedoc_client import CodedocClient
except Exception:  # pragma: no cover
    CodedocClient = Any  # type: ignore

try:  # 复用完整 testgen(执行 + 覆盖率 + 修复)
    from .testgen_agent import generate_tests as _gen_tests
except Exception:  # pragma: no cover
    _gen_tests = None  # type: ignore

MAX_REPAIR = 2

_GEN_SYSTEM = (
    "You are a senior software engineer. Generate code for the requested task.\n"
    "This is a code generation task: produce clean, idiomatic, production-ready code in the "
    "target language. **Ground your code in the retrieved real APIs / conventions below — "
    "reuse existing symbols and call signatures, do not invent APIs that aren't shown.** "
    "For Python, define top-level functions/classes so they can be imported and tested.\n\n"
    "## Output format\nRespond with EXACTLY this JSON (no prose outside the JSON):\n"
    "```json\n{\n"
    '  "generated_code": "<the full source code as a string>",\n'
    '  "language": "<language>",\n'
    '  "explanation": "<one short paragraph; mention which retrieved symbols you reused>"\n'
    "}\n```"
)

_VALIDATE_SYSTEM = (
    "You are a strict code reviewer. Validate and review code for correctness, safety and "
    "adherence to best practices. Check syntax errors, logic bugs, unhandled edge cases, "
    "resource leaks and security issues. Be concrete.\n\n"
    "## Output format\nRespond with EXACTLY this JSON (no prose outside the JSON):\n"
    "```json\n{\n"
    '  "is_valid": <true|false>,\n'
    '  "validation_notes": "<summary of the review>",\n'
    '  "suggestions": ["<actionable suggestion>", "..."]\n'
    "}\n```"
)


@dataclass
class CodegenState:
    task: str
    language: str = "python"
    repo: str = ""
    context_summary: str = ""
    retrieved_symbols: list[str] = field(default_factory=list)
    impact_nodes: list[dict[str, Any]] = field(default_factory=list)
    generated_code: str = ""
    explanation: str = ""
    syntax_ok: bool = True
    syntax_error: str = ""
    exec_ran: bool = False
    exec_ok: bool = True
    exec_error: str = ""
    tests_total: int = 0
    tests_passed: int = 0
    coverage: Any = None
    tests_note: str = ""
    repair_rounds: int = 0
    validation: dict[str, Any] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    error: Optional[str] = None


def _extract_json(text: str) -> dict[str, Any]:
    if not text:
        return {}
    for pat in (r"```json\s*(\{.*?\})\s*```", r"```\s*(\{.*?\})\s*```", r"(\{.*\})"):
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(1))
                if isinstance(obj, dict):
                    return obj
            except (json.JSONDecodeError, ValueError):
                continue
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _coerce_suggestions(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                v = item.get("suggestion") or item.get("message") or item.get("text")
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
            elif item is not None:
                out.append(str(item))
        return out
    return [str(raw)]


def _coerce_bool(raw: Any, default: bool = True) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "yes", "valid", "1", "ok")
    return default


def _guess_files(task: str) -> list[str]:
    if not task:
        return []
    hits = re.findall(r"[\w./-]+\.(?:py|java|ts|js|go|rb|kt|c|cpp|h|cs|rs)", task)
    seen: list[str] = []
    for h in hits:
        if h not in seen:
            seen.append(h)
    return seen[:5]


class CodegenAgent:
    """检索接地 + 沙箱执行验证的代码生成。"""

    def __init__(self, llm: LlmClient | None = None, codedoc: Any | None = None):
        self.llm = llm or LlmClient()
        self.codedoc = codedoc

    # ── 阶段 1:检索接地 ──────────────────────────────────────────────
    def retrieve(self, state: CodegenState) -> CodegenState:
        if self.codedoc is None or not state.repo:
            state.context_summary = "(no codedoc context: repo not provided)"
            return state
        try:
            hits = self.codedoc.search(state.repo, state.task, top_k=6) or []
            lines: list[str] = []
            for h in hits[:6]:
                qn = h.get("qualified_name") or h.get("name") or "?"
                sig = h.get("signature") or ""
                state.retrieved_symbols.append(qn)
                lines.append(f"- `{qn}`{sig}".rstrip())
            bodies: list[str] = []
            for h in hits[:3]:
                nid = h.get("node_id") or h.get("id")
                if not nid:
                    continue
                body = self.codedoc.get_body(state.repo, nid, max_lines=40)
                if body:
                    qn = h.get("qualified_name") or h.get("name") or nid
                    bodies.append(f"# {qn}\n{body}")
            files = _guess_files(state.task)
            if files:
                imp = self.codedoc.get_impact(files, repo=state.repo)
                state.impact_nodes = [n for n in (imp.affected_nodes or []) if isinstance(n, dict)]
            parts = []
            if lines:
                parts.append("## 相关已有符号(优先复用,不要另造)\n" + "\n".join(lines))
            if bodies:
                parts.append("## 真实实现参考(API / 风格)\n```\n" + "\n\n".join(bodies)[:3000] + "\n```")
            if state.impact_nodes:
                names = ", ".join(str(n.get("qualified_name") or n.get("id")) for n in state.impact_nodes[:8])
                parts.append(f"## 集成点(改动影响)\n{names}")
            state.context_summary = "\n\n".join(parts) if parts else "(codedoc returned no relevant context)"
        except Exception as e:
            state.context_summary = "(codedoc lookup failed; proceeding without context)"
            state.error = f"retrieve: {e}"
        return state

    # ── 阶段 2:接地生成 ─────────────────────────────────────────────
    def _generate(self, state: CodegenState, repair_hint: str = "") -> CodegenState:
        user_msg = f"Task: {state.task}\nTarget language: {state.language}"
        if state.context_summary:
            user_msg += f"\n\n# Retrieved context (from codedoc)\n{state.context_summary}"
        if repair_hint:
            user_msg += (f"\n\n# Previous attempt FAILED checks — fix and regenerate\n{repair_hint}\n"
                         "Return the full corrected code.")
        try:
            resp = self.llm.chat(
                [{"role": "system", "content": _GEN_SYSTEM},
                 {"role": "user", "content": user_msg}],
                model_key="codegen", max_tokens=1200, temperature=0.2,
            )
            state.tokens_in += resp.tokens_in
            state.tokens_out += resp.tokens_out
            data = _extract_json(resp.text)
            state.generated_code = str(data.get("generated_code", "") or "")
            lang = data.get("language")
            if isinstance(lang, str) and lang.strip():
                state.language = lang.strip()
            state.explanation = str(data.get("explanation", "") or "")
            if not state.generated_code:
                state.generated_code = resp.text.strip()
                state.explanation = state.explanation or "(model returned no structured code)"
        except Exception as e:
            state.error = f"generate: {e}"
            state.explanation = state.explanation or f"generation failed: {e}"
        return state

    # ── 阶段 3:确定性语法检查 ──────────────────────────────────────
    def _syntax_check(self, state: CodegenState) -> CodegenState:
        code, lang = state.generated_code, (state.language or "").lower()
        if not code:
            state.syntax_ok, state.syntax_error = False, "empty code"
            return state
        try:
            if lang in ("python", "py"):
                ast.parse(code)
            elif lang in ("json",):
                json.loads(code)
            state.syntax_ok, state.syntax_error = True, ""
        except SyntaxError as e:
            state.syntax_ok = False
            state.syntax_error = f"SyntaxError: {e.msg} (line {e.lineno})"
        except Exception as e:
            state.syntax_ok, state.syntax_error = False, str(e)[:200]
        return state

    # ── 阶段 4:沙箱执行验证(冒烟驱动修复 + 复用完整 testgen 拿覆盖率) ──
    def _exec_check(self, state: CodegenState) -> CodegenState:
        if (state.language or "").lower() not in ("python", "py") or not state.generated_code:
            return state  # 非 Python 不执行(exec_ok 保持 True)
        if not state.syntax_ok or not sandbox.enabled():
            return state
        # 1) 冒烟:import/执行不崩(可靠信号,驱动修复回路)
        ok, err = sandbox.smoke_run(state.generated_code, timeout=6)
        state.exec_ran = True
        state.exec_ok = ok
        if not ok:
            state.exec_error = ("运行失败:\n" + err).strip()[-600:]
            return state
        # 2) 复用完整 testgen:生成测试 + 沙箱执行 + 覆盖率(best-effort,不阻塞主结论)
        if _gen_tests is not None:
            try:
                tg = _gen_tests(code=state.generated_code, language="python",
                                llm=self.llm, execute=True)
                state.tests_passed = tg.get("tests_passed", 0)
                state.tests_total = tg.get("tests_total", 0)
                state.coverage = tg.get("coverage")
                if state.tests_total:
                    state.tests_note = "pytest %d/%d 通过, 覆盖率 %s%%(测试为自动生成)" % (
                        state.tests_passed, state.tests_total,
                        state.coverage if state.coverage is not None else "?")
                else:
                    state.tests_note = "测试未能运行(导入/收集失败),仅供参考"
            except Exception as e:
                state.tests_note = "测试生成/运行异常: %s" % str(e)[:120]
        return state

    # ── 阶段 5:终审 ─────────────────────────────────────────────────
    def _validate(self, state: CodegenState) -> CodegenState:
        if not state.generated_code:
            state.validation = {"is_valid": False,
                                "validation_notes": "no code generated to validate", "suggestions": []}
            return state
        user_msg = (f"Language: {state.language}\nOriginal task: {state.task}\n\n"
                    f"Code under review:\n```\n{state.generated_code[:6000]}\n```")
        try:
            resp = self.llm.chat(
                [{"role": "system", "content": _VALIDATE_SYSTEM},
                 {"role": "user", "content": user_msg}],
                model_key="codegen", max_tokens=512, temperature=0.1,
            )
            state.tokens_in += resp.tokens_in
            state.tokens_out += resp.tokens_out
            data = _extract_json(resp.text)
            state.validation = {
                "is_valid": _coerce_bool(data.get("is_valid"), default=True),
                "validation_notes": str(data.get("validation_notes", "") or ""),
                "suggestions": _coerce_suggestions(data.get("suggestions")),
            }
        except Exception as e:
            state.validation = {"is_valid": False,
                                "validation_notes": f"validation step failed: {e}", "suggestions": []}
            state.error = state.error or f"validate: {e}"
        return state

    # ── 编排:retrieve → generate →(syntax → exec → repair)* → validate ──
    def run(self, task: str, language: str = "python", repo: str = "") -> dict[str, Any]:
        state = CodegenState(task=str(task or ""), language=str(language or "python"), repo=str(repo or ""))
        state = self.retrieve(state)
        state = self._generate(state)
        state = self._syntax_check(state)
        state = self._exec_check(state)
        # 修复回路:语法 或 执行 失败,把错误喂回重生成
        while (not state.syntax_ok or not state.exec_ok) and state.repair_rounds < MAX_REPAIR:
            state.repair_rounds += 1
            hint = state.syntax_error or state.exec_error
            state = self._generate(state, repair_hint=hint)
            state = self._syntax_check(state)
            state = self._exec_check(state)
        state = self._validate(state)
        return {
            "generated_code": state.generated_code,
            "language": state.language,
            "explanation": state.explanation,
            "retrieved_symbols": state.retrieved_symbols,
            "syntax_ok": state.syntax_ok,
            "syntax_error": state.syntax_error,
            "exec_ran": state.exec_ran,
            "exec_ok": state.exec_ok,
            "exec_error": state.exec_error,
            "tests": {"passed": state.tests_passed, "total": state.tests_total,
                      "coverage": state.coverage, "note": state.tests_note},
            "repair_rounds": state.repair_rounds,
            "validation": state.validation,
            "tokens": {"tokens_in": state.tokens_in, "tokens_out": state.tokens_out,
                       "total": state.tokens_in + state.tokens_out},
            **({"error": state.error} if state.error else {}),
        }


def generate_code(task: str, language: str = "python", repo: str = "",
                  llm: LlmClient | None = None, codedoc: Any | None = None) -> dict[str, Any]:
    """检索接地 + 沙箱执行验证的代码生成入口。"""
    return CodegenAgent(llm=llm, codedoc=codedoc).run(task, language=language, repo=repo)
