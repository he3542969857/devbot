"""GitHub webhook handler — signature verification, event parsing, review dispatch."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from typing import Any

from devbot_eval.domain import PRReviewInput
from .config import get_settings, GithubCfg
from .github_client import GithubClient
from .review_agent import review_pr

logger = logging.getLogger(__name__)

# Events & actions we care about
_PR_ACTIONS = {"opened", "synchronize", "reopened"}

# Commands users can put in commit messages or PR body
_CMD_PATTERN = re.compile(r"/(review|ask|gen-test|gen|decompose|impl|complex|fix)")


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def verify_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature.

    ``signature_header`` looks like ``sha256=<hex>``.
    Returns True if the signature is valid.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_sig = signature_header[len("sha256="):]
    mac = hmac.new(secret.encode(), payload, hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), expected_sig)


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------

def parse_pr_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract owner/repo/pr_number from a pull_request event payload.

    Returns None if the action is not one we handle.
    """
    action = payload.get("action", "")
    if action not in _PR_ACTIONS:
        return None

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    full_name = repo.get("full_name", "")

    if "/" not in full_name:
        return None

    owner, repo_name = full_name.split("/", 1)

    return {
        "owner": owner,
        "repo": repo_name,
        "pr_number": pr.get("number", 0),
        "title": pr.get("title", ""),
        "body": pr.get("body", "") or "",
        "head_sha": pr.get("head", {}).get("sha", ""),
        "action": action,
    }


def parse_commands(text: str) -> list[str]:
    """Extract /review, /ask, /gen, /gen-test, /decompose commands from text."""
    return _CMD_PATTERN.findall(text)


# webhook 评论命令 -> 技能名(命令经统一技能注册表分发)
_COMMAND_SKILL = {"review": "review", "gen": "codegen",
                  "gen-test": "testgen", "decompose": "requirement",
                  "impl": "pr_codegen",            # 需求文档+PR → 接地生成/补全代码
                  "complex": "complex_codegen",    # 复杂需求 → 多组件生成
                  "fix": "review_fix"}             # 评审→修复→重评审 循环编排


def dispatch_command(command: str, payload: dict) -> dict:
    """把 webhook 命令(/review /gen /gen-test /decompose /impl /complex)经技能注册表执行——一处分发、与 API 同源。"""
    from .skills import run_skill
    name = _COMMAND_SKILL.get(command.lstrip("/"))
    if not name:
        return {"status": "ignored", "reason": "unknown command: %s" % command}
    return {"status": "ok", "command": command, "skill": name,
            "result": run_skill(name, payload or {})}


def _brief_result(result: dict) -> dict:
    """命令结果摘要(回贴 PR 评论用,避免巨长 body)。"""
    out = {"status": result.get("status"), "skill": result.get("skill")}
    r = result.get("result")
    if isinstance(r, dict):
        for k in ("verified", "risk_score", "risk_level", "note", "summary",
                  "repair_rounds", "auto_fixed", "test_count", "coverage"):
            if k in r:
                out[k] = r[k]
    return out


async def handle_comment_event(payload: dict[str, Any], cfg: GithubCfg | None = None) -> dict[str, Any]:
    """处理 PR 评论里的命令(/impl /complex /gen ...):解析命令→构造 payload→dispatch_command→回贴结果。"""
    cfg = cfg or get_settings().github
    issue = payload.get("issue") or {}
    if not issue.get("pull_request"):                       # 只处理 PR 上的评论
        return {"status": "ignored", "reason": "not a PR comment"}
    comment = (payload.get("comment") or {}).get("body", "") or ""
    cmds = parse_commands(comment)
    if not cmds:
        return {"status": "ignored", "reason": "no command"}

    repo_full = (payload.get("repository") or {}).get("full_name", "")
    if "/" not in repo_full:
        return {"status": "ignored", "reason": "no repo"}
    owner, repo = repo_full.split("/", 1)
    pr_number = issue.get("number", 0)
    cmd = cmds[0]
    client = GithubClient(cfg)

    # 需求 = 评论文本去掉命令本身;按需拉 PR diff 当上下文
    requirement = _CMD_PATTERN.sub("", comment).strip()
    diff = ""
    try:
        diff = client.get_pr_diff(owner, repo, pr_number)
    except Exception:
        diff = ""

    skill_payload = {
        "requirement": requirement, "text": requirement, "task": requirement,
        "description": requirement, "title": issue.get("title", ""),
        "diff": diff, "pr_id": f"{owner}/{repo}#{pr_number}", "impact_files": [],
    }
    try:
        result = dispatch_command("/" + cmd, skill_payload)
    except Exception as e:
        logger.exception("dispatch_command failed")
        result = {"status": "error", "error": str(e)[:200]}

    try:
        import json as _json
        body = "🤖 DevBot `/%s` 执行结果:\n\n```json\n%s\n```" % (
            cmd, _json.dumps(_brief_result(result), ensure_ascii=False)[:1500])
        client.post_issue_comment(owner, repo, pr_number, body)
    except Exception:
        logger.warning("post comment result failed for %s/%s#%d", owner, repo, pr_number,
                       exc_info=True)

    return {"status": "dispatched", "command": cmd,
            "pr": f"{owner}/{repo}#{pr_number}", "result_status": result.get("status")}


# ---------------------------------------------------------------------------
# Review dispatch
# ---------------------------------------------------------------------------

def format_review_body(output: Any) -> str:
    """Format a PRReviewOutput into a Markdown comment body."""
    lines = [
        f"## DevBot Review",
        f"",
        f"**Risk Score:** {output.risk_score}/100 ({output.risk_level.value})",
        f"",
        f"**Summary:** {output.summary}",
        f"",
    ]

    all_findings = []
    for cr in output.critics:
        for f in cr.findings:
            all_findings.append(f)

    if all_findings:
        lines.append("### Findings")
        lines.append("")
        for f in all_findings:
            loc = f"{f.file}"
            if f.line:
                loc += f":{f.line}"
            lines.append(f"- **[{f.severity.upper()}]** `{loc}` {f.message}")
        lines.append("")

    lines.append("---")
    lines.append(f"_Reviewed by devbot | tokens: {output.total_tokens} | latency: {output.total_latency_ms}ms_")

    return "\n".join(lines)


async def handle_pr_event(payload: dict[str, Any], cfg: GithubCfg | None = None) -> dict[str, Any]:
    """Full webhook handler: parse event, fetch diff, run review, post results.

    Returns a dict with status information.
    """
    cfg = cfg or get_settings().github

    parsed = parse_pr_event(payload)
    if parsed is None:
        return {"status": "ignored", "reason": "unhandled action"}

    owner = parsed["owner"]
    repo = parsed["repo"]
    pr_number = parsed["pr_number"]
    head_sha = parsed["head_sha"]

    client = GithubClient(cfg)

    # Post pending status
    client.post_check_status(owner, repo, head_sha, "pending", "DevBot review in progress...")

    try:
        # Fetch diff and PR info
        diff = client.get_pr_diff(owner, repo, pr_number)
        pr_info = client.get_pr_info(owner, repo, pr_number)

        # Build review input
        pr_input = PRReviewInput(
            pr_id=f"{owner}/{repo}#{pr_number}",
            diff=diff,
            impact_files=[],
            title=pr_info.get("title", parsed["title"]),
            description=pr_info.get("body", parsed["body"]),
            language="",
        )

        # Run the review
        result = review_pr(pr_input)

        # Format and post results
        body = format_review_body(result)

        # Collect findings for inline comments
        findings = []
        for cr in result.critics:
            for f in cr.findings:
                findings.append(f.to_dict())

        client.post_review_comment(owner, repo, pr_number, body, findings)

        # 回贴 autofix 修复建议(沙箱验证过的 suggested change);失败不影响评审回写
        suggestions = getattr(result, "auto_fix_suggestions", []) or []
        posted_fixes = 0
        if suggestions:
            try:
                posted_fixes = client.post_suggestions(owner, repo, pr_number, suggestions)
            except Exception:
                logger.warning("post_suggestions failed for %s/%s#%d", owner, repo, pr_number,
                               exc_info=True)

        # Post final status
        state = "success" if result.risk_score < 70 else "failure"
        client.post_check_status(
            owner, repo, head_sha, state,
            f"Risk: {result.risk_score}/100 ({result.risk_level.value})",
        )

        return {
            "status": "reviewed",
            "pr": f"{owner}/{repo}#{pr_number}",
            "risk_score": result.risk_score,
            "risk_level": result.risk_level.value,
            "auto_fixes_posted": posted_fixes,
        }

    except Exception as e:
        logger.exception("Failed to review PR %s/%s#%d", owner, repo, pr_number)
        client.post_check_status(owner, repo, head_sha, "error", f"DevBot error: {str(e)[:100]}")
        return {"status": "error", "error": str(e)}
