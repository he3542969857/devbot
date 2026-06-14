"""DevBot 技能注册表 —— 把四个 Agent 技能统一成一张可注册、可分发、可评测的技能表。

设计:
- **一张表 `SKILLS`**:review / codegen / testgen / requirement 同构注册,各带 name /
  描述 / 是否用 codedoc / 输入字段 / 统一 ``run(payload, llm, codedoc)`` 入口。
- **共享底座**:所有技能复用同一个带模型路由的 ``LlmClient`` 与同一个 ``CodedocClient``
  (MCP → codedoc 技能层);用得上图谱的技能(review/codegen)才注入 codedoc。
- **统一分发 `run_skill`**:Web API(``/api/v1/skill/{name}``)与 webhook 评论命令
  都经这里分发,一处实现多处消费(对齐 codedoc 那套 tools/skills 注册表的理念)。

review 是接到 webhook/异步 API 队列的**生产主线**;其余技能此前是悬空库函数,
本次统一接入注册表后,经 API / webhook 命令均可一致调用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .llm import LlmClient
from .codedoc_client import CodedocClient
from .review_agent import review_pr
from .codegen_agent import generate_code
from .testgen_agent import generate_tests
from .requirement_agent import analyze_requirement
from devbot_eval.domain import PRReviewInput


@dataclass
class Skill:
    name: str
    description: str
    uses_codedoc: bool
    run: Callable[..., dict]
    input_fields: dict


def _run_review(payload: dict, *, llm=None, codedoc=None) -> dict:
    pr = PRReviewInput(
        pr_id=str(payload.get("pr_id") or "pr"),
        diff=payload.get("diff", "") or "",
        impact_files=payload.get("impact_files", []) or [],
        title=payload.get("title", "") or "",
        description=payload.get("description", "") or "",
        language=payload.get("language", "java") or "java",
    )
    out = review_pr(pr, llm=llm, codedoc=codedoc)
    return {
        "risk_score": out.risk_score,
        "risk_level": out.risk_level.value,
        "summary": out.summary,
        "findings": [
            {"file": f.file, "line": f.line, "severity": f.severity,
             "message": f.message, "critic": getattr(f, "critic", "")}
            for cr in out.critics for f in cr.findings
        ],
        "critics": [{"critic": c.critic, "risk_score": c.risk_score,
                     "confidence": c.confidence, "findings": len(c.findings),
                     "suggestion": getattr(c, "suggestion", "")}
                    for c in out.critics],
        "total_tokens": out.total_tokens,
        "total_latency_ms": out.total_latency_ms,
    }


def _run_codegen(payload: dict, *, llm=None, codedoc=None) -> dict:
    return generate_code(payload.get("task", "") or "",
                         language=payload.get("language", "python") or "python",
                         repo=payload.get("repo", "") or "",
                         llm=llm, codedoc=codedoc)


def _run_testgen(payload: dict, *, llm=None, codedoc=None) -> dict:
    return generate_tests(diff=payload.get("diff", "") or "",
                          code=payload.get("code", "") or "",
                          language=payload.get("language", "python") or "python",
                          repo=payload.get("repo", "") or "",
                          llm=llm, codedoc=codedoc)


def _run_requirement(payload: dict, *, llm=None, codedoc=None) -> dict:
    return analyze_requirement(payload.get("text", "") or "", llm=llm)


SKILLS: dict[str, Skill] = {
    "review": Skill(
        "review", "PR 多 Critic 评审:4 Critic 并行评分 + 跨 Critic 去重 + 一票否决 + 置信度校准",
        True, _run_review,
        {"pr_id": "PR 标识", "diff": "统一 diff(必填)", "impact_files": "受影响文件(给 codedoc 拉影响子图)",
         "title": "PR 标题", "description": "PR 描述", "language": "语言"}),
    "codegen": Skill(
        "codegen", "检索接地代码生成:codedoc 语义检索取相似实现/真实API → 接地生成 → 语法检查 → 修复回路 → 终审",
        True, _run_codegen,
        {"task": "自然语言编码任务(必填)", "language": "目标语言", "repo": "接地参考的代码仓(可选,给了才检索)"}),
    "testgen": Skill(
        "testgen", "完整 UT 生成闭环:AST 抽真实分支/异常场景 → 生成 → 沙箱执行+coverage → 覆盖率驱动修复",
        True, _run_testgen,
        {"code": "被测代码(优先)", "diff": "或给 diff(抽新增代码)", "language": "语言",
         "repo": "接地参考的代码仓(可选)"}),
    "requirement": Skill(
        "requirement", "需求文本并行拆解为子任务 + 验收点 + 工作量",
        False, _run_requirement,
        {"text": "自由格式需求描述(必填)"}),
}


def list_skills() -> list[dict]:
    """列出全部技能(给 /api/v1/skills 与 webhook 帮助用)。"""
    return [{"name": s.name, "description": s.description,
             "uses_codedoc": s.uses_codedoc, "input_fields": s.input_fields}
            for s in SKILLS.values()]


def run_skill(name: str, payload: dict, *, llm: Optional[LlmClient] = None,
              codedoc: Optional[CodedocClient] = None) -> dict:
    """统一技能分发:Web API 与 webhook 命令都走这里。用得上 codedoc 的技能自动注入客户端。"""
    skill = SKILLS.get(name)
    if not skill:
        raise KeyError("unknown skill: %s" % name)
    cd: Any = None
    if skill.uses_codedoc:
        cd = codedoc or CodedocClient()
    return skill.run(payload or {}, llm=llm, codedoc=cd)


__all__ = ["Skill", "SKILLS", "list_skills", "run_skill"]
