"""review→fix 循环编排 —— 跨技能 meta 状态机(LangGraph 有环图)。

把 review + 修复技能串成"改了再评、不行再改"的**不确定迭代流**:
单技能是线性流水线串不了这种(改几轮、要不要继续由结果定);所以用**有环状态机**。

    START → review ──(无高危 finding | 到轮数上限)──> END
              ↑                                    │
              └──────── fix(autofix/pr_codegen) ←──┘(有高危 finding 且未到上限)

收敛条件:评审无高危 finding(干净)或到 max_rounds。与 codedoc 多仓 reflect↔refine 同形:
有环 + 有界(max_rounds)+ 收敛即出。reviewer / fixer 可插拔(生产包真技能,测试注入确定性)。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

MAX_ROUNDS = 2


class FixLoopState(TypedDict, total=False):
    requirement: str
    code: str                                   # 当前代码(每轮 fix 后演进)
    round: int
    findings: list[Any]
    suggestions: list[Any]
    history: Annotated[list[Any], operator.add]  # 各轮 finding 数(reducer 累加)
    done_reason: str


def build_fix_loop_graph(reviewer: Callable, fixer: Callable, max_rounds: int = MAX_ROUNDS):
    """reviewer(code, requirement) -> {findings, suggestions};fixer(code, findings, suggestions, requirement) -> new_code。"""

    def review_node(state: FixLoopState) -> dict:
        r = reviewer(state.get("code", ""), state.get("requirement", "")) or {}
        f = r.get("findings", []) or []
        return {"findings": f, "suggestions": r.get("suggestions", []) or [],
                "history": [{"round": state.get("round", 0), "high_findings": len(f)}]}

    def fix_node(state: FixLoopState) -> dict:
        new_code = fixer(state.get("code", ""), state.get("findings", []),
                         state.get("suggestions", []), state.get("requirement", ""))
        return {"code": new_code or state.get("code", ""), "round": state.get("round", 0) + 1}

    def route(state: FixLoopState) -> str:
        if not state.get("findings"):
            return "done"                       # 收敛:无高危 finding
        if state.get("round", 0) >= max_rounds:
            return "done"                       # 有界:到轮数上限
        return "fix"

    g = StateGraph(FixLoopState)
    g.add_node("review", review_node)
    g.add_node("fix", fix_node)
    g.add_edge(START, "review")
    g.add_conditional_edges("review", route, {"fix": "fix", "done": END})
    g.add_edge("fix", "review")                 # ← 有环:修完回评审
    return g.compile()


def run_fix_loop(requirement: str, code: str, reviewer: Callable, fixer: Callable,
                 max_rounds: int = MAX_ROUNDS) -> dict[str, Any]:
    graph = build_fix_loop_graph(reviewer, fixer, max_rounds)
    final = graph.invoke({"requirement": requirement, "code": code, "round": 0},
                         {"recursion_limit": 2 * max_rounds + 5})
    clean = not final.get("findings")
    return {
        "final_code": final.get("code", ""),
        "rounds": final.get("round", 0),
        "converged_clean": clean,
        "remaining_findings": len(final.get("findings", []) or []),
        "history": final.get("history", []),
        "done_reason": "clean" if clean else "max_rounds",
    }


# ── 生产适配:reviewer=review_pr,fixer=autofix ──
def _as_added_diff(code: str, path: str = "solution.py") -> str:
    lines = (code or "").splitlines()
    head = ("diff --git a/%s b/%s\nnew file mode 100644\n--- /dev/null\n+++ b/%s\n@@ -0,0 +1,%d @@\n"
            % (path, path, path, len(lines)))
    return head + "\n".join("+" + ln for ln in lines) + "\n"


class ProdLoop:
    """生产 reviewer/fixer:review_pr 找高危 finding,autofix 生成沙箱验证过的修复并应用。"""

    def __init__(self, llm: Any, codedoc: Any = None):
        self.llm, self.codedoc = llm, codedoc

    def review(self, code: str, requirement: str) -> dict:
        from devbot_eval.domain import PRReviewInput
        from .review_agent import review_pr
        out = review_pr(PRReviewInput(pr_id="fixloop", diff=_as_added_diff(code),
                                      title=requirement, description=requirement))
        high = [{"file": f.file, "line": f.line, "severity": f.severity, "message": f.message}
                for f in out.findings if (f.severity or "").lower() == "error"]
        return {"findings": high, "suggestions": getattr(out, "auto_fix_suggestions", []) or []}

    def fix(self, code: str, findings: list, suggestions: list, requirement: str) -> str:
        from .auto_fix import AutoFix, Finding, LlmFixGenerator
        if not findings:
            return code
        f = findings[0]
        r = AutoFix(LlmFixGenerator(self.llm, self.codedoc)).run(
            Finding(message=f.get("message", ""), file=f.get("file", "solution.py") or "solution.py",
                    line=int(f.get("line") or 0), severity=8),
            code, check_code="import solution\n")
        return r.fixed_code if r.verified else code


def run_fix_loop_prod(requirement: str, code: str, llm: Any, codedoc: Any = None,
                      max_rounds: int = MAX_ROUNDS) -> dict[str, Any]:
    p = ProdLoop(llm, codedoc)
    return run_fix_loop(requirement, code, p.review, p.fix, max_rounds=max_rounds)
