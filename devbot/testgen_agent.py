"""单元测试生成 Agent —— 完整闭环:接地 → 规划 → 生成 → 执行+覆盖率 → 覆盖率驱动修复。

    目标代码/diff
       │ ① 接地: AST 确定性抽真实分支/异常/返回场景(不靠 LLM 猜) + codedoc 取相关实现
       ▼
    ② 规划: 确定性分支场景 + LLM 补语义边界
       │
       ▼
    ③ 生成: 接地在真实签名+分支上,可 import 被测模块
       │
       ▼
    ④ 执行+覆盖率(沙箱 coverage.py): 通过数 + 行覆盖率 + 未覆盖行号
       │
       ▼
    ⑤ 修复回路(≤2 轮): 收集失败→修;覆盖率<阈值→针对未覆盖行补测试,重跑重测
       │
       ▼
    {generated_tests, test_count, target_methods, boundary_scenarios, language,
     executed, tests_passed, tests_total, coverage, missing_lines, repair_rounds}

非 Python 或执行关闭时退化为"接地+规划+生成"(不执行)。robust JSON + 静态兜底,绝不抛给调用方。
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any

from .llm import LlmClient
from . import sandbox

try:  # codedoc 可选注入
    from .codedoc_client import CodedocClient
except Exception:  # pragma: no cover
    CodedocClient = Any  # type: ignore

COV_TARGET = 80      # 覆盖率目标(%),低于则触发补测修复
MAX_ROUNDS = 2       # 修复回路上限

_FRAMEWORKS = {"python": "pytest", "java": "JUnit 5", "javascript": "Jest",
               "typescript": "Jest", "go": "the standard testing package"}

_ANALYZE_SYSTEM = (
    "You are a unit-test analyst. Analyze the code and the deterministic scenarios "
    "(real branches / exception paths extracted from its AST) and add any *semantic* "
    "edge cases worth covering (empty/null/boundary/overflow/error). Do NOT write code.\n\n"
    "## Output format\nRespond with EXACTLY this JSON:\n"
    "```json\n{\n  \"boundary_scenarios\": [\"<scenario>\", ...]\n}\n```"
)

_GENERATE_SYSTEM_TMPL = (
    "You are a unit-test generator. Generate runnable {framework} tests in {language} for the "
    "code under test. **Import the code under test** (e.g. `from solution import <names>`), write "
    "one focused, deterministic test per scenario including each branch and exception path, with "
    "real assertions on real return values. Cover the listed uncovered lines if any.\n\n"
    "## Output format\nRespond with EXACTLY this JSON:\n"
    "```json\n{{\n  \"generated_tests\": \"<full test source as one string>\",\n"
    "  \"test_count\": <number>,\n  \"language\": \"<language>\"\n}}\n```"
)

_DECL_PATTERNS = [
    re.compile(r"^\+?\s*def\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\+?\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\+?\s*(?:public|private|protected|static|final|\s)*[A-Za-z_][\w<>\[\]]*\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?"),
    re.compile(r"^\+?\s*(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\("),
]
_NOISE = {"if", "for", "while", "switch", "catch", "return", "new", "else", "func", "def", "class"}


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
            except json.JSONDecodeError:
                continue
    return {}


def _as_str_list(value: Any) -> list[str]:
    out, seen = [], set()
    if isinstance(value, str):
        value = [value]
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                item = item.get("name") or item.get("scenario") or item.get("method") or ""
            s = str(item).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
    return out


def _heuristic_methods(text: str) -> list[str]:
    names, seen = [], set()
    for raw in (text or "").splitlines():
        line = raw.lstrip("+")
        for pat in _DECL_PATTERNS:
            m = pat.match(raw) or pat.match(line)
            if m and m.group(1).lower() not in _NOISE and m.group(1).lower() not in seen:
                seen.add(m.group(1).lower())
                names.append(m.group(1))
                break
    return names


def _count_tests(source: str, language: str) -> int:
    if not source:
        return 0
    lang = (language or "python").lower()
    if lang == "java":
        n = len(re.findall(r"@Test\b", source))
    elif lang in ("javascript", "typescript"):
        n = len(re.findall(r"\b(?:test|it)\s*\(", source))
    elif lang == "go":
        n = len(re.findall(r"\bfunc\s+Test\w+\s*\(", source))
    else:
        n = len(re.findall(r"^\s*def\s+test\w*\s*\(", source, re.MULTILINE))
    return n or 1


def _ast_scenarios(code: str) -> list[dict[str, Any]]:
    """① 接地核心:从 AST 确定性抽每个函数的真实分支/异常/循环场景。"""
    funcs: list[dict[str, Any]] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return funcs

    def _u(n):
        try:
            return ast.unparse(n)
        except Exception:
            return "?"

    def _handle(fn):
        scen = []
        for sub in ast.walk(fn):
            if isinstance(sub, ast.If):
                scen.append("条件真/假分支: " + _u(sub.test)[:60])
            elif isinstance(sub, ast.Raise):
                scen.append("异常路径: " + (_u(sub.exc)[:50] if sub.exc else "re-raise"))
            elif isinstance(sub, (ast.For, ast.While)):
                scen.append("循环边界: 空/单元素/多元素")
        args = [a.arg for a in fn.args.args if a.arg != "self"]
        funcs.append({"name": fn.name, "args": args, "scenarios": list(dict.fromkeys(scen))})

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _handle(node)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and not sub.name.startswith("_"):
                    _handle(sub)
    return funcs


class TestgenAgent:
    """完整测试生成闭环:接地 → 规划 → 生成 → 执行+覆盖率 → 修复。"""

    def __init__(self, llm: LlmClient | None = None, codedoc: Any | None = None):
        self.llm = llm or LlmClient()
        self.codedoc = codedoc

    # ② 规划:确定性场景 + LLM 语义补充
    def _plan(self, code: str, language: str, det_scen: list[str]) -> list[str]:
        scen = list(det_scen)
        try:
            user = f"Language: {language}\nCode:\n```\n{code[:4000]}\n```\n\n确定性分支场景:\n" + \
                   "\n".join("- " + s for s in det_scen[:20])
            resp = self.llm.chat([{"role": "system", "content": _ANALYZE_SYSTEM},
                                  {"role": "user", "content": user}],
                                 model_key="testgen", max_tokens=500, temperature=0.2)
            scen += _as_str_list(_extract_json(resp.text).get("boundary_scenarios"))
        except Exception:
            pass
        if not scen:
            scen = ["valid input", "empty input", "boundary value"]
        # 去重保序
        out, seen = [], set()
        for s in scen:
            if s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        return out

    # ③ 生成(支持修复 hint + 上一版测试)
    def _generate(self, code: str, language: str, methods: list[str], scenarios: list[str],
                  ctx: str = "", hint: str = "", prev_tests: str = "") -> str:
        framework = _FRAMEWORKS.get((language or "python").lower(), "an idiomatic test framework")
        system = _GENERATE_SYSTEM_TMPL.format(framework=framework, language=language)
        user = (f"Language: {language}\nTarget functions: {json.dumps(methods)}\n"
                f"Scenarios to cover:\n" + "\n".join("- " + s for s in scenarios[:25]) +
                f"\n\n# Code under test (import from it)\n```\n{code[:4500]}\n```")
        if ctx:
            user += f"\n\n# Related code from codedoc (for realistic deps/usage)\n{ctx[:1500]}"
        if hint:
            user += f"\n\n# Previous attempt feedback — fix\n{hint}"
            if prev_tests:
                user += f"\n\n# Previous tests\n```\n{prev_tests[:2500]}\n```"
        try:
            resp = self.llm.chat([{"role": "system", "content": system},
                                  {"role": "user", "content": user}],
                                 model_key="testgen", max_tokens=1400, temperature=0.2)
            gen = _extract_json(resp.text).get("generated_tests")
            if isinstance(gen, str) and gen.strip():
                return gen.replace("<module>", "solution")
        except Exception:
            pass
        return prev_tests or ""

    # ① codedoc 接地:取相关实现当依赖/用法参考
    def _codedoc_ctx(self, repo: str, query: str) -> str:
        if not (repo and self.codedoc):
            return ""
        try:
            hits = self.codedoc.search(repo, query, top_k=4) or []
            lines = []
            for h in hits[:4]:
                qn = h.get("qualified_name") or h.get("name") or "?"
                lines.append("- %s%s" % (qn, h.get("signature") or ""))
            return "\n".join(lines)
        except Exception:
            return ""

    # 顶层编排
    def run(self, diff: str = "", language: str = "python", code: str = "",
            repo: str = "", execute: bool = True) -> dict[str, Any]:
        language = language or "python"
        target_code = code or sandbox.extract_added_python(diff)
        is_py = (language or "").lower() in ("python", "py")

        ast_funcs = _ast_scenarios(target_code) if is_py else []
        det_scen = [f"{f['name']}: {s}" for f in ast_funcs for s in f["scenarios"]]
        methods = [f["name"] for f in ast_funcs] or _heuristic_methods(diff or target_code)
        ctx = self._codedoc_ctx(repo, " ".join(methods) or (target_code[:200]))

        scenarios = self._plan(target_code, language, det_scen)
        tests = self._generate(target_code, language, methods, scenarios, ctx=ctx)

        out = {"generated_tests": tests, "test_count": _count_tests(tests, language),
               "target_methods": methods, "boundary_scenarios": scenarios, "language": language,
               "executed": False, "tests_passed": 0, "tests_total": 0,
               "coverage": None, "missing_lines": "", "repair_rounds": 0}

        if not (execute and is_py and target_code.strip() and sandbox.enabled() and tests):
            return out

        # ④⑤ 执行 + 覆盖率 + 修复回路
        r = sandbox.run_tests_with_coverage(target_code, tests)
        out.update(executed=r["ran"], tests_passed=r["passed"], tests_total=r["total"],
                   coverage=r["coverage"], missing_lines=r["missing_lines"])
        rounds = 0
        while rounds < MAX_ROUNDS and (
                (not r["ran"]) or (r["coverage"] is not None and r["coverage"] < COV_TARGET)):
            rounds += 1
            if not r["ran"]:
                hint = "测试收集/导入失败,修正 import 与语法让 pytest 能跑: " + (r.get("note", "") or "")
            else:
                hint = ("当前覆盖率 %s%%、未覆盖行 %s;补测覆盖这些行对应的分支/异常路径,保留已通过用例。"
                        % (r["coverage"], r["missing_lines"] or "?"))
            new_tests = self._generate(target_code, language, methods, scenarios,
                                       ctx=ctx, hint=hint, prev_tests=tests)
            if not new_tests or new_tests == tests:
                break
            r2 = sandbox.run_tests_with_coverage(target_code, new_tests)
            # 只在变好(能跑 且 覆盖率不降)时采纳
            better = r2["ran"] and (not r["ran"] or (r2.get("coverage") or 0) >= (r["coverage"] or 0))
            if better:
                tests, r = new_tests, r2
                out.update(generated_tests=tests, test_count=_count_tests(tests, language),
                           executed=r["ran"], tests_passed=r["passed"], tests_total=r["total"],
                           coverage=r["coverage"], missing_lines=r["missing_lines"])
            if r["ran"] and r["coverage"] is not None and r["coverage"] >= COV_TARGET:
                break
        out["repair_rounds"] = rounds
        return out


def generate_tests(diff: str = "", language: str = "python", llm: LlmClient | None = None,
                   code: str = "", repo: str = "", codedoc: Any | None = None,
                   execute: bool = True) -> dict[str, Any]:
    """完整测试生成入口(接地 + 执行 + 覆盖率 + 修复)。兼容旧签名 generate_tests(diff, language, llm)。"""
    return TestgenAgent(llm=llm, codedoc=codedoc).run(diff=diff, language=language, code=code,
                                                      repo=repo, execute=execute)
