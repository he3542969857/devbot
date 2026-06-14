"""GitHub API client — fetch PR data, post review comments and check statuses.

Uses httpx for HTTP calls. Supports a mock mode when the token is empty or "mock".
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import GithubCfg, get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mock data (deterministic, used when token is empty or "mock")
# ---------------------------------------------------------------------------

_MOCK_DIFF = """\
diff --git a/src/main.py b/src/main.py
index 1234567..abcdefg 100644
--- a/src/main.py
+++ b/src/main.py
@@ -10,6 +10,8 @@ def main():
     config = load_config()
+    if config is None:
+        raise ValueError("config must not be None")
     run(config)
"""

_MOCK_PR_INFO: dict[str, Any] = {
    "number": 1,
    "title": "fix: add null-check for config",
    "body": "Prevents crash when config file is missing.",
    "head": {"sha": "abcdef1234567890abcdef1234567890abcdef12"},
    "changed_files": 1,
    "additions": 2,
    "deletions": 0,
    "state": "open",
    "user": {"login": "mock-user"},
}


def _is_mock(cfg: GithubCfg) -> bool:
    return cfg.token == "" or cfg.token == "mock"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class GithubClient:
    """Thin wrapper around GitHub REST API v3."""

    def __init__(self, cfg: GithubCfg | None = None):
        self.cfg = cfg or get_settings().github
        self._base = self.cfg.api_url.rstrip("/")
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if not _is_mock(self.cfg):
            self._headers["Authorization"] = f"Bearer {self.cfg.token}"

    # -- helpers -------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _get(self, path: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        merged = {**self._headers, **(headers or {})}
        resp = httpx.get(self._url(path), headers=merged, timeout=30)
        resp.raise_for_status()
        return resp

    def _post(self, path: str, json: Any = None) -> httpx.Response:
        resp = httpx.post(self._url(path), headers=self._headers, json=json, timeout=30)
        resp.raise_for_status()
        return resp

    # -- public API ----------------------------------------------------------

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Fetch the unified diff for a pull request."""
        if _is_mock(self.cfg):
            return _MOCK_DIFF

        resp = self._get(
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return resp.text

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        """Fetch PR metadata (title, description, changed_files, head sha, etc.)."""
        if _is_mock(self.cfg):
            return {**_MOCK_PR_INFO, "number": pr_number}

        resp = self._get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
        data = resp.json()
        return {
            "number": data["number"],
            "title": data.get("title", ""),
            "body": data.get("body", ""),
            "head": {"sha": data["head"]["sha"]},
            "changed_files": data.get("changed_files", 0),
            "additions": data.get("additions", 0),
            "deletions": data.get("deletions", 0),
            "state": data.get("state", ""),
            "user": {"login": data.get("user", {}).get("login", "")},
        }

    def post_review_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        findings: list[dict[str, Any]] | None = None,
    ) -> None:
        """Post a PR review with optional inline comments on specific lines."""
        if _is_mock(self.cfg):
            logger.info("mock: would post review comment on %s/%s#%d", owner, repo, pr_number)
            return

        comments: list[dict[str, Any]] = []
        for f in (findings or []):
            if f.get("file") and f.get("line"):
                comments.append({
                    "path": f["file"],
                    "line": f["line"],
                    "body": f"**[{f.get('severity', 'info').upper()}]** {f.get('message', '')}",
                })

        payload: dict[str, Any] = {
            "body": body,
            "event": "COMMENT",
        }
        if comments:
            payload["comments"] = comments

        self._post(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", json=payload)

    def post_check_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        state: str,
        description: str = "",
    ) -> None:
        """Create a commit status (pending / success / failure / error)."""
        if _is_mock(self.cfg):
            logger.info("mock: would post status %s on %s/%s@%s", state, owner, repo, sha)
            return

        self._post(
            f"/repos/{owner}/{repo}/statuses/{sha}",
            json={
                "state": state,
                "description": description[:140],
                "context": "devbot/review",
            },
        )
