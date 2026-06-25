"""LangGraph PR Review Agent — 多 Critic 并行扇出 + 加权聚合 + finding 去噪 + 置信度校准。

    START → prep_context → Send(critic × 4, 并行) → aggregate → END

本会话定型的质量修复(默认即生效,无需环境变量):
- **领域纪律 prompt**:每个 Critic 只报本域、不重复他域、必带行号 → 视角正交。
- **severity rubric**:error 只给安全漏洞/确定崩溃,设计/可维护/潜在 → warn(压 strict-FP)。
- **_dedup_findings**:跨 Critic 按 file+line±DEDUP_WINDOW 聚簇、留最高 severity 一条,写回
  critic["findings"](metric / PR 评论 / API 都读这个字段,一处去重处处受益)。
- **Platt 后置校准**:confidence = sigmoid(_CAL_A·risk/100 + _CAL_B),留出 ECE 0.37→0.24。
可选(默认关):DEVBOT_CRITIC_SAMPLES>1 自一致性采样;DEVBOT_VERIFY_ENABLE=1 逐条 verifier。
"""
from __future__ import annotations

import json
import math
import operator
import os
import textwrap
import time
from collections import Counter
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from devbot_eval.domain import (
    CriticResult, Finding, PRReviewInput, PRReviewOutput, RiskLevel,
)
from .codedoc_client import CodedocClient
from .sandbox import run_diff_exec_check, extract_added_python
from .testgen_agent import generate_tests
from .config import get_settings
from .llm import LlmClient

CRITIC_NAMES = ["correctness", "design", "security", "readability"]

CRITIC_WEIGHTS = {
    "correctness": 0.40,
    "design": 0.20,
    "security": 0.30,
    "readability": 0.10,
}

VETO_THRESHOLD = 80

# ── 去噪 / 校准参数 ──────────────────────────────────────────────────
from .findings import dedup_entries, calibrated_confidence as _cc_pure
_SEVERITY_ORDER = {"error": 3, "warn": 2, "warning": 2, "info": 1}
_DEDUP_WINDOW = int(os.environ.get("DEVBOT_DEDUP_WINDOW", "3"))
_CALIBRATE = os.environ.get("DEVBOT_CALIBRATE", "1") == "1"
_CAL_A = 3.82   # Platt 系数(5 折 CV 拟合,留出 ECE 0.249)
_CAL_B = -1.42
_SAMPLES = int(os.environ.get("DEVBOT_CRITIC_SAMPLES", "1"))
_VERIFY = os.environ.get("DEVBOT_VERIFY_ENABLE", "0") == "1"
_PREFILTER = int(os.environ.get("DEVBOT_PREFILTER", "40"))
# 风险下限:Critic 把代码判为本质无虞(risk<floor)时不出 finding —— 压干净代码上的投机过报
_FINDING_RISK_FLOOR = int(os.environ.get("DEVBOT_FINDING_RISK_FLOOR", "35"))
# 丢掉 info 级 finding:info=提示/建议非缺陷,缺陷一律 error/warn(压投机噪声,不伤缺陷召回)
_DROP_INFO = os.environ.get("DEVBOT_DROP_INFO", "1") == "1"
# auto-fix:对高危 finding 产出沙箱验证过的修复建议(codegen 引擎的真实产品形态)。
# 默认开,但全程 try/except 包住——失败一律降级空,绝不影响评审主流程。
_AUTOFIX = os.environ.get("DEVBOT_AUTOFIX_ENABLE", "1") == "1"
_AUTOFIX_SEVERITY = int(os.environ.get("DEVBOT_AUTOFIX_SEVERITY", "7"))

_DISCIPLINE = (
    "\n\n## Reviewer discipline\n"
    "You are the {name} reviewer ONLY. Report ONLY {name} issues. Do NOT duplicate the "
    "concerns of the other reviewers (correctness / design / security / readability are "
    "separate reviewers, each handling its own axis). Always include a line number for each "
    "finding.\n"
    "Report a finding ONLY for a concrete, demonstrable defect introduced by the changed (+) "
    "lines on your axis. If the change introduces no real {name} defect, return an empty "
    "findings list (findings: []). Do NOT pad with speculative 'potential' problems, "
    "missing-docstring/comment notes, or pure style preferences — those are not findings.\n"
    "## Severity rubric\n"
    "Use 'error' ONLY for security vulnerabilities or certain crashes / data loss. "
    "Design, maintainability, or genuinely *potential* issues are 'warn'. Trivia is 'info'.\n"
    "## Risk scoring\n"
    "Score risk_score for the WORST defect on your axis:\n"
    "- An EXPLOITABLE security vulnerability (SQL/command injection, auth bypass, RCE) OR a "
    "CERTAIN crash / data loss (out-of-bounds that will throw, null deref on a reachable path) "
    "=> 80-95.\n"
    "- A security WEAKNESS not directly exploitable (weak/broken crypto like MD5/SHA1/DES for "
    "security, disabled TLS verification, secret in logs) OR a LIKELY-but-not-certain crash bug "
    "(TOCTOU/race, off-by-one, resource leak, unchecked cast, missing error handling) => 60-75.\n"
    "- A contained/moderate issue (one design smell, a minor logic bug) => 35-55.\n"
    "- Genuinely clean code on your axis => below 25.\n"
    "Report findings ONLY on the file shown in the diff header; never cite another file. "
    "Do not inflate clean refactors; do not under-rate confirmed security weaknesses."
)

CRITIC_PROMPTS = {
    "correctness": textwrap.dedent("""\
        You are a code correctness reviewer. Analyze the diff for:
        - Logic errors, off-by-one, null/None dereferences
        - Resource leaks (unclosed streams, connections, locks)
        - Boundary conditions and edge cases
        - Concurrency issues (race conditions, deadlocks)
        - Error handling gaps (uncaught exceptions, swallowed errors)
    """),
    "design": textwrap.dedent("""\
        You are a software design reviewer. Analyze the diff for:
        - SOLID principle violations
        - Inappropriate coupling or god classes
        - Missing abstractions or premature abstractions
        - API design issues (unclear contracts, breaking changes)
        - Naming and structure clarity
    """),
    "security": textwrap.dedent("""\
        You are a security reviewer. Analyze the diff for:
        - SQL injection, XSS, command injection
        - Authentication / authorization gaps
        - Sensitive data exposure (secrets, PII in logs)
        - Insecure deserialization or file operations
        - Dependency vulnerabilities if imports changed
    """),
    "readability": textwrap.dedent("""\
        You are a readability reviewer. Analyze the diff for:
        - Unclear variable/function names
        - Overly complex expressions or deeply nested logic
        - Missing or misleading comments
        - Inconsistent code style
        - Dead code or unnecessary complexity
    """),
}


def _sigmoid(x: float) -> float:
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def _calibrated_confidence(risk_score: float) -> float:
    return _cc_pure(risk_score, _CAL_A, _CAL_B)


def _line_close(a, b, w: int) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(int(a) - int(b)) <= w
    except Exception:
        return False


def _consensus_findings(samples_findings: list[list], min_count: int) -> list[dict]:
    """自一致性共识:跨多次采样按 file+line±2 聚簇,只留出现在 >=min_count 次采样里的(留最高 severity)。
    投机 finding 逐次变化被滤掉,稳定的真缺陷保留。"""
    items = [(si, f) for si, fl in enumerate(samples_findings) for f in fl if isinstance(f, dict)]
    used = [False] * len(items)
    kept = []
    for i, (si, f) in enumerate(items):
        if used[i]:
            continue
        used[i] = True
        seen = {si}
        rep = f
        for j in range(i + 1, len(items)):
            if used[j]:
                continue
            sj, fj = items[j]
            if fj.get("file") == f.get("file") and _line_close(f.get("line"), fj.get("line"), 2):
                used[j] = True
                seen.add(sj)
                if _SEVERITY_ORDER.get((fj.get("severity") or "info").lower(), 1) > \
                   _SEVERITY_ORDER.get((rep.get("severity") or "info").lower(), 1):
                    rep = fj
        if len(seen) >= min_count:
            kept.append(rep)
    return kept


def _dedup_findings(results: list[dict[str, Any]]) -> int:
    """跨 Critic 去重:同 file + line±window 聚簇,留最高 severity 一条,写回 critic['findings']。
    返回去掉的重复数。纯去重逻辑见 findings.dedup_entries(已单测)。"""
    entries = [(ci, f) for ci, cr in enumerate(results)
               for f in cr.get("findings", []) if isinstance(f, dict)]
    kept, dropped = dedup_entries(entries, _DEDUP_WINDOW)
    new: dict[int, list] = {ci: [] for ci in range(len(results))}
    for ci, f in kept:
        new[ci].append(f)
    for ci, cr in enumerate(results):
        cr["findings"] = new[ci]
    return dropped


class ReviewState(TypedDict, total=False):
    pr_input: dict[str, Any]
    impact_summary: str
    impact_nodes: list[dict[str, Any]]
    critic_name: str
    critic_results: Annotated[list[dict[str, Any]], operator.add]
    exec_result: dict[str, Any]
    final_output: dict[str, Any]


def build_review_graph(llm: LlmClient | None = None, codedoc: CodedocClient | None = None):
    llm = llm or LlmClient()
    codedoc = codedoc or CodedocClient()

    def prep_context(state: ReviewState) -> dict[str, Any]:
        pr = state["pr_input"]
        impact = codedoc.get_impact(pr.get("impact_files", []))
        return {"impact_summary": impact.summary, "impact_nodes": impact.affected_nodes}

    def _run_critic_once(name, system_prompt, user_msg, temperature):
        t0 = time.time()
        resp = llm.chat(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_msg}],
            model_key=name, max_tokens=800, temperature=temperature)
        parsed = _parse_critic_output(resp.text)
        return parsed, resp, int((time.time() - t0) * 1000)

    def critic(state: ReviewState) -> dict[str, Any]:
        name = state["critic_name"]
        pr = state["pr_input"]
        diff = pr.get("diff", "")
        impact = state.get("impact_summary", "")
        system_prompt = CRITIC_PROMPTS[name] + _DISCIPLINE.format(name=name) + textwrap.dedent(f"""

            ## Impact context (from codedoc)
            {impact}

            ## Line numbers
            In the diff below every code line is prefixed with `Lnnn:` = its line number in the NEW
            file. When you report a finding's `line`, copy that exact L number of the offending line.
            Do NOT guess line numbers.

            ## Output format
            Respond with EXACTLY this JSON:
            ```json
            {{
              "risk_score": <0-100>,
              "confidence": <0.0-1.0>,
              "findings": [
                {{"file": "<path>", "line": <number_or_null>, "severity": "info|warn|error", "message": "<description>"}}
              ],
              "suggestion": "<one paragraph improvement suggestion>"
            }}
            ```
        """)
        user_msg = f"PR Title: {pr.get('title', '')}\n\nDiff:\n```\n{_annotate_diff(diff)[:6000]}\n```"

        try:
            n = max(1, _SAMPLES)
            samples = [_run_critic_once(name, system_prompt, user_msg, 0.2 if n == 1 else 0.5)
                       for _ in range(n)]
            risks = [s[0].get("risk_score", 50) for s in samples]
            mean_risk = sum(risks) / len(risks)
            parsed, resp, lat = min(samples, key=lambda s: abs(s[0].get("risk_score", 50) - mean_risk))
            risk_score = int(round(mean_risk))
            if n > 1:                                  # 自一致性:置信度 = 风险档位一致率
                tiers = [RiskLevel.from_score(r).value for r in risks]
                _, cnt = Counter(tiers).most_common(1)[0]
                confidence = round(cnt / len(tiers), 4)
            else:
                confidence = float(parsed.get("confidence", 0.5))
            if _CALIBRATE:                             # Platt 后置校准(默认开)
                confidence = _calibrated_confidence(risk_score)
            if n > 1:                                  # 自一致性共识过滤(滤投机、留稳定)
                findings = _consensus_findings([s[0].get("findings", []) for s in samples], min_count=n)
            else:
                findings = [f for f in parsed.get("findings", []) if isinstance(f, dict)]
            if _PREFILTER and len(findings) > _PREFILTER:
                findings = findings[:_PREFILTER]
            result = {
                "critic": name, "risk_score": risk_score, "confidence": confidence,
                "findings": findings, "suggestion": parsed.get("suggestion", ""),
                "model": resp.model, "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out,
                "latency_ms": lat, "error": None,
            }
        except Exception as e:
            result = {"critic": name, "risk_score": 50, "confidence": 0.0, "findings": [],
                      "suggestion": "", "model": "", "tokens_in": 0, "tokens_out": 0,
                      "latency_ms": 0, "error": str(e)}
        return {"critic_results": [result]}

    def aggregate(state: ReviewState) -> dict[str, Any]:
        pr = state["pr_input"]
        results = state.get("critic_results", [])
        for cr in results:                             # 风险下限门控:判定无虞的 Critic 不出 finding
            if cr.get("risk_score", 50) < _FINDING_RISK_FLOOR:
                cr["findings"] = []
            elif _DROP_INFO:                           # 丢 info 级(提示非缺陷)
                cr["findings"] = [f for f in cr.get("findings", [])
                                  if (f.get("severity") or "").lower() not in ("info", "")]
        dropped = _dedup_findings(results)             # 去噪:写回 critic['findings']
        if _VERIFY:
            _verify_findings(results, pr, llm)

        # 沙箱实跑作为客观 critic 并入。诚实定位:可靠信号是"崩不崩"(冒烟);
        # 自动生成测试只是参考(可能本身有误)、不驱动风险、不报 finding。
        er = state.get("exec_result") or {}
        if er.get("ran"):
            cov = er.get("coverage")
            _tp, _tt = er.get("tests_passed", 0), er.get("tests_total", 0)
            _cnote = ("自动测试 %d/%d、覆盖率 %s%%(参考:测试为生成,覆盖率表征新代码可测性)" % (_tp, _tt, cov)
                      if _tt else "")
            if er.get("smoke_ok") is False and er.get("kind") == "crash":
                ef = [{"file": "<sandbox>", "line": None, "severity": "error",
                       "message": "沙箱实跑:新增代码 import/执行即崩 — %s" % (er.get("error") or "")[:200]}]
                erisk, sug = 70, "实跑崩溃"
            elif er.get("smoke_ok") is False:
                ef, erisk, sug = [], 5, "实跑跳过:缺依赖/非自包含(需完整仓 checkout 才能实跑)"
            elif _tt and cov is not None and cov < 50:
                ef = [{"file": "<sandbox>", "line": None, "severity": "warn",
                       "message": "沙箱实跑:新增代码自动测试覆盖率仅 %s%%(分支多/难覆盖,建议补测或审视复杂度)" % cov}]
                erisk, sug = 45, _cnote
            else:
                ef, erisk = [], 5
                sug = "实跑通过(import/执行无崩)" + ("; " + _cnote if _cnote else "")
            results = results + [{"critic": "exec", "risk_score": erisk, "confidence": 1.0,
                                  "findings": ef, "suggestion": sug,
                                  "model": "sandbox", "tokens_in": 0, "tokens_out": 0,
                                  "latency_ms": 0, "error": None}]

        weighted_score = total_weight = 0.0
        veto = False
        all_findings: list[dict[str, Any]] = []
        total_tokens = total_latency = 0
        for cr in results:
            name = cr["critic"]
            w = CRITIC_WEIGHTS.get(name, 0.1)
            weighted_score += cr["risk_score"] * w
            total_weight += w
            if cr["risk_score"] >= VETO_THRESHOLD:
                veto = True
            for f in cr.get("findings", []):
                if isinstance(f, dict):
                    f["critic"] = name
                    all_findings.append(f)
            total_tokens += cr.get("tokens_in", 0) + cr.get("tokens_out", 0)
            total_latency = max(total_latency, cr.get("latency_ms", 0))

        final_score = int(weighted_score / total_weight) if total_weight else 50
        if veto:
            final_score = max(final_score, VETO_THRESHOLD)
        risk_level = RiskLevel.from_score(final_score)
        summary = " | ".join(
            f"{cr['critic']}: {cr['risk_score']} ({RiskLevel.from_score(cr['risk_score']).value})"
            for cr in results)
        if veto:
            summary += " [VETO triggered]"
        summary += f" | deduped {dropped} dup findings"

        # auto-fix:对高危 finding 生成沙箱验证过的修复建议(只贴验证通过的)。
        # 全程 try/except,失败降级空、绝不影响评审;实际回贴 GitHub suggested change 由 webhook 层做。
        auto_fix_suggestions: list[dict[str, Any]] = []
        if _AUTOFIX and all_findings:
            try:
                from .review_autofix import auto_fix_review
                from .auto_fix import Finding as _AFinding, LlmFixGenerator
                added = extract_added_python(pr.get("diff", ""))
                if added.strip():
                    af = [_AFinding(message=f.get("message", ""),
                                    file=f.get("file", "solution.py") or "solution.py",
                                    line=int(f.get("line") or 0),
                                    severity=8 if (f.get("severity") == "error") else 5,
                                    critic=f.get("critic", ""))
                          for f in all_findings if isinstance(f, dict)]
                    afr = auto_fix_review(af, added, "import solution\n",
                                          LlmFixGenerator(llm, codedoc),
                                          severity_threshold=_AUTOFIX_SEVERITY)
                    auto_fix_suggestions = afr.get("suggestions", [])
            except Exception:
                auto_fix_suggestions = []

        return {"final_output": {
            "pr_id": pr.get("pr_id", ""), "risk_score": final_score, "risk_level": risk_level.value,
            "critics": results, "findings": all_findings, "summary": summary,
            "total_tokens": total_tokens, "total_cost_usd": 0.0, "total_latency_ms": total_latency,
            "exec_check": (state.get("exec_result") or {}),
            "auto_fix_suggestions": auto_fix_suggestions,
        }}

    def exec_check(state: ReviewState) -> dict[str, Any]:
        diff = state["pr_input"].get("diff", "")
        try:
            res = run_diff_exec_check(diff, None)  # 冒烟(可靠的崩不崩信号)
        except Exception as e:  # noqa: BLE001
            return {"exec_result": {"ran": False, "note": "exec error: %s" % str(e)[:120]}}
        # 自包含新增函数跑通后,用完整 testgen 测真实覆盖率(可测性信号)
        if res.get("ran") and res.get("smoke_ok"):
            try:
                code = extract_added_python(diff)
                tg = generate_tests(code=code, language="python", llm=llm, execute=True)
                res["coverage"] = tg.get("coverage")
                res["tests_passed"] = tg.get("tests_passed", 0)
                res["tests_total"] = tg.get("tests_total", 0)
            except Exception:  # noqa: BLE001
                pass
        return {"exec_result": res}

    def fan_out_critics(state: ReviewState) -> list[Send]:
        sends = [Send("critic", {**state, "critic_name": name}) for name in CRITIC_NAMES]
        sends.append(Send("exec_check", dict(state)))  # 与 Critic 并行实跑
        return sends

    graph = StateGraph(ReviewState)
    graph.add_node("prep_context", prep_context)
    graph.add_node("critic", critic)
    graph.add_node("exec_check", exec_check)
    graph.add_node("aggregate", aggregate)
    graph.add_edge(START, "prep_context")
    graph.add_conditional_edges("prep_context", fan_out_critics, ["critic", "exec_check"])
    graph.add_edge("critic", "aggregate")
    graph.add_edge("exec_check", "aggregate")
    graph.add_edge("aggregate", END)
    return graph.compile()


def _annotate_diff(diff: str) -> str:
    """给 diff 的每行标上"新文件行号"(Lnnn:),让 Critic 引用准确行号、与 GT 对齐。
    解析 @@ -a,b +c,d @@ 取新文件起始行;+ 与上下文行推进计数,- 行不推进。"""
    import re as _re
    out, new_ln = [], 0
    for line in (diff or "").splitlines():
        if line.startswith("@@"):
            m = _re.search(r"\+(\d+)", line)
            new_ln = int(m.group(1)) if m else new_ln
            out.append(line)
        elif line.startswith("+++") or line.startswith("---"):
            out.append(line)
        elif line.startswith("+"):
            out.append("L%d: %s" % (new_ln, line))
            new_ln += 1
        elif line.startswith("-"):
            out.append("    %s" % line)          # 删除行不占新文件行号
        else:
            out.append("L%d: %s" % (new_ln, line))  # 上下文行
            new_ln += 1
    return "\n".join(out)


def _parse_critic_output(text: str) -> dict[str, Any]:
    import re
    for pat in (r"```json\s*(\{.*?\})\s*```", r"(\{.*\})"):
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return {"risk_score": 50, "confidence": 0.3, "findings": [], "suggestion": text[:200]}


def _verify_findings(results, pr, llm) -> None:
    """可选(默认关):逐条 verifier 砍误报。评测显示它无差别砍真报、降 recall,故默认关。"""
    diff = pr.get("diff", "")[:4000]
    for cr in results:
        kept = []
        for f in cr.get("findings", []):
            try:
                resp = llm.chat([
                    {"role": "system", "content": "You are a strict verifier. Given a diff and a "
                     "claimed finding, answer JSON {\"real\": true|false}. Default real=false if unsure."},
                    {"role": "user", "content": f"Diff:\n{diff}\n\nClaimed finding: {json.dumps(f)}"},
                ], model_key="default", max_tokens=50, temperature=0.0)
                if _parse_critic_output(resp.text).get("real", True):
                    kept.append(f)
            except Exception:
                kept.append(f)
        cr["findings"] = kept


def review_pr(pr: PRReviewInput, *, llm: LlmClient | None = None,
              codedoc: CodedocClient | None = None) -> PRReviewOutput:
    """跑完整多 Critic 评审,返回 PRReviewOutput。生产默认=去重+rubric+校准、verifier 关。"""
    graph = build_review_graph(llm=llm, codedoc=codedoc)
    result = graph.invoke({
        "pr_input": {
            "pr_id": pr.pr_id, "diff": pr.diff, "impact_files": pr.impact_files,
            "title": pr.title, "description": pr.description, "language": pr.language,
        },
        "critic_results": [],
    })
    out = result["final_output"]
    critics = [
        CriticResult(
            critic=cr["critic"], risk_score=cr["risk_score"], confidence=cr["confidence"],
            findings=[Finding(**{k: v for k, v in f.items()
                                 if k in ("file", "line", "severity", "message", "critic")})
                      for f in cr.get("findings", []) if isinstance(f, dict)],
            suggestion=cr.get("suggestion", ""), model=cr.get("model", ""),
            tokens_in=cr.get("tokens_in", 0), tokens_out=cr.get("tokens_out", 0),
            latency_ms=cr.get("latency_ms", 0), error=cr.get("error"),
        )
        for cr in out.get("critics", [])
    ]
    out_obj = PRReviewOutput(
        pr_id=out["pr_id"], risk_score=out["risk_score"], risk_level=RiskLevel(out["risk_level"]),
        critics=critics, summary=out.get("summary", ""), total_tokens=out.get("total_tokens", 0),
        total_cost_usd=out.get("total_cost_usd", 0.0), total_latency_ms=out.get("total_latency_ms", 0),
    )
    # 把 autofix 修复建议挂到返回对象(PRReviewOutput 无此字段,动态附加;失败不影响主结果)
    try:
        out_obj.auto_fix_suggestions = out.get("auto_fix_suggestions", []) or []
    except Exception:
        pass
    return out_obj
