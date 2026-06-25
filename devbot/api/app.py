"""DevBot Platform Server — PR Review with Auth, Queue, History, and Professional UI.

Features:
- JWT + SQLite user authentication (bcrypt password hashing)
- Async review queue with threading + progress tracking
- Review history with pagination
- Professional Vue 3 + Tailwind CSS single-page application (Chinese UI)
- GitHub webhook integration
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
# ----------------------------------------------------------------------
# PostgreSQL compatibility shim (replaces sqlite3 module)
# ----------------------------------------------------------------------
import re as _re_pg
import psycopg as _psycopg
from psycopg.rows import dict_row as _dict_row
import os as _os_pg

PG_DSN = _os_pg.environ.get(
    "DEVPLATFORM_PG_DSN",
    "host=127.0.0.1 port=5432 dbname=devplatform user=devplatform password=devplatform-pwd-2026",
)

class _SQLiteCompatCursor:
    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid
    @property
    def rowcount(self):
        return self._cur.rowcount
    def fetchone(self):
        try:
            return self._cur.fetchone()
        except _psycopg.ProgrammingError:
            return None
    def fetchall(self):
        try:
            return self._cur.fetchall()
        except _psycopg.ProgrammingError:
            return []
    def __iter__(self):
        return iter(self._cur)

_INSERT_RE_PG = _re_pg.compile(r"^\s*INSERT\s+INTO\s+", _re_pg.IGNORECASE)

class _SQLiteCompatConn:
    def __init__(self):
        self._conn = _psycopg.connect(PG_DSN, row_factory=_dict_row, autocommit=False)
    def execute(self, sql, params=()):
        pg_sql = sql.replace("?", "%s")
        if pg_sql.strip().upper().startswith("PRAGMA"):
            class _N:
                lastrowid = None
                rowcount = 0
                def fetchone(self_): return None
                def fetchall(self_): return []
                def __iter__(self_): return iter(())
            return _N()
        cur = self._conn.cursor()
        lastrowid = None
        if _INSERT_RE_PG.match(pg_sql) and "RETURNING" not in pg_sql.upper():
            pg_sql_ret = pg_sql.rstrip().rstrip(";") + " RETURNING id"
            try:
                cur.execute(pg_sql_ret, params)
                row = cur.fetchone()
                if row:
                    lastrowid = row.get("id") if isinstance(row, dict) else row[0]
            except _psycopg.errors.UndefinedColumn:
                self._conn.rollback()
                cur = self._conn.cursor()
                cur.execute(pg_sql, params)
        else:
            cur.execute(pg_sql, params)
        return _SQLiteCompatCursor(cur, lastrowid=lastrowid)
    def executescript(self, script):
        return None
    def commit(self):
        self._conn.commit()
    def rollback(self):
        self._conn.rollback()
    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

class _SQLite3ModuleShim:
    IntegrityError = _psycopg.errors.UniqueViolation
    OperationalError = _psycopg.errors.OperationalError
    Row = dict
    @staticmethod
    def connect(*_a, **_kw):
        return _SQLiteCompatConn()

sqlite3 = _SQLite3ModuleShim()

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from devbot_eval.domain import PRReviewInput
from ..config import get_settings
from ..github_webhook import handle_pr_event, handle_comment_event, verify_signature
from ..review_agent import review_pr
from ..skills import list_skills, run_skill, SKILLS

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DB_PATH = "/home/ubuntu/apps/devbot/users.db"
JWT_SECRET = os.environ.get("DEVBOT_JWT_SECRET", "devbot-jwt-secret-key-2024-secure-enough-for-hmac256")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 72

app = FastAPI(title="devbot", version="0.2.0", root_path="/devbot")
security = HTTPBearer(auto_error=False)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SQLite Setup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_db_lock = threading.Lock()


def _get_db():
    """PostgreSQL-backed connection (sqlite3-compatible API via _SQLiteCompatConn)."""
    return _SQLiteCompatConn()


def _init_db():
    """Schema is provisioned in PostgreSQL externally; no-op here."""
    return None


_init_db()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Review Task Queue (in-memory)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_tasks: dict[str, dict[str, Any]] = {}
_tasks_lock = threading.Lock()


def _run_review_task(task_id: str, pr_input: PRReviewInput, user_id: int):
    """Run the review in a background thread."""
    try:
        with _tasks_lock:
            _tasks[task_id]["status"] = "running"

        out = review_pr(pr_input)

        critics_data = []
        for c in out.critics:
            critics_data.append({
                "critic": c.critic,
                "risk_score": c.risk_score,
                "confidence": c.confidence,
                "model": c.model,
                "findings_count": len(c.findings),
                "findings": [
                    {"file": f.file, "line": f.line, "severity": f.severity, "message": f.message}
                    for f in c.findings
                ],
                "suggestion": c.suggestion,
                "latency_ms": c.latency_ms,
                "error": c.error,
            })

        result = {
            "pr_id": out.pr_id,
            "risk_score": out.risk_score,
            "risk_level": out.risk_level.value,
            "summary": out.summary,
            "critics": critics_data,
            "total_tokens": out.total_tokens,
            "total_latency_ms": out.total_latency_ms,
        }

        with _tasks_lock:
            _tasks[task_id]["status"] = "done"
            _tasks[task_id]["result"] = result
            _tasks[task_id]["critics_done"] = len(critics_data)
            _tasks[task_id]["finished_at"] = datetime.now(timezone.utc).isoformat()

        # Save to history
        try:
            with _db_lock:
                conn = _get_db()
                conn.execute(
                    "INSERT INTO review_history (user_id, pr_id, risk_score, risk_level, summary, critics_json) VALUES (?,?,?,?,?,?)",
                    (user_id, out.pr_id, out.risk_score, out.risk_level.value, out.summary, json.dumps(critics_data, ensure_ascii=False)),
                )
                conn.commit()
                conn.close()
        except Exception as e:
            logger.error("Failed to save review history: %s", e)

    except Exception as e:
        logger.exception("Review task %s failed", task_id)
        with _tasks_lock:
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["error"] = str(e)
            _tasks[task_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JWT Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _create_token(user_id: int, username: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, detail="Token 已过期，请重新登录")
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, detail=f"无效的 Token: {e}")


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    if not credentials:
        raise HTTPException(401, detail="未提供认证信息")
    payload = _decode_token(credentials.credentials)
    return {"id": int(payload["sub"]), "username": payload["username"]}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pydantic Models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class ReviewRequest(BaseModel):
    pr_id: str
    diff: str
    impact_files: list[str] = []
    title: str = ""
    description: str = ""
    language: str = "java"


class FromUrlRequest(BaseModel):
    url: str


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Auth Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.post("/api/v1/auth/register")
def register(req: RegisterRequest):
    username = req.username.strip()
    password = req.password

    if len(username) < 2 or len(username) > 32:
        raise HTTPException(400, detail="用户名长度需要 2-32 个字符")
    if len(password) < 6:
        raise HTTPException(400, detail="密码长度至少 6 个字符")

    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    try:
        with _db_lock:
            conn = _get_db()
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, pw_hash),
            )
            conn.commit()
            user = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            conn.close()
    except sqlite3.IntegrityError:
        raise HTTPException(409, detail="用户名已存在")

    token = _create_token(user["id"], username)
    return {"token": token, "username": username, "message": "注册成功"}


@app.post("/api/v1/auth/login")
def login(req: LoginRequest):
    username = req.username.strip()
    password = req.password

    with _db_lock:
        conn = _get_db()
        user = conn.execute("SELECT id, username, password_hash FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()

    if not user:
        raise HTTPException(401, detail="用户名或密码错误")

    if not bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8")):
        raise HTTPException(401, detail="用户名或密码错误")

    token = _create_token(user["id"], user["username"])
    return {"token": token, "username": user["username"], "message": "登录成功"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Review Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.post("/api/v1/review")
def start_review(req: ReviewRequest, user: dict = Depends(get_current_user)):
    pr_input = PRReviewInput(
        pr_id=req.pr_id, diff=req.diff, impact_files=req.impact_files,
        title=req.title, description=req.description, language=req.language,
    )

    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "queued",
            "critics_done": 0,
            "result": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "pr_id": req.pr_id,
            "user_id": user["id"],
        }

    t = threading.Thread(target=_run_review_task, args=(task_id, pr_input, user["id"]), daemon=True)
    t.start()

    return {"task_id": task_id, "status": "queued"}


# Upload limit: 5 MB
REVIEW_UPLOAD_MAX_BYTES = 5 * 1024 * 1024


@app.post("/api/v1/review/upload")
async def upload_review(
    file: UploadFile = File(...),
    title: str = Form(""),
    language: str = Form("java"),
    user: dict = Depends(get_current_user),
):
    """Upload a .diff/.patch file and start a review with its contents."""
    filename = (file.filename or "").lower()
    if not (filename.endswith(".diff") or filename.endswith(".patch")):
        raise HTTPException(400, detail="仅支持 .diff 或 .patch 文件")

    # Read file with size cap
    data = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > REVIEW_UPLOAD_MAX_BYTES:
            raise HTTPException(413, detail="文件超过 5MB")

    try:
        diff_text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        diff_text = bytes(data).decode("utf-8", errors="replace")

    if not diff_text.strip():
        raise HTTPException(400, detail="文件内容为空")

    pr_id = f"upload-{uuid.uuid4().hex[:8]}"
    pr_input = PRReviewInput(
        pr_id=pr_id,
        diff=diff_text,
        impact_files=[],
        title=title or (file.filename or "uploaded diff"),
        description="",
        language=language or "java",
    )

    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "queued",
            "critics_done": 0,
            "result": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "pr_id": pr_id,
            "user_id": user["id"],
        }

    t = threading.Thread(target=_run_review_task, args=(task_id, pr_input, user["id"]), daemon=True)
    t.start()

    return {"task_id": task_id, "status": "queued"}


@app.get("/api/v1/review/{task_id}/status")
def review_status(task_id: str, user: dict = Depends(get_current_user)):
    with _tasks_lock:
        task = _tasks.get(task_id)

    if not task:
        raise HTTPException(404, detail="任务不存在")

    return {
        "task_id": task_id,
        "status": task["status"],
        "critics_done": task["critics_done"],
        "result": task["result"],
        "error": task["error"],
        "created_at": task["created_at"],
        "finished_at": task["finished_at"],
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# History Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━



@app.get("/api/v1/review/tasks")
def list_review_tasks(user: dict = Depends(get_current_user)):
    """List the current user's review tasks (queued/running/done/error)."""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT task_id, pr_id, status, critics_done, title, language, "
            "submitted_at, finished_at, position, error "
            "FROM review_tasks WHERE user_id=? "
            "ORDER BY submitted_at DESC LIMIT 50",
            (user["id"],),
        ).fetchall()
    except Exception:
        # Fallback: derive task list from review_history if review_tasks doesn't exist
        try:
            rows = conn.execute(
                "SELECT id as task_id, pr_id, 'done' as status, 4 as critics_done, "
                "'' as title, '' as language, created_at as submitted_at, "
                "created_at as finished_at, 0 as position, NULL as error "
                "FROM review_history WHERE user_id=? "
                "ORDER BY created_at DESC LIMIT 50",
                (user["id"],),
            ).fetchall()
        except Exception:
            rows = []
    finally:
        try: conn.close()
        except Exception: pass
    return {"tasks": [dict(r) if hasattr(r, "keys") else dict(zip(["task_id","pr_id","status","critics_done","title","language","submitted_at","finished_at","position","error"], r)) for r in rows]}


@app.get("/api/v1/review/{task_id}/document")
def get_review_document(task_id: str, user: dict = Depends(get_current_user)):
    """Return the cached full result for a completed review task."""
    conn = _get_db()
    try:
        try:
            row = conn.execute(
                "SELECT * FROM review_tasks WHERE task_id=? AND user_id=?",
                (task_id, user["id"]),
            ).fetchone()
        except Exception:
            row = None
        if not row:
            # Fallback to review_history
            try:
                row = conn.execute(
                    "SELECT id, pr_id, risk_score, risk_level, summary, critics_json, created_at "
                    "FROM review_history WHERE pr_id=? AND user_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (task_id, user["id"]),
                ).fetchone()
            except Exception:
                row = None
        if not row:
            raise HTTPException(404, "任务不存在")
        d = dict(row) if hasattr(row, "keys") else row
        # Determine result payload
        result_json = d.get("result_json") if isinstance(d, dict) else None
        critics_json = d.get("critics_json") if isinstance(d, dict) else None
        result = None
        if result_json:
            try:
                result = json.loads(result_json) if isinstance(result_json, str) else result_json
            except Exception:
                result = None
        elif critics_json:
            try:
                critics = json.loads(critics_json) if isinstance(critics_json, str) else critics_json
            except Exception:
                critics = []
            result = {
                "pr_id": d.get("pr_id"),
                "risk_score": d.get("risk_score"),
                "risk_level": d.get("risk_level"),
                "summary": d.get("summary", ""),
                "critics": critics,
            }
    finally:
        try: conn.close()
        except Exception: pass
    return {"status": "done", "result": result}



@app.get("/api/v1/reviews")
def list_reviews(page: int = 1, page_size: int = 20, user: dict = Depends(get_current_user)):
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20

    offset = (page - 1) * page_size

    with _db_lock:
        conn = _get_db()
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM review_history WHERE user_id = ?", (user["id"],)
        ).fetchone()["cnt"]
        rows = conn.execute(
            "SELECT id, pr_id, risk_score, risk_level, summary, critics_json, created_at FROM review_history WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (user["id"], page_size, offset),
        ).fetchall()
        conn.close()

    items = []
    for r in rows:
        try:
            critics = json.loads(r["critics_json"])
        except Exception:
            critics = []
        items.append({
            "id": r["id"],
            "pr_id": r["pr_id"],
            "risk_score": r["risk_score"],
            "risk_level": r["risk_level"],
            "summary": r["summary"],
            "critics": critics,
            "created_at": r["created_at"],
        })

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GitHub Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/v1/github/{owner}/{repo}/pulls/{pr_number}")
def fetch_github_pr(owner: str, repo: str, pr_number: int, user: dict = Depends(get_current_user)):
    """Fetch PR diff and info from GitHub."""
    import httpx

    try:
        cfg = get_settings().github
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "devbot"}
        if cfg.token and cfg.token != "mock":
            headers["Authorization"] = f"Bearer {cfg.token}"

        r_info = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers, timeout=15,
        )
        if r_info.status_code != 200:
            return {"error": f"GitHub API 返回 {r_info.status_code}: {r_info.text[:200]}"}
        info = r_info.json()

        diff_headers = {**headers, "Accept": "application/vnd.github.v3.diff"}
        r_diff = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=diff_headers, timeout=15,
        )
        diff_text = r_diff.text[:80000] if r_diff.status_code == 200 else ""

        return {
            "diff": diff_text,
            "title": info.get("title", ""),
            "body": info.get("body", ""),
            "files": info.get("changed_files", 0),
            "additions": info.get("additions", 0),
            "deletions": info.get("deletions", 0),
            "user": info.get("user", {}).get("login", ""),
            "state": info.get("state", ""),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/v1/review/from-url")
async def review_from_url(req: FromUrlRequest, user: dict = Depends(get_current_user)):
    """Parse a GitHub URL or shortcut and fetch the diff for preview.

    Accepts:
      - https://github.com/owner/repo/pull/N (+/files)
      - https://github.com/owner/repo/compare/base...head
      - https://github.com/owner/repo/commit/sha
      - owner/repo#N  or  owner/repo/N
    Returns the diff plus metadata; does NOT submit a review.
    """
    import re as _re
    import httpx

    url = (req.url or "").strip()
    if not url:
        raise HTTPException(400, detail="请输入 URL 或 owner/repo#N")

    pr_m = _re.match(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    cmp_m = _re.match(r"^https?://github\.com/([^/]+)/([^/]+)/compare/(.+?)\.\.\.([^?#]+?)(?:[?#].*)?$", url)
    com_m = _re.match(r"^https?://github\.com/([^/]+)/([^/]+)/commit/([a-f0-9]+)", url)
    sc_m = _re.match(r"^([\w.-]+)/([\w.-]+)[#/](\d+)$", url)

    # Auth headers if available
    try:
        cfg = get_settings().github
        gh_token = cfg.token if (cfg.token and cfg.token != "mock") else None
    except Exception:
        gh_token = None

    def _headers(accept: str = "application/vnd.github.v3+json") -> dict:
        h = {"Accept": accept, "User-Agent": "devbot"}
        if gh_token:
            h["Authorization"] = f"Bearer {gh_token}"
        return h

    if pr_m or sc_m:
        m = pr_m or sc_m
        owner, repo, pr_num = m.group(1), m.group(2), int(m.group(3))
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                pr_resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}",
                    headers=_headers(),
                )
                if pr_resp.status_code != 200:
                    raise HTTPException(404, detail=f"无法访问 PR ({pr_resp.status_code}): {pr_resp.text[:200]}")
                pr_data = pr_resp.json()

                diff_resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}",
                    headers=_headers("application/vnd.github.v3.diff"),
                )
                diff_text = diff_resp.text if diff_resp.status_code == 200 else ""
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, detail=f"GitHub 请求失败: {e}")

        body = pr_data.get("body") or ""
        return {
            "diff": diff_text,
            "title": pr_data.get("title", ""),
            "body": body[:500],
            "pr_id": f"{owner}/{repo}#{pr_num}",
            "language": ((pr_data.get("base") or {}).get("repo") or {}).get("language", "") or "",
            "url": pr_data.get("html_url"),
            "stats": {
                "files": pr_data.get("changed_files", 0),
                "additions": pr_data.get("additions", 0),
                "deletions": pr_data.get("deletions", 0),
            },
        }

    if com_m:
        owner, repo, sha = com_m.group(1), com_m.group(2), com_m.group(3)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                meta = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                    headers=_headers(),
                )
                if meta.status_code != 200:
                    raise HTTPException(404, detail=f"无法访问 commit ({meta.status_code}): {meta.text[:200]}")
                meta_data = meta.json()
                diff_resp = await client.get(
                    f"https://github.com/{owner}/{repo}/commit/{sha}.diff",
                    headers={"User-Agent": "devbot"},
                    follow_redirects=True,
                )
                diff_text = diff_resp.text if diff_resp.status_code == 200 else ""
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, detail=f"GitHub 请求失败: {e}")

        files = meta_data.get("files", []) or []
        commit_msg = (meta_data.get("commit") or {}).get("message", "") or ""
        return {
            "diff": diff_text,
            "title": commit_msg.split("\n")[0][:200] if commit_msg else f"Commit {sha[:8]}",
            "body": commit_msg[:500],
            "pr_id": f"{owner}/{repo}@{sha[:8]}",
            "language": "",
            "url": meta_data.get("html_url"),
            "stats": {
                "files": len(files),
                "additions": sum(f.get("additions", 0) for f in files),
                "deletions": sum(f.get("deletions", 0) for f in files),
            },
        }

    if cmp_m:
        owner, repo, base, head = cmp_m.group(1), cmp_m.group(2), cmp_m.group(3), cmp_m.group(4)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                cmp_resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/compare/{base}...{head}",
                    headers=_headers(),
                )
                if cmp_resp.status_code != 200:
                    raise HTTPException(404, detail=f"无法访问 compare ({cmp_resp.status_code}): {cmp_resp.text[:200]}")
                data = cmp_resp.json()
                diff_resp = await client.get(
                    f"https://github.com/{owner}/{repo}/compare/{base}...{head}.diff",
                    headers={"User-Agent": "devbot"},
                    follow_redirects=True,
                )
                diff_text = diff_resp.text if diff_resp.status_code == 200 else ""
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, detail=f"GitHub 请求失败: {e}")

        files = data.get("files", []) or []
        return {
            "diff": diff_text,
            "title": f"Compare {base}...{head}",
            "body": "",
            "pr_id": f"{owner}/{repo}@{base}...{head}",
            "language": "",
            "url": data.get("html_url") or f"https://github.com/{owner}/{repo}/compare/{base}...{head}",
            "stats": {
                "files": len(files),
                "additions": sum(f.get("additions", 0) for f in files),
                "deletions": sum(f.get("deletions", 0) for f in files),
            },
        }

    raise HTTPException(400, detail="无法识别的 URL 格式。支持：GitHub PR URL / commit URL / compare URL / owner/repo#PR")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Webhook (no auth required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GitHub repo listing (PRs / Commits / Branches)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import httpx as _httpx_listing

def _gh_headers():
    cfg = get_settings().github
    h = {"Accept": "application/vnd.github.v3+json", "User-Agent": "devbot"}
    if cfg.token and cfg.token != "mock":
        h["Authorization"] = f"Bearer {cfg.token}"
    return h

@app.get("/api/v1/github/{owner}/{repo}/prs")
async def list_github_prs(owner: str, repo: str, state: str = "open", per_page: int = 30,
                          user: dict = Depends(get_current_user)):
    """List PRs of a GitHub repo. state in {open, closed, all}."""
    if state not in ("open", "closed", "all"):
        state = "open"
    try:
        async with _httpx_listing.AsyncClient(timeout=15) as cli:
            r = await cli.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                params={"state": state, "per_page": min(per_page, 100), "sort": "updated", "direction": "desc"},
                headers=_gh_headers(),
            )
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"GitHub: {r.text[:200]}")
        data = r.json()
    except _httpx_listing.HTTPError as e:
        raise HTTPException(502, f"GitHub 请求失败: {e}")
    return {"prs": [
        {
            "number": p["number"],
            "title": p["title"],
            "state": p["state"],
            "author": (p.get("user") or {}).get("login"),
            "branch_head": (p.get("head") or {}).get("ref"),
            "branch_base": (p.get("base") or {}).get("ref"),
            "draft": p.get("draft", False),
            "created_at": p.get("created_at"),
            "updated_at": p.get("updated_at"),
            "url": p.get("html_url"),
            "comments": p.get("comments", 0),
        } for p in data
    ]}

@app.get("/api/v1/github/{owner}/{repo}/commits")
async def list_github_commits(owner: str, repo: str, branch: str = None, per_page: int = 30,
                              user: dict = Depends(get_current_user)):
    """List recent commits, optionally on a specific branch."""
    params = {"per_page": min(per_page, 100)}
    if branch:
        params["sha"] = branch
    try:
        async with _httpx_listing.AsyncClient(timeout=15) as cli:
            r = await cli.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits",
                params=params, headers=_gh_headers(),
            )
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"GitHub: {r.text[:200]}")
        data = r.json()
    except _httpx_listing.HTTPError as e:
        raise HTTPException(502, f"GitHub 请求失败: {e}")
    return {"commits": [
        {
            "sha": c["sha"],
            "short_sha": c["sha"][:8],
            "message": (c.get("commit") or {}).get("message", "").split("\n")[0][:120],
            "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
            "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
            "url": c.get("html_url"),
        } for c in data
    ]}

@app.get("/api/v1/github/{owner}/{repo}/branches")
async def list_github_branches(owner: str, repo: str, per_page: int = 50,
                               user: dict = Depends(get_current_user)):
    """List branches of a repo."""
    try:
        async with _httpx_listing.AsyncClient(timeout=15) as cli:
            r = await cli.get(
                f"https://api.github.com/repos/{owner}/{repo}/branches",
                params={"per_page": min(per_page, 100)}, headers=_gh_headers(),
            )
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"GitHub: {r.text[:200]}")
        data = r.json()
    except _httpx_listing.HTTPError as e:
        raise HTTPException(502, f"GitHub 请求失败: {e}")
    return {"branches": [
        {
            "name": b["name"],
            "sha": (b.get("commit") or {}).get("sha", "")[:8],
            "protected": b.get("protected", False),
        } for b in data
    ]}

@app.get("/api/v1/github/{owner}/{repo}/info")
async def github_repo_info(owner: str, repo: str, user: dict = Depends(get_current_user)):
    """Repo metadata: stars, language, description, default_branch."""
    try:
        async with _httpx_listing.AsyncClient(timeout=15) as cli:
            r = await cli.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                headers=_gh_headers(),
            )
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"GitHub: {r.text[:200]}")
        d = r.json()
    except _httpx_listing.HTTPError as e:
        raise HTTPException(502, f"GitHub 请求失败: {e}")
    return {
        "name": d.get("full_name"),
        "description": d.get("description"),
        "default_branch": d.get("default_branch"),
        "language": d.get("language"),
        "stars": d.get("stargazers_count"),
        "forks": d.get("forks_count"),
        "open_issues": d.get("open_issues_count"),
        "url": d.get("html_url"),
    }


class SkillRequest(BaseModel):
    payload: dict = {}


@app.get("/api/v1/skills")
def get_skills(user: dict = Depends(get_current_user)):
    """列出 devbot 所有可调用 Agent 技能(统一注册表)。"""
    return {"skills": list_skills()}


@app.post("/api/v1/skill/{name}")
def invoke_skill(name: str, req: SkillRequest, user: dict = Depends(get_current_user)):
    """统一技能调用入口:review / codegen / testgen / requirement 经同一注册表分发。"""
    if name not in SKILLS:
        raise HTTPException(404, detail="unknown skill: %s" % name)
    try:
        result = run_skill(name, req.payload or {})
    except Exception as e:
        logger.exception("skill %s failed", name)
        raise HTTPException(500, detail=str(e))
    return {"skill": name, "status": "ok", "result": result}


@app.post("/webhook/github")
async def github_webhook(request: Request):
    cfg = get_settings().github
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if cfg.webhook_secret and cfg.webhook_secret != "dev-secret":
        if not verify_signature(body, sig, cfg.webhook_secret):
            raise HTTPException(403, detail="Invalid webhook signature")
    event = request.headers.get("X-GitHub-Event")
    payload = await request.json()
    if event == "pull_request":
        return await handle_pr_event(payload, cfg)
    if event == "issue_comment":
        return await handle_comment_event(payload, cfg)
    return {"status": "ignored"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Health
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/health")
def health():
    return {"status": "ok", "version": "0.2.0"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Frontend SPA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SPA_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DevBot - 智能评审平台</title>
<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
:root {
  --bg: #ffffff;
  --bg-soft: #fbfbfd;
  --panel: #ffffff;
  --border: #d2d2d7;
  --border-strong: #d2d2d7;
  --text: #1d1d1f;
  --text-2: #1d1d1f;
  --text-3: #86868b;
  --accent: #0071e3;
  --accent-hover: #0077ed;
  --hover-bg: #f5f5f7;
  --input-bg: #f5f5f7;
  --success: #34c759;
  --warn: #ff9f0a;
  --danger: #ff3b30;
  --diff-add: #e8f6ec;
  --diff-add-text: #248a3d;
  --diff-add-bg: #d4eedb;
  --diff-del: #fde9e8;
  --diff-del-text: #d70015;
  --diff-del-bg: #fbd9d6;
  --shadow-card: 0 4px 14px rgba(0,0,0,0.05);
  --shadow-card-hover: 0 8px 24px rgba(0,0,0,0.08);
  --sidebar: 240px;
}
html, body {
  font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'SF Pro Text', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
  color: var(--text);
  background: #ffffff;
  -webkit-font-smoothing: antialiased;
  min-height: 100vh;
}
body { background: #ffffff; }
[v-cloak] { display: none !important; }
a { color: inherit; text-decoration: none; }
button { font-family: inherit; cursor: pointer; border: none; background: none; color: inherit; }
input, textarea, select { font-family: inherit; outline: none; }

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: #d2d2d7; border-radius: 6px; }
::-webkit-scrollbar-thumb:hover { background: #b0b0b5; }

/* Toast */
.toast-wrap { position: fixed; top: 18px; right: 18px; z-index: 9999; display: flex; flex-direction: column; gap: 10px; pointer-events: none; }
.toast {
  padding: 12px 18px;
  border-radius: 12px;
  font-size: 14px;
  background: #1d1d1f;
  color: #fff;
  box-shadow: 0 4px 14px rgba(0,0,0,0.15);
  animation: slideIn .25s ease;
  pointer-events: auto;
  max-width: 360px;
  font-weight: 400;
}
.toast.success { background: #34c759; }
.toast.error   { background: #ff3b30; }
@keyframes slideIn { from { transform: translateX(20px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

/* Auth */
@keyframes appleAuthFadeIn { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: translateY(0); } }
@keyframes appleSpin { to { transform: rotate(360deg); } }
.apple-auth-page {
  min-height: 100vh;
  display: flex; align-items: center; justify-content: center;
  background: #fbfbfd;
  position: relative;
  overflow: hidden;
}
.apple-auth-card {
  width: 100%; max-width: 420px;
  background: #ffffff;
  border: 1px solid #d2d2d7;
  border-radius: 18px;
  padding: 44px 40px;
  box-shadow: 0 4px 14px rgba(0,0,0,0.05);
  animation: appleAuthFadeIn 0.35s ease;
  position: relative; z-index: 1;
}
.apple-auth-logo {
  text-align: center;
  font-size: 32px; font-weight: 600; letter-spacing: -0.015em; margin-bottom: 8px;
  color: #1d1d1f;
}
.apple-auth-subtitle { text-align: center; font-size: 15px; color: #86868b; margin-bottom: 34px; font-weight: 400; }
.apple-auth-form { display: flex; flex-direction: column; gap: 12px; }
.apple-input {
  width: 100%; height: 48px;
  background: #f5f5f7;
  border: none;
  border-radius: 12px;
  padding: 0 16px;
  font-size: 15px;
  color: #1d1d1f;
  outline: none;
  transition: box-shadow 0.15s;
}
.apple-input::placeholder { color: #86868b; }
.apple-input:focus {
  box-shadow: 0 0 0 4px rgba(0,113,227,0.2);
}
.apple-btn {
  width: 100%; height: 48px;
  background: #0071e3;
  color: #fff;
  border: none;
  border-radius: 980px;
  font-size: 17px; font-weight: 400;
  cursor: pointer;
  transition: background 0.15s;
  display: inline-flex; align-items: center; justify-content: center; text-align: center; gap: 8px;
  margin-top: 8px;
}
.apple-btn:hover { background: #0077ed; }
.apple-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.apple-spinner {
  width: 18px; height: 18px;
  border: 2px solid rgba(255,255,255,0.35);
  border-top-color: #fff;
  border-radius: 50%;
  animation: appleSpin 0.7s linear infinite;
  display: inline-block;
}
.apple-auth-toggle { text-align: center; margin-top: 8px; }
.apple-auth-toggle span {
  color: #0071e3;
  font-size: 14px; font-weight: 400; cursor: pointer;
}
.apple-auth-toggle span:hover { text-decoration: underline; }
.apple-auth-error { color: #ff3b30; font-size: 13px; margin-top: -4px; padding: 0 4px; font-weight: 400; }

/* Layout */
.shell { display: grid; grid-template-columns: var(--sidebar) 1fr; min-height: 100vh; }
.sidebar {
  background: #ffffff;
  border-right: 1px solid #d2d2d7;
  display: flex; flex-direction: column;
  height: 100vh; position: sticky; top: 0;
  overflow-y: auto;
}
.sidebar-head { padding: 20px 18px 16px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid #d2d2d7; }
.brand-mark {
  width: 32px; height: 32px;
  border-radius: 10px;
  background: #1d1d1f;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 600; font-size: 15px;
  flex-shrink: 0;
}
.brand-title {
  font-size: 16px; font-weight: 600; letter-spacing: -0.2px;
  color: #1d1d1f;
}
.brand-sub { font-size: 11px; color: #86868b; margin-top: 1px; letter-spacing: 0.2px; font-weight: 400; }
.sidebar-section-title { padding: 20px 18px 8px; font-size: 11px; font-weight: 600; color: #86868b; letter-spacing: 1px; text-transform: uppercase; }
.nav-items { padding: 0 10px; flex: 1; }
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px;
  border-radius: 10px;
  font-size: 13.5px;
  color: #1d1d1f;
  cursor: pointer;
  transition: background .15s;
  margin: 2px 0;
  font-weight: 500;
}
.nav-item:hover { background: #f5f5f7; }
.nav-item.active {
  background: #f5f5f7;
  color: #1d1d1f;
}
.nav-item.active .ic { color: #1d1d1f; }
.nav-item .ic { width: 16px; height: 16px; flex-shrink: 0; }
.sidebar-foot { padding: 14px 18px; border-top: 1px solid #d2d2d7; display: flex; align-items: center; gap: 10px; }
.avatar {
  width: 34px; height: 34px;
  border-radius: 50%;
  background: #f5f5f7;
  color: #1d1d1f;
  border: 1px solid #d2d2d7;
  display: flex; align-items: center; justify-content: center;
  font-weight: 600; font-size: 13px;
  flex-shrink: 0;
}
.user-info { flex: 1; min-width: 0; }
.user-name { font-size: 13px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #1d1d1f; }
.user-sub { font-size: 11px; color: #86868b; font-weight: 400; }
.btn-icon {
  padding: 6px;
  border-radius: 8px;
  color: #86868b;
  transition: background .15s;
  display: inline-flex; align-items: center; justify-content: center; text-align: center;
}
.btn-icon:hover { background: #f5f5f7; color: #1d1d1f; }

/* Main */
.main { display: flex; flex-direction: column; min-height: 100vh; }
.topbar {
  height: 52px;
  border-bottom: 1px solid #d2d2d7;
  background: #ffffff;
  display: flex; align-items: center; padding: 0 24px; gap: 14px;
  position: sticky; top: 0; z-index: 10;
}
.topbar .crumb { font-size: 13px; color: #86868b; display: flex; align-items: center; gap: 6px; font-weight: 400; }
.topbar .crumb b {
  font-weight: 600;
  color: #1d1d1f;
}
.topbar .topbar-actions { margin-left: auto; display: flex; align-items: center; gap: 10px; }
.topbar a.platform-link { font-size: 13px; color: #1d1d1f; padding: 6px 12px; border-radius: 8px; transition: background .15s; font-weight: 400; }
.topbar a.platform-link:hover { background: #f5f5f7; color: #0071e3; }
.content { padding: 30px 32px; background: #ffffff; }

/* Detail head */
.detail-head { padding-bottom: 22px; border-bottom: 1px solid #d2d2d7; margin-bottom: 24px; }
.detail-title-row { display: flex; align-items: center; gap: 14px; margin-bottom: 6px; }
.detail-title {
  font-size: 28px; font-weight: 600; letter-spacing: -0.5px;
  color: #1d1d1f;
}
.detail-sub { font-size: 14px; color: #86868b; font-weight: 400; }

/* Card */
.card {
  background: #ffffff;
  border: 1px solid #d2d2d7;
  border-radius: 18px;
  transition: box-shadow .2s;
}
.card:hover { box-shadow: 0 4px 14px rgba(0,0,0,0.05); }
.card .card-head { padding: 16px 22px; border-bottom: 1px solid #d2d2d7; display: flex; align-items: center; justify-content: space-between; }
.card .card-head-title { font-size: 14px; font-weight: 600; color: #1d1d1f; }
.card .card-body { padding: 22px; }
.field { margin-bottom: 16px; }
.field-label { display: block; font-size: 11px; font-weight: 600; color: #86868b; margin-bottom: 7px; letter-spacing: 0.5px; text-transform: uppercase; }
.text-input {
  width: 100%; height: 42px;
  border: none;
  border-radius: 12px;
  padding: 0 14px;
  font-size: 14px;
  background: #f5f5f7;
  color: #1d1d1f;
  transition: box-shadow .15s;
}
.text-input:focus { box-shadow: 0 0 0 4px rgba(0,113,227,0.2); }
.diff-input {
  width: 100%; min-height: 200px;
  border: none;
  border-radius: 12px;
  padding: 12px 14px;
  font-family: 'SF Mono', 'Monaco', monospace;
  font-size: 13px;
  background: #f5f5f7;
  color: #1d1d1f;
  line-height: 1.6;
  resize: vertical;
  transition: box-shadow .15s;
}
.diff-input:focus { box-shadow: 0 0 0 4px rgba(0,113,227,0.2); }
.field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }

/* Smart URL input card */
.url-input-card {
  background: #ffffff;
  border: 1px solid #d2d2d7;
  border-radius: 14px;
  padding: 22px 24px;
  margin-bottom: 16px;
}
.url-input-card label {
  display: block;
  font-size: 14px;
  color: #1d1d1f;
  font-weight: 600;
  margin-bottom: 10px;
}
.url-input-row { display: flex; gap: 10px; align-items: center; }
.url-input {
  flex: 1;
  background: #f5f5f7;
  border: none;
  border-radius: 10px;
  padding: 12px 16px;
  font-size: 14px;
  color: #1d1d1f;
  outline: none;
  transition: box-shadow .15s;
}
.url-input:focus { box-shadow: 0 0 0 4px rgba(0,113,227,0.18); }
.url-hint { font-size: 12px; color: #86868b; margin-top: 8px; }

.diff-preview-card {
  background: #ffffff;
  border: 1px solid #d2d2d7;
  border-radius: 14px;
  padding: 20px 22px;
  margin-bottom: 16px;
}
.diff-preview-card .diff-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 14px;
  gap: 12px;
}
.diff-preview-card .diff-title {
  font-weight: 600;
  font-size: 15px;
  color: #1d1d1f;
  line-height: 1.4;
}
.diff-preview-card .diff-meta {
  display: flex;
  gap: 10px;
  font-size: 12px;
  color: #86868b;
  margin-top: 6px;
  flex-wrap: wrap;
  align-items: center;
}
.diff-preview-card .diff-meta .badge {
  background: #f5f5f7;
  padding: 2px 8px;
  border-radius: 6px;
  font-family: 'SF Mono', 'Monaco', monospace;
  color: #1d1d1f;
}
.diff-preview-card .diff-meta .added   { color: #34c759; font-weight: 600; }
.diff-preview-card .diff-meta .removed { color: #ff3b30; font-weight: 600; }
.diff-preview-card .link-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 32px; height: 32px;
  border-radius: 8px;
  color: #86868b;
  background: #f5f5f7;
  transition: background .15s, color .15s;
  flex-shrink: 0;
}
.diff-preview-card .link-icon:hover { background: #ececef; color: #0071e3; }
.diff-preview-card pre.diff-preview {
  background: #fafafa;
  border: 1px solid #e8e8ec;
  border-radius: 8px;
  padding: 14px;
  font-family: 'SF Mono', 'Monaco', monospace;
  font-size: 12px;
  max-height: 360px;
  overflow: auto;
  white-space: pre;
  color: #424245;
  margin-bottom: 14px;
}
.diff-preview-card .review-actions {
  display: flex; gap: 10px; align-items: center;
}
.diff-preview-card .review-actions select {
  background: #f5f5f7;
  border: none;
  border-radius: 980px;
  padding: 10px 16px;
  font-size: 14px;
  color: #1d1d1f;
  outline: none;
}
.btn-primary-large {
  background: #0071e3;
  color: #fff;
  border: none;
  border-radius: 980px;
  padding: 12px 28px;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  flex: 1;
  transition: background .15s;
  display: inline-flex; align-items: center; justify-content: center; gap: 7px;
}
.btn-primary-large:hover { background: #0077ed; }
.btn-primary-large:disabled { opacity: .4; cursor: not-allowed; }

.advanced-options {
  background: #ffffff;
  border: 1px solid #d2d2d7;
  border-radius: 14px;
  padding: 14px 20px;
  margin-bottom: 16px;
}
.advanced-options[open] { padding-bottom: 18px; }
.advanced-options summary {
  cursor: pointer;
  color: #1d1d1f;
  font-size: 13px;
  font-weight: 500;
  list-style: none;
  display: flex; align-items: center; gap: 6px;
  padding: 4px 0;
}
.advanced-options summary::-webkit-details-marker { display: none; }
.advanced-options summary::before {
  content: '▸';
  color: #86868b;
  font-size: 11px;
  transition: transform .15s;
  display: inline-block;
}
.advanced-options[open] summary::before { transform: rotate(90deg); }
.advanced-options .adv-body { margin-top: 12px; }

.mode-tabs {
  display: inline-flex;
  background: #f5f5f7;
  border-radius: 12px;
  padding: 4px;
  margin-bottom: 18px;
}
.mode-tab {
  padding: 8px 18px;
  border-radius: 9px;
  font-size: 13px;
  font-weight: 500;
  color: #1d1d1f;
  transition: all .15s;
  display: inline-flex; align-items: center; justify-content: center; text-align: center;
}
.mode-tab.active {
  background: #ffffff;
  color: #1d1d1f;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}
.btn-primary {
  padding: 12px 22px;
  border-radius: 980px;
  background: #0071e3;
  color: #fff;
  border: none;
  font-size: 17px; font-weight: 400;
  display: inline-flex; align-items: center; justify-content: center; text-align: center; gap: 7px;
  transition: background .15s;
}
.btn-primary:hover { background: #0077ed; }
.btn-primary:disabled { opacity: .4; cursor: not-allowed; }
.btn-block { width: 100%; }
.btn-secondary {
  padding: 12px 22px;
  border-radius: 980px;
  border: 1px solid #0071e3;
  background: transparent;
  font-size: 17px; font-weight: 400;
  color: #0071e3;
  transition: background .15s;
  display: inline-flex; align-items: center; justify-content: center; text-align: center; gap: 6px;
}
.btn-secondary:hover { background: rgba(0,113,227,0.05); }
.btn-dark {
  padding: 10px 22px;
  border-radius: 980px;
  background: #1d1d1f;
  color: #fff;
  border: none;
  font-size: 14px; font-weight: 400;
  transition: background .15s;
  display: inline-flex; align-items: center; justify-content: center; text-align: center; gap: 6px;
}
.btn-dark:hover { background: #2d2d2f; }

/* Drop zone */
.drop-zone {
  border: 2px dashed #d2d2d7;
  border-radius: 14px;
  background: #fbfbfd;
  padding: 34px 20px;
  text-align: center;
  cursor: pointer;
  transition: all .2s;
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  min-height: 170px; justify-content: center;
}
.drop-zone:hover { background: #f5f5f7; border-color: #0071e3; }
.drop-zone.dragging { background: #f5f5f7; border-color: #0071e3; }
.drop-zone.has-file {
  border-color: #0071e3;
  background: #ffffff;
  border-style: solid;
  cursor: default;
}
.drop-zone .drop-icon { color: #0071e3; }
.drop-zone .drop-text { font-size: 14px; color: #1d1d1f; font-weight: 500; }
.drop-zone .drop-hint { font-size: 12px; color: #86868b; }
.picked-file { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.picked-name { font-size: 14px; font-weight: 600; color: #1d1d1f; word-break: break-all; max-width: 100%; }
.picked-size { font-size: 12px; color: #86868b; }
.picked-clear {
  margin-top: 6px;
  font-size: 12px;
  color: #ff3b30;
  background: transparent;
  padding: 5px 12px;
  border: 1px solid #ff3b30;
  border-radius: 980px;
  transition: background .15s;
}
.picked-clear:hover { background: rgba(255,59,48,0.06); }

/* Diff viewer */
.diff-viewer {
  border: 1px solid #d2d2d7;
  border-radius: 12px;
  overflow: hidden;
  background: #fbfbfd;
  font-family: 'SF Mono', 'Monaco', monospace;
  font-size: 12.5px;
  line-height: 1.65;
}
.diff-line { display: flex; padding: 0; align-items: stretch; }
.diff-line .ln { width: 44px; color: #86868b; text-align: right; padding: 0 8px 0 12px; flex-shrink: 0; user-select: none; font-size: 11.5px; line-height: 1.65; }
.diff-line .code { flex: 1; padding: 0 12px; white-space: pre-wrap; word-break: break-all; color: #1d1d1f; }
.diff-line.add { background: var(--diff-add); }
.diff-line.add .ln { background: var(--diff-add-bg); color: var(--diff-add-text); }
.diff-line.add .code { color: var(--diff-add-text); }
.diff-line.del { background: var(--diff-del); }
.diff-line.del .ln { background: var(--diff-del-bg); color: var(--diff-del-text); }
.diff-line.del .code { color: var(--diff-del-text); }
.diff-line.hunk { background: #f5f5f7; }
.diff-line.hunk .ln { color: #0071e3; background: #eef5fd; }
.diff-line.hunk .code { color: #0071e3; font-weight: 500; }
.diff-line.meta { background: #f5f5f7; color: #86868b; }
.diff-line.meta .ln { background: #eaeaea; }

/* Progress critics */
.progress-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; }
.progress-tile {
  display: flex; align-items: center; gap: 12px;
  padding: 14px 16px;
  border-radius: 12px;
  background: #ffffff;
  border: 1px solid #d2d2d7;
  transition: background .15s, border-color .15s;
}
.progress-tile.running { background: #eef5fd; border-color: #b9d8f6; }
.progress-tile.done    { background: #e8f6ec; border-color: #b5e3c2; }
.progress-tile.error   { background: #fde9e8; border-color: #f4b5b0; }
.progress-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.progress-dot.waiting { background: #d2d2d7; }
.progress-dot.running { background: #0071e3; animation: dotPulse 1.2s infinite; }
.progress-dot.done    { background: #34c759; }
@keyframes dotPulse { 0%, 100% { opacity: 1; } 50% { opacity: .4; } }
.progress-tile .name { font-size: 13.5px; font-weight: 500; color: #1d1d1f; }
.progress-tile.done .name    { color: #248a3d; }
.progress-tile.running .name { color: #0071e3; }

/* Results */
.dashboard { display: grid; grid-template-columns: 1fr 2fr; gap: 20px; margin-bottom: 22px; }
@media (max-width: 980px) { .dashboard { grid-template-columns: 1fr; } }

.gauge-card {
  background: #ffffff;
  border: 1px solid #d2d2d7;
  border-radius: 18px;
  padding: 30px;
  display: flex; flex-direction: column; align-items: center;
}
.gauge { width: 180px; height: 180px; position: relative; }
.gauge svg { transform: rotate(-90deg); }
.gauge-circle-bg { fill: none; stroke: #f5f5f7; stroke-width: 14; }
.gauge-circle { fill: none; stroke-width: 14; stroke-linecap: round; transition: stroke-dashoffset 1.2s cubic-bezier(.2, .8, .2, 1); }
.gauge-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
.gauge-num { font-size: 48px; font-weight: 600; letter-spacing: -1.5px; color: #1d1d1f; }
.gauge-divisor { font-size: 12px; color: #86868b; margin-top: -2px; font-weight: 400; }
.risk-pill {
  margin-top: 16px;
  padding: 6px 16px;
  border-radius: 980px;
  font-size: 13px; font-weight: 500;
  letter-spacing: 0.2px;
  color: #fff;
}
.risk-pill.LOW    { background: #34c759; }
.risk-pill.MEDIUM { background: #ff9f0a; }
.risk-pill.HIGH   { background: #ff3b30; }
.gauge-summary { font-size: 14px; color: #1d1d1f; margin-top: 14px; text-align: center; line-height: 1.55; max-width: 280px; }

.critics-card {
  background: #ffffff;
  border: 1px solid #d2d2d7;
  border-radius: 18px;
  padding: 26px;
}
.critics-card-title { font-size: 14px; font-weight: 600; margin-bottom: 20px; color: #1d1d1f; }
.critic-bar-row { margin-bottom: 16px; }
.critic-bar-head { display: flex; align-items: center; justify-content: space-between; font-size: 13px; margin-bottom: 7px; }
.critic-bar-name { font-weight: 500; color: #1d1d1f; display: flex; align-items: center; gap: 8px; }
.critic-tag {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px;
  border-radius: 7px;
  font-size: 11px; font-weight: 600;
  color: #fff;
}
.critic-tag.correctness { background: #0071e3; }
.critic-tag.design      { background: #5e5ce6; }
.critic-tag.security    { background: #ff3b30; }
.critic-tag.readability { background: #34c759; }
.critic-bar-score { font-weight: 600; font-size: 15px; color: #1d1d1f; }
.critic-bar-track { height: 8px; background: #f5f5f7; border-radius: 4px; overflow: hidden; }
.critic-bar-fill  { height: 100%; border-radius: 4px; transition: width 1s cubic-bezier(.2, .8, .2, 1); }
.critic-bar-foot  { font-size: 11.5px; color: #86868b; margin-top: 6px; font-weight: 400; }

/* Findings */
.findings-list {
  background: #ffffff;
  border: 1px solid #d2d2d7;
  border-radius: 18px;
  overflow: hidden;
}
.findings-head { padding: 18px 24px; border-bottom: 1px solid #d2d2d7; display: flex; align-items: center; gap: 10px; }
.findings-head-title { font-size: 14px; font-weight: 600; color: #1d1d1f; }
.findings-head-count {
  font-size: 12px;
  color: #fff;
  background: #1d1d1f;
  padding: 3px 11px;
  border-radius: 980px;
  font-weight: 500;
}
.finding { padding: 16px 24px; border-bottom: 1px solid #d2d2d7; display: flex; gap: 14px; align-items: flex-start; transition: background .15s; }
.finding:last-child { border-bottom: none; }
.finding:hover { background: #fbfbfd; }
.sev {
  flex-shrink: 0;
  padding: 3px 10px;
  border-radius: 8px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: #fff;
}
.sev.error              { background: #ff3b30; }
.sev.warn, .sev.warning { background: #ff9f0a; }
.sev.info               { background: #0071e3; }
.finding-body { flex: 1; min-width: 0; }
.finding-file {
  font-family: 'SF Mono', 'Monaco', monospace;
  font-size: 12px;
  color: #0071e3;
  font-weight: 500;
  margin-bottom: 4px;
}
.finding-msg { font-size: 14px; line-height: 1.55; color: #1d1d1f; }
.finding-by  { font-size: 11px; color: #86868b; margin-top: 6px; font-weight: 400; }

/* History table */
.table { width: 100%; border-collapse: separate; border-spacing: 0; }
.table thead th {
  text-align: left;
  padding: 14px 20px;
  font-size: 11.5px;
  font-weight: 600;
  color: #86868b;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  border-bottom: 1px solid #d2d2d7;
  background: #fbfbfd;
}
.table thead th.sortable { cursor: pointer; user-select: none; }
.table thead th.sortable:hover { color: #0071e3; }
.table thead th .sort-ic { display: inline-block; width: 10px; font-size: 10px; color: #86868b; margin-left: 3px; }
.table tbody td { padding: 14px 20px; font-size: 14px; border-bottom: 1px solid #d2d2d7; color: #1d1d1f; }
.table tbody tr { cursor: pointer; transition: background .15s; }
.table tbody tr:hover    { background: #fbfbfd; }
.table tbody tr.expanded { background: #f5f5f7; }
.expand-row td {
  background: #fbfbfd;
  padding: 20px 24px;
  border-bottom: 1px solid #d2d2d7;
}
.empty { padding: 60px 20px; text-align: center; color: #86868b; }
.empty-icon { font-size: 48px; margin-bottom: 10px; opacity: .7; }
.empty-title { font-size: 16px; font-weight: 600; color: #1d1d1f; margin-bottom: 4px; }
.empty-sub { font-size: 13px; color: #86868b; }
.loading-block { padding: 60px; text-align: center; }
.spinner-lg {
  display: inline-block;
  width: 36px; height: 36px;
  border: 3px solid #f5f5f7;
  border-top-color: #0071e3;
  border-radius: 50%;
  animation: appleSpin .8s linear infinite;
}

/* Webhook */
.webhook-url-bar {
  display: flex; align-items: center; gap: 10px;
  background: #f5f5f7;
  border: 1px solid #d2d2d7;
  border-radius: 12px;
  padding: 14px 16px;
  margin-bottom: 20px;
}
.webhook-url-text {
  flex: 1;
  font-family: 'SF Mono', 'Monaco', monospace;
  font-size: 13px;
  color: #1d1d1f;
  word-break: break-all;
  font-weight: 400;
}

.steps-list { counter-reset: step; list-style: none; }
.steps-list li { position: relative; padding: 10px 0 10px 40px; font-size: 14px; color: #1d1d1f; line-height: 1.6; counter-increment: step; }
.steps-list li::before {
  content: counter(step);
  position: absolute;
  left: 0; top: 8px;
  width: 26px; height: 26px;
  border-radius: 50%;
  background: #0071e3;
  color: #fff;
  font-size: 12px; font-weight: 600;
  display: flex; align-items: center; justify-content: center;
}
.steps-list li code { background: #f5f5f7; padding: 2px 8px; border-radius: 6px; font-size: 12px; font-family: 'SF Mono', 'Monaco', monospace; color: #1d1d1f; font-weight: 400; }
.steps-list li b { font-weight: 600; color: #1d1d1f; }
.test-result {
  margin-top: 14px;
  padding: 14px 18px;
  border-radius: 12px;
  font-size: 14px;
  display: flex; align-items: center; gap: 10px;
  font-weight: 500;
}
.test-result.ok {
  background: #e8f6ec;
  border: 1px solid #b5e3c2;
  color: #248a3d;
}
.test-result.fail {
  background: #fde9e8;
  border: 1px solid #f4b5b0;
  color: #d70015;
}

.risk-color-low    { color: #248a3d; }
.risk-color-medium { color: #b25b00; }
.risk-color-high   { color: #d70015; }
.tag {
  display: inline-block;
  padding: 3px 11px;
  border-radius: 980px;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.2px;
  color: #fff;
}
.tag.LOW    { background: #34c759; }
.tag.MEDIUM { background: #ff9f0a; }
.tag.HIGH   { background: #ff3b30; }

/* Suggestion */
.suggestion {
  background: #fbfbfd;
  border-left: 3px solid #0071e3;
  padding: 12px 16px;
  border-radius: 0 10px 10px 0;
  font-size: 14px;
  line-height: 1.55;
  color: #1d1d1f;
  margin-top: 10px;
}

/* Pagination */
.pagination { padding: 16px 20px; display: flex; align-items: center; justify-content: space-between; border-top: 1px solid #d2d2d7; }
.pagination .pages { display: flex; gap: 6px; }
.pg-btn {
  padding: 7px 13px;
  border: 1px solid #d2d2d7;
  border-radius: 980px;
  font-size: 13px;
  color: #1d1d1f;
  background: #ffffff;
  font-weight: 400;
  transition: background .15s;
  display: inline-flex; align-items: center; justify-content: center; text-align: center;
}
.pg-btn:hover:not(:disabled) { background: #f5f5f7; color: #0071e3; }
.pg-btn:disabled { opacity: .4; cursor: not-allowed; }
.pg-btn.active {
  background: #0071e3;
  color: #fff;
  border-color: #0071e3;
}

/* Hamburger */
.hamburger {
  display: none; width: 36px; height: 36px; border-radius: 10px;
  align-items: center; justify-content: center; text-align: center;
  color: #1d1d1f; background: transparent; transition: background .15s; flex-shrink: 0;
}
.hamburger:hover { background: #f5f5f7; }
.hamburger svg { width: 20px; height: 20px; }
.sidebar-backdrop { display: none; }

/* Generic review button - covered by btn-primary too */
.review-btn {
  padding: 12px 22px;
  border-radius: 980px;
  background: #0071e3;
  color: #fff;
  border: none;
  font-size: 17px; font-weight: 400;
  display: inline-flex; align-items: center; justify-content: center; text-align: center; gap: 7px;
  transition: background .15s;
}
.review-btn:hover { background: #0077ed; }

/* ============================================================
   RESPONSIVE DESIGN
   ============================================================ */

/* Tablet : 768px - 1024px */
@media (max-width: 1024px) {
  :root { --sidebar: 200px; }
  .content { padding: 24px 22px; }
  .topbar { padding: 0 18px; }
  .card .card-body { padding: 18px; }
  .detail-title { font-size: 24px; }
  .sidebar-head { padding: 16px 14px 12px; }
  .sidebar-section-title { padding: 14px 14px 6px; }
  .dashboard { grid-template-columns: 1fr; }
  .progress-grid { grid-template-columns: repeat(2, 1fr); }
}

/* Mobile : < 768px */
@media (max-width: 768px) {
  html, body { font-size: 14px; }
  .shell { grid-template-columns: 1fr; }
  .sidebar {
    position: fixed; top: 0; left: 0;
    width: 280px; height: 100vh;
    z-index: 200;
    transform: translateX(-100%);
    transition: transform .28s cubic-bezier(.2, .8, .2, 1);
    box-shadow: 0 10px 30px rgba(0,0,0,0.1);
    background: #ffffff;
  }
  .shell.sidebar-open .sidebar { transform: translateX(0); }
  .sidebar-backdrop {
    display: block;
    position: fixed; inset: 0;
    background: rgba(0, 0, 0, 0.4);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    z-index: 150;
    opacity: 0; pointer-events: none;
    transition: opacity .25s ease;
  }
  .shell.sidebar-open .sidebar-backdrop { opacity: 1; pointer-events: auto; }
  .hamburger { display: inline-flex; }
  .topbar { padding: 0 14px; height: 50px; gap: 10px; }
  .topbar .crumb { font-size: 12.5px; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .topbar .topbar-actions { gap: 4px; }
  .topbar a.platform-link { padding: 5px 8px; font-size: 11.5px; }
  .content { padding: 20px 16px; }
  .detail-title { font-size: 22px; }
  .detail-title-row { flex-wrap: wrap; gap: 10px; }
  .detail-head { padding-bottom: 16px; margin-bottom: 18px; }
  .card { border-radius: 14px; }
  .card .card-head { padding: 12px 16px; }
  .card .card-body { padding: 16px; }
  .field-row { grid-template-columns: 1fr; gap: 12px; }
  .diff-input { min-height: 200px; font-size: 12px; padding: 10px 12px; }
  .diff-viewer { font-size: 11.5px; }
  .diff-line .ln { width: 36px; font-size: 10.5px; padding: 0 6px 0 8px; }
  .diff-line .code { padding: 0 8px; }
  .text-input { height: 44px; font-size: 14px; }
  .btn-primary, .btn-secondary, .btn-dark { width: 100%; }
  .btn-block { width: 100%; }
  .field-row .btn-dark { width: auto; flex-shrink: 0; }
  .mode-tabs { display: flex; width: 100%; }
  .mode-tab { flex: 1; }
  .dashboard { grid-template-columns: 1fr; gap: 14px; margin-bottom: 16px; }
  .gauge-card { padding: 22px; }
  .gauge { width: 150px; height: 150px; }
  .gauge svg { width: 150px; height: 150px; }
  .gauge-num { font-size: 40px; }
  .critics-card { padding: 18px; }
  .progress-grid { grid-template-columns: 1fr; gap: 10px; }
  .progress-tile { padding: 12px 14px; }
  .findings-head { padding: 14px 16px; }
  .finding { padding: 12px 16px; gap: 10px; }
  .finding-msg { font-size: 13px; }
  .finding-file { font-size: 11.5px; word-break: break-all; }
  .table thead { display: none; }
  .table, .table tbody, .table tr, .table td { display: block; width: 100%; }
  .table tbody tr {
    background: #ffffff;
    border: 1px solid #d2d2d7;
    border-radius: 14px;
    padding: 14px;
    margin-bottom: 10px;
  }
  .table tbody tr:hover    { background: #fbfbfd; }
  .table tbody tr.expanded { background: #f5f5f7; }
  .table tbody td {
    border-bottom: none;
    padding: 4px 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    font-size: 13px;
  }
  .table tbody td::before {
    content: attr(data-label);
    font-size: 11px;
    font-weight: 600;
    color: #86868b;
    letter-spacing: 0.4px;
    text-transform: uppercase;
    flex-shrink: 0;
  }
  .table tbody td:first-child { font-size: 14px; font-weight: 600; padding-bottom: 8px; border-bottom: 1px solid #d2d2d7; margin-bottom: 6px; }
  .table tbody td:nth-child(4),
  .table tbody td.td-summary {
    flex-direction: column !important; align-items: flex-start !important; gap: 4px;
    max-width: none !important; white-space: normal !important; text-overflow: clip !important; overflow: visible !important;
  }
  .table tbody td:nth-child(4)::before,
  .table tbody td.td-summary::before { margin-bottom: 2px; }
  .expand-row { background: transparent; }
  .expand-row td { padding: 0; background: transparent; border: none; }
  .expand-row td::before { display: none; }
  .expand-row td > div { grid-template-columns: 1fr !important; }
  .pagination { flex-direction: column; gap: 10px; padding: 12px 14px; }
  .webhook-url-bar { flex-direction: column; align-items: stretch; gap: 8px; }
  .webhook-url-bar .btn-secondary { width: 100%; }
  .webhook-url-text { font-size: 12px; }
  .steps-list li { font-size: 13px; padding-left: 36px; }
  .toast-wrap { top: 12px; right: 12px; left: 12px; }
  .toast { max-width: none; font-size: 13px; padding: 11px 14px; }
}

/* Small phone : < 480px */
@media (max-width: 480px) {
  .content { padding: 16px 12px; }
  .topbar { padding: 0 10px; height: 48px; }
  .topbar a.platform-link { display: none; }
  .topbar .topbar-actions { margin-left: auto; }
  .detail-title { font-size: 20px; letter-spacing: -0.3px; }
  .detail-head { padding-bottom: 12px; margin-bottom: 14px; }
  .detail-sub { font-size: 12.5px; }
  .card .card-body { padding: 14px; }
  .card .card-head { padding: 10px 14px; }
  .card .card-head-title { font-size: 13px; }
  .diff-input { min-height: 180px; font-size: 11.5px; }
  .gauge { width: 130px; height: 130px; }
  .gauge svg { width: 130px; height: 130px; }
  .gauge-num { font-size: 34px; }
  .gauge-card { padding: 18px; }
  .critics-card { padding: 14px; }
  .finding { padding: 10px 14px; }
  .findings-head { padding: 12px 14px; }
  .table tbody tr { padding: 12px 14px; }
  .brand-sub { display: none; }
  .table tbody td:first-child {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;
    display: block;
  }
}

@media (max-width: 540px) {
  .modal-card { width: calc(100vw - 24px) !important; max-width: 100% !important; margin: 12px !important; }
  .apple-auth-card { width: calc(100vw - 32px) !important; max-width: 100% !important; padding: 28px 20px !important; }
  .toast { width: calc(100vw - 24px) !important; max-width: 100% !important; right: 12px !important; left: 12px !important; }
}
@media (max-width: 480px) {
  .modal-card { padding: 20px !important; }
  .apple-auth-card { padding: 24px 16px !important; }
  .apple-auth-title { font-size: 22px !important; }
  body { font-size: 14px !important; }
  .topbar { padding: 0 12px !important; }
}


.sidebar { height: 100vh; height: 100dvh; }
.repo-tree { flex: 1 1 auto; overflow-y: auto; -webkit-overflow-scrolling: touch; min-height: 0; }
.sidebar-add { flex-shrink: 0; }
.sidebar-foot { flex-shrink: 0; }
.sidebar-head { flex-shrink: 0; }
.sidebar-section-title { flex-shrink: 0; }
@media (max-width: 768px) {
  .sidebar { height: 100vh !important; height: 100dvh !important; max-height: 100vh; max-height: 100dvh; }
  .repo-tree { max-height: none !important; }
}

.gh-input-row { display: flex; gap: 10px; margin-bottom: 16px; }
.gh-card { background: #fff; border: 1px solid #d2d2d7; border-radius: 14px; padding: 20px; margin-top: 12px; }
.gh-header { display: flex; justify-content: space-between; align-items: flex-start; padding-bottom: 14px; border-bottom: 1px solid #e8e8ec; margin-bottom: 14px; }
.gh-name { font-size: 16px; font-weight: 600; color: #1d1d1f; }
.gh-desc { font-size: 12.5px; color: #86868b; margin-top: 4px; }
.gh-stats { display: flex; gap: 12px; font-size: 12.5px; color: #424245; }
.gh-tabs { display: flex; gap: 4px; border-bottom: 1px solid #e8e8ec; margin-bottom: 14px; }
.gh-tabs button { background: none; border: none; padding: 8px 14px; color: #86868b; font-size: 13px; cursor: pointer; border-bottom: 2px solid transparent; }
.gh-tabs button.active { color: #0071e3; border-bottom-color: #0071e3; font-weight: 500; }
.gh-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.gh-table th { text-align: left; padding: 10px 12px; background: #fafafa; color: #86868b; font-weight: 500; border-bottom: 1px solid #e8e8ec; }
.gh-table td { padding: 10px 12px; border-bottom: 1px solid #f0f0f3; color: #1d1d1f; vertical-align: middle; }
.gh-table tr:hover { background: #fafafa; }
.gh-table code { background: #f5f5f7; padding: 1px 6px; border-radius: 4px; font-size: 11.5px; }
.branch-tag { background: #e6f0ff !important; color: #0040a3 !important; }
.btn-mini { background: #0071e3; color: #fff; border: none; border-radius: 980px; padding: 4px 12px; font-size: 11.5px; cursor: pointer; }
.btn-mini:hover { background: #0077ed; }
.empty-state { padding: 40px 20px; text-align: center; color: #86868b; font-size: 13px; }

</style>
</head>
<body>

<div id="app" v-cloak>

  <!-- Toasts -->
  <div class="toast-wrap">
    <div v-for="(t, i) in toasts" :key="i" :class="'toast ' + (t.type||'info')">{{ t.message }}</div>
  </div>

  <!-- ===================== AUTH ===================== -->
  <div v-if="!token" class="apple-auth-page">
    <div class="apple-auth-card">
      <div class="apple-auth-logo">DevBot</div>
      <div class="apple-auth-subtitle">智能评审平台</div>
      <form class="apple-auth-form" @submit.prevent="authTab==='login' ? doLogin() : doRegister()">
        <input v-model="authForm.username" type="text" class="apple-input" :placeholder="authTab==='login' ? '用户名' : '设置用户名'" autocomplete="username">
        <input v-model="authForm.password" type="password" class="apple-input" :placeholder="authTab==='login' ? '密码' : '设置密码'" autocomplete="current-password">
        <button type="submit" class="apple-btn" :disabled="authLoading">
          <span v-if="authLoading" class="apple-spinner"></span>
          {{ authTab === 'login' ? '登录' : '注册' }}
        </button>
        <div class="apple-auth-toggle">
          <span v-if="authTab==='login'" @click="authTab='register'">没有账号？创建账号</span>
          <span v-else @click="authTab='login'">已有账号？登录</span>
        </div>
      </form>
    </div>
  </div>

  <!-- ===================== MAIN ===================== -->
  <div v-else class="shell" :class="{'sidebar-open': sidebarOpen}">

    <!-- Backdrop for mobile drawer -->
    <div class="sidebar-backdrop" @click="sidebarOpen=false"></div>

    <!-- Sidebar -->
    <aside class="sidebar">
      <div class="sidebar-head">
        <div class="brand-mark">D</div>
        <div>
          <div class="brand-title">DevBot</div>
          <div class="brand-sub">智能评审平台</div>
        </div>
      </div>

      <div class="sidebar-section-title">工作台</div>

      <div class="nav-items">
        <div class="nav-item" :class="{active: currentPage==='review'}" @click="goPage('review')">
          <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          新建评审
        </div>
        <div class="nav-item" :class="{active: currentPage==='history'}" @click="goPage('history')">
          <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
          评审历史
        </div>
        <div class="nav-item" :class="{active: currentPage==='github'}" @click="goPage('github')">
          <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/></svg>
          GitHub 仓库
        </div>
        <div class="nav-item" :class="{active: currentPage==='webhook'}" @click="goPage('webhook')">
          <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 16.08c-.76 0-1.44.3-1.96.77L8.91 12.7c.05-.23.09-.46.09-.7s-.04-.47-.09-.7l7.05-4.11c.54.5 1.25.81 2.04.81 1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3c0 .24.04.47.09.7L8.04 9.81C7.5 9.31 6.79 9 6 9c-1.66 0-3 1.34-3 3s1.34 3 3 3c.79 0 1.5-.31 2.04-.81l7.12 4.16c-.05.21-.08.43-.08.65 0 1.61 1.31 2.92 2.92 2.92s2.92-1.31 2.92-2.92-1.31-2.92-2.92-2.92z"/></svg>
          Webhook 设置
        </div>
      </div>

      <div class="sidebar-foot">
        <div class="avatar">{{ (username||'?').charAt(0).toUpperCase() }}</div>
        <div class="user-info">
          <div class="user-name">{{ username }}</div>
          <div class="user-sub">已登录</div>
        </div>
        <button class="btn-icon" @click="doLogout" title="退出">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
        </button>
      </div>
    </aside>

    <!-- Main -->
    <main class="main">
      <header class="topbar">
        <button class="hamburger" @click="sidebarOpen=!sidebarOpen" :aria-label="sidebarOpen ? '关闭菜单' : '打开菜单'">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line v-if="!sidebarOpen" x1="3" y1="6" x2="21" y2="6"/>
            <line v-if="!sidebarOpen" x1="3" y1="12" x2="21" y2="12"/>
            <line v-if="!sidebarOpen" x1="3" y1="18" x2="21" y2="18"/>
            <line v-if="sidebarOpen" x1="18" y1="6" x2="6" y2="18"/>
            <line v-if="sidebarOpen" x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
        <div class="crumb">
          <span>DevBot</span>
          <span>/</span>
          <b>{{ pageLabel(currentPage) }}</b>
        </div>
        <div class="topbar-actions">
          <a href="/platform/" class="platform-link">平台首页</a>
          <a href="/codedoc/" class="platform-link">CodeDoc 知识库</a>
        </div>
      </header>

      <div class="content">

        <!-- ============= REVIEW PAGE ============= -->
        <div v-if="currentPage==='review'">
          <div class="detail-head">
            <div class="detail-title-row">
              <h1 class="detail-title">新建评审</h1>
            </div>
            <div class="detail-sub">提交代码 diff，DevBot 将调度多 Critic（正确性 / 设计 / 安全 / 可读性）并行评审</div>
          </div>

          <!-- Smart URL input — primary -->
          <div class="url-input-card">
            <label>粘贴 GitHub PR 链接</label>
            <div class="url-input-row">
              <input
                v-model="reviewUrl"
                class="url-input"
                placeholder="https://github.com/owner/repo/pull/123 或 owner/repo#123"
                @keydown.enter="fetchFromUrl"
              />
              <button class="btn-primary" @click="fetchFromUrl" :disabled="urlLoading">
                <span v-if="urlLoading" class="apple-spinner" style="width:14px;height:14px;border-width:2px"></span>
                {{ urlLoading ? '拉取中…' : '拉取 Diff' }}
              </button>
            </div>
            <div class="url-hint">支持格式：PR 链接 / Commit 链接 / Compare 链接 / 简写 owner/repo#N</div>
          </div>

          <!-- After fetch: preview + start review -->
          <div v-if="diffData" class="diff-preview-card">
            <div class="diff-header">
              <div style="flex:1;min-width:0">
                <div class="diff-title">{{ diffData.title || '（无标题）' }}</div>
                <div class="diff-meta">
                  <span class="badge">{{ diffData.pr_id }}</span>
                  <span>{{ diffData.stats.files }} 文件</span>
                  <span class="added">+{{ diffData.stats.additions }}</span>
                  <span class="removed">-{{ diffData.stats.deletions }}</span>
                </div>
              </div>
              <a v-if="diffData.url" :href="diffData.url" target="_blank" class="link-icon" title="在 GitHub 中查看">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
                  <polyline points="15 3 21 3 21 9"/>
                  <line x1="10" y1="14" x2="21" y2="3"/>
                </svg>
              </a>
            </div>

            <pre class="diff-preview">{{ diffData.diff.slice(0, 3000) }}{{ diffData.diff.length > 3000 ? '\n\n…（截断显示，提交后会评审全部）' : '' }}</pre>

            <div class="review-actions">
              <select v-model="diffData.language">
                <option value="python">Python</option>
                <option value="java">Java</option>
                <option value="go">Go</option>
                <option value="typescript">TypeScript</option>
                <option value="javascript">JavaScript</option>
                <option value="rust">Rust</option>
                <option value="cpp">C++</option>
                <option value="">自动检测</option>
              </select>
              <button class="btn-primary-large" @click="submitReview" :disabled="reviewing">
                <span v-if="reviewing" class="apple-spinner" style="width:14px;height:14px;border-width:2px"></span>
                {{ reviewing ? '评审进行中…' : '开始评审 →' }}
              </button>
            </div>
          </div>

          <!-- Advanced: manual paste / file upload (collapsed) -->
          <details class="advanced-options">
            <summary>高级选项（手动粘贴 Diff / 上传文件）</summary>
            <div class="adv-body">
              <div class="mode-tabs">
                <button class="mode-tab" :class="{active:reviewMode==='manual'}" @click="reviewMode='manual'">手动粘贴</button>
                <button class="mode-tab" :class="{active:reviewMode==='upload'}" @click="reviewMode='upload'">上传文件</button>
              </div>

              <div v-if="reviewMode==='upload'" class="field">
                <input ref="diffFileInput" type="file" accept=".diff,.patch" style="display:none" @change="onDiffFilePicked">
                <div class="drop-zone"
                     :class="{'has-file': !!uploadedDiffFileName, dragging: diffDragOver}"
                     @click="$refs.diffFileInput && $refs.diffFileInput.click()"
                     @dragover.prevent="diffDragOver=true"
                     @dragleave.prevent="diffDragOver=false"
                     @drop.prevent="onDiffFileDropped">
                  <svg v-if="!uploadedDiffFileName" class="drop-icon" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                    <polyline points="17 8 12 3 7 8"/>
                    <line x1="12" y1="3" x2="12" y2="15"/>
                  </svg>
                  <div v-if="!uploadedDiffFileName" class="drop-text">拖拽 .diff / .patch 文件到此处或点击选择</div>
                  <div v-if="!uploadedDiffFileName" class="drop-hint">（最大 5MB · 文件内容将自动填充到下方 diff 输入框）</div>
                  <div v-else class="picked-file">
                    <div class="picked-name">📄 {{ uploadedDiffFileName }}</div>
                    <div class="picked-size">{{ uploadedDiffFileSize }}</div>
                    <button class="picked-clear" @click.stop="clearDiffFile">移除</button>
                  </div>
                </div>
              </div>

              <div class="field" style="margin-top:14px">
                <label class="field-label">Diff 内容</label>
                <textarea v-model="reviewForm.diff" class="diff-input" placeholder="粘贴你的 diff 内容…"></textarea>
                <div v-if="reviewForm.diff.trim()" style="margin-top:10px">
                  <div style="font-size:12px;color:var(--text-3);margin-bottom:6px">📋 Diff 预览（语法高亮）</div>
                  <div class="diff-viewer" style="max-height:280px;overflow:auto">
                    <div v-for="(ln, i) in parseDiffLines(reviewForm.diff)" :key="i" class="diff-line" :class="ln.kind">
                      <div class="ln">{{ ln.no || '' }}</div>
                      <div class="code">{{ ln.text }}</div>
                    </div>
                  </div>
                </div>
              </div>

              <div class="field-row">
                <div>
                  <label class="field-label">PR 标题（可选）</label>
                  <input v-model="reviewForm.title" class="text-input" placeholder="PR 标题">
                </div>
                <div>
                  <label class="field-label">主要语言</label>
                  <select v-model="reviewForm.language" class="text-input">
                    <option value="java">Java</option>
                    <option value="python">Python</option>
                    <option value="go">Go</option>
                    <option value="typescript">TypeScript</option>
                    <option value="javascript">JavaScript</option>
                    <option value="rust">Rust</option>
                    <option value="cpp">C++</option>
                    <option value="">自动检测</option>
                  </select>
                </div>
              </div>

              <button @click="startReview" :disabled="reviewing" class="btn-primary btn-block" style="margin-top:8px">
                <span v-if="reviewing" class="apple-spinner" style="width:14px;height:14px;border-width:2px"></span>
                <svg v-else width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                {{ reviewing ? '评审进行中…' : '使用此 Diff 评审' }}
              </button>
            </div>
          </details>

          <!-- Progress -->
          <div v-if="taskId" class="card" style="margin-bottom:18px">
            <div class="card-head">
              <span class="card-head-title">⚙️ 评审进度</span>
              <span class="tag" :class="{LOW:taskStatus==='done',MEDIUM:taskStatus==='running'||taskStatus==='queued',HIGH:taskStatus==='error'}">
                {{ ({queued:'排队中',running:'评审中',done:'已完成',error:'出错'})[taskStatus] || taskStatus }}
              </span>
            </div>
            <div class="card-body">
              <div class="progress-grid">
                <div v-for="c in criticNames" :key="c" class="progress-tile" :class="taskStatus==='done'?'done':(taskStatus==='running'?'running':'')">
                  <span class="progress-dot" :class="taskStatus==='done'?'done':(taskStatus==='running'?'running':'waiting')"></span>
                  <div>
                    <div class="name">{{ criticLabels[c] }}</div>
                    <div style="font-size:11px;color:var(--text-3);margin-top:2px">{{ criticIcons[c] }}-Critic</div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- Result -->
          <div v-if="reviewResult">
            <div class="dashboard">
              <!-- Risk gauge -->
              <div class="gauge-card">
                <div class="gauge">
                  <svg viewBox="0 0 200 200" width="180" height="180">
                    
                    <circle class="gauge-circle-bg" cx="100" cy="100" r="86"/>
                    <circle class="gauge-circle" cx="100" cy="100" r="86"
                      :stroke="riskGradientUrl(reviewResult.risk_score)"
                      :stroke-dasharray="540"
                      :stroke-dashoffset="540 - (540 * reviewResult.risk_score / 100)"/>
                  </svg>
                  <div class="gauge-center">
                    <div class="gauge-num" :style="{background: riskGradient(reviewResult.risk_score), '-webkit-background-clip': 'text', 'background-clip': 'text', '-webkit-text-fill-color': 'transparent', color: 'transparent'}">{{ reviewResult.risk_score }}</div>
                    <div class="gauge-divisor">/ 100 风险分</div>
                  </div>
                </div>
                <div class="risk-pill" :class="reviewResult.risk_level">{{ riskLevelLabel(reviewResult.risk_level) }}</div>
                <div class="gauge-summary">{{ reviewResult.summary }}</div>
              </div>

              <!-- Critics breakdown -->
              <div class="critics-card">
                <div class="critics-card-title">Critic 评估明细</div>
                <div v-for="c in reviewResult.critics" :key="c.critic" class="critic-bar-row">
                  <div class="critic-bar-head">
                    <div class="critic-bar-name">
                      <span class="critic-tag" :class="c.critic">{{ criticIcons[c.critic] }}</span>
                      {{ criticLabels[c.critic] || c.critic }}
                    </div>
                    <span class="critic-bar-score" :style="{background: riskGradient(c.risk_score), '-webkit-background-clip': 'text', 'background-clip': 'text', '-webkit-text-fill-color': 'transparent', color: 'transparent'}">{{ c.risk_score }}</span>
                  </div>
                  <div class="critic-bar-track">
                    <div class="critic-bar-fill" :style="{width: c.risk_score+'%', background: riskGradient(c.risk_score), boxShadow: '0 4px 12px -3px ' + riskColor(c.risk_score) + '55'}"></div>
                  </div>
                  <div class="critic-bar-foot">{{ c.model || '—' }} · {{ c.latency_ms }}ms · {{ (c.findings||[]).length }} 个发现</div>
                </div>
              </div>
            </div>

            <!-- All findings -->
            <div class="findings-list" style="margin-bottom:20px">
              <div class="findings-head">
                <span class="findings-head-title">🔍 发现的问题</span>
                <span class="findings-head-count">{{ totalFindings(reviewResult) }} 项</span>
              </div>
              <div v-if="totalFindings(reviewResult) === 0" class="empty">
                <div class="empty-icon">✨</div>
                <div class="empty-title">未发现明显问题</div>
                <div class="empty-sub">各项 Critic 均未给出严重警告</div>
              </div>
              <template v-for="c in reviewResult.critics" :key="'cf-'+c.critic">
                <div v-for="(f, fi) in (c.findings||[])" :key="c.critic+'-'+fi" class="finding">
                  <span class="sev" :class="f.severity">{{ f.severity }}</span>
                  <div class="finding-body">
                    <div v-if="f.file" class="finding-file">{{ f.file }}{{ f.line ? ':' + f.line : '' }}</div>
                    <div class="finding-msg">{{ f.message }}</div>
                    <div class="finding-by">来自 {{ criticLabels[c.critic] || c.critic }} Critic</div>
                  </div>
                </div>
              </template>
            </div>

            <!-- Suggestions -->
            <div class="card">
              <div class="card-head"><span class="card-head-title">💡 Critic 建议汇总</span></div>
              <div class="card-body" style="padding:18px 22px">
                <div v-for="c in reviewResult.critics" :key="'s-'+c.critic" style="margin-bottom:12px">
                  <div style="display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--text);margin-bottom:4px">
                    <span class="critic-tag" :class="c.critic">{{ criticIcons[c.critic] }}</span>
                    {{ criticLabels[c.critic] }}
                  </div>
                  <div class="suggestion" v-if="c.suggestion">{{ c.suggestion }}</div>
                </div>
              </div>
            </div>
          </div>

        </div>

        <!-- ============= HISTORY PAGE ============= -->
        <div v-if="currentPage==='history'">
          <div class="detail-head">
            <div class="detail-title-row">
              <h1 class="detail-title">评审历史</h1>
              <button @click="loadHistory" class="btn-secondary" style="margin-left:auto">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;margin-right:4px"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                刷新
              </button>
            </div>
            <div class="detail-sub">所有提交过的代码评审记录</div>
          </div>

          <div class="card">
            <div v-if="historyLoading" class="loading-block"><div class="spinner-lg"></div></div>
            <div v-else-if="!historyItems.length" class="empty">
              <div class="empty-icon">📜</div>
              <div class="empty-title">还没有评审记录</div>
              <div class="empty-sub">点击左侧"新建评审"开始</div>
            </div>
            <div v-else style="overflow-x:auto">
              <table class="table">
                <thead>
                  <tr>
                    <th class="sortable" @click="setSort('pr_id')">PR <span class="sort-ic">{{ sortKey==='pr_id' ? (sortAsc?'▲':'▼') : '↕' }}</span></th>
                    <th class="sortable" @click="setSort('risk_level')">风险等级 <span class="sort-ic">{{ sortKey==='risk_level' ? (sortAsc?'▲':'▼') : '↕' }}</span></th>
                    <th class="sortable" @click="setSort('risk_score')">评分 <span class="sort-ic">{{ sortKey==='risk_score' ? (sortAsc?'▲':'▼') : '↕' }}</span></th>
                    <th>摘要</th>
                    <th class="sortable" @click="setSort('created_at')">提交时间 <span class="sort-ic">{{ sortKey==='created_at' ? (sortAsc?'▲':'▼') : '↕' }}</span></th>
                  </tr>
                </thead>
                <tbody>
                  <template v-for="item in sortedHistory" :key="item.id">
                    <tr :class="{expanded: expandedHistoryId === item.id}" @click="expandedHistoryId = expandedHistoryId === item.id ? null : item.id">
                      <td data-label="PR" style="font-weight:500">{{ item.pr_id }}</td>
                      <td data-label="风险"><span class="tag" :class="item.risk_level">{{ item.risk_level }}</span></td>
                      <td data-label="评分" style="font-weight:800;font-size:14px" :style="{background: riskGradient(item.risk_score), '-webkit-background-clip': 'text', 'background-clip': 'text', '-webkit-text-fill-color': 'transparent', color: 'transparent'}">{{ item.risk_score }}</td>
                      <td data-label="摘要" class="td-summary" style="color:var(--text-2);max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ item.summary }}</td>
                      <td data-label="提交时间" style="color:var(--text-3);font-size:12.5px">{{ formatDate(item.created_at) }}</td>
                    </tr>
                    <tr v-if="expandedHistoryId === item.id" class="expand-row">
                      <td colspan="5">
                        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
                          <div v-for="c in item.critics" :key="c.critic" style="background:rgba(255,255,255,0.85);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border:1px solid var(--border);border-radius:12px;padding:14px;box-shadow:0 4px 14px -8px rgba(99,102,241,0.15)">
                            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
                              <div style="display:flex;align-items:center;gap:6px;font-size:13px;font-weight:600">
                                <span class="critic-tag" :class="c.critic" style="width:18px;height:18px;font-size:10px">{{ criticIcons[c.critic] }}</span>
                                {{ criticLabels[c.critic] || c.critic }}
                              </div>
                              <span style="font-weight:800;font-size:13px" :style="{background: riskGradient(c.risk_score), '-webkit-background-clip': 'text', 'background-clip': 'text', '-webkit-text-fill-color': 'transparent', color: 'transparent'}">{{ c.risk_score }}</span>
                            </div>
                            <div class="critic-bar-track"><div class="critic-bar-fill" :style="{width: c.risk_score+'%', background: riskGradient(c.risk_score)}"></div></div>
                            <div style="margin-top:8px;font-size:12px;color:var(--text-2);line-height:1.6">{{ c.suggestion || '—' }}</div>
                          </div>
                        </div>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>
              <div v-if="historyTotal > historyPageSize" class="pagination">
                <span style="font-size:12px;color:var(--text-3)">共 {{ historyTotal }} 条 · 第 {{ historyPage }} / {{ Math.ceil(historyTotal/historyPageSize) }} 页</span>
                <div class="pages">
                  <button class="pg-btn" :disabled="historyPage<=1" @click="historyPage--; loadHistory()">上一页</button>
                  <button class="pg-btn" :disabled="historyPage>=Math.ceil(historyTotal/historyPageSize)" @click="historyPage++; loadHistory()">下一页</button>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- ============= WEBHOOK PAGE ============= -->
        <div v-if="currentPage==='webhook'">
          <div class="detail-head">
            <div class="detail-title-row">
              <h1 class="detail-title">Webhook 设置</h1>
            </div>
            <div class="detail-sub">配置 GitHub Webhook，让每次 PR 创建或更新都自动触发评审</div>
          </div>

          <div class="card" style="margin-bottom:18px">
            <div class="card-head"><span class="card-head-title">📡 Webhook 地址</span></div>
            <div class="card-body">
              <div class="webhook-url-bar">
                <span class="webhook-url-text">https://124.222.50.21/devbot/webhook/github</span>
                <button @click="copyWebhookUrl" class="btn-secondary">复制</button>
              </div>

              <div style="margin-top:14px">
                <div style="font-size:13.5px;font-weight:600;margin-bottom:10px">配置步骤</div>
                <ol class="steps-list">
                  <li v-for="(step, i) in webhookSteps" :key="i" v-html="step"></li>
                </ol>
              </div>

              <div style="margin-top:18px;padding-top:18px;border-top:1px solid var(--border)">
                <div style="font-size:13.5px;font-weight:600;margin-bottom:6px">连通性测试</div>
                <div style="font-size:13px;color:var(--text-3);margin-bottom:12px">向 DevBot 发送测试请求，验证服务运行状态</div>
                <button @click="testWebhook" :disabled="webhookTesting" class="btn-dark">
                  <span v-if="webhookTesting" class="apple-spinner" style="width:13px;height:13px;border-width:2px"></span>
                  {{ webhookTesting ? '测试中…' : '测试连接' }}
                </button>
                <div v-if="webhookTestResult" class="test-result" :class="webhookTestResult.ok?'ok':'fail'">
                  <span v-if="webhookTestResult.ok">✓</span>
                  <span v-else>✕</span>
                  <span>{{ webhookTestResult.message }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

      </div>
    </main>
  </div>

</div>

<script>
const { createApp, ref, reactive, computed, onMounted, watch } = Vue;
const BASE = '/devbot';

createApp({
  setup() {
    const token = ref(localStorage.getItem('devbot_token') || '');
    const username = ref(localStorage.getItem('devbot_username') || '');
    const authTab = ref('login');
    const authForm = reactive({ username: '', password: '' });
    const authLoading = ref(false);

    const currentPage = ref('review');
    const sidebarOpen = ref(false);
    const toasts = ref([]);
    function showToast(message, type='info') {
      const t = { message, type };
      toasts.value.push(t);
      setTimeout(()=>{ const i=toasts.value.indexOf(t); if(i>=0) toasts.value.splice(i,1); }, 3500);
    }
    function pageLabel(p){ return ({review:'新建评审',history:'评审历史',github:'GitHub 仓库',webhook:'Webhook 设置'})[p] || p; }
    function goPage(p){
      currentPage.value=p;
      // Auto-close mobile drawer
      if (window.innerWidth <= 768) sidebarOpen.value = false;
      if(p==='history') loadHistory();
    }

    async function api(method, path, body=null) {
      const opts = { method, headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token.value } };
      if (body) opts.body = JSON.stringify(body);
      const r = await fetch(BASE + path, opts);
      let data;
      try { data = await r.json(); } catch { data = {}; }
      if (!r.ok) {
        const msg = data.detail || data.error || '请求失败';
        if (r.status === 401) doLogout();
        throw new Error(msg);
      }
      return data;
    }

    async function doLogin() {
      authLoading.value = true;
      try {
        const data = await fetch(BASE + '/api/v1/auth/login', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify(authForm)
        }).then(r => r.json().then(d => { if (!r.ok) throw new Error(d.detail || '登录失败'); return d; }));
        token.value = data.token; username.value = data.username;
        localStorage.setItem('devbot_token', data.token);
        localStorage.setItem('devbot_username', data.username);
        showToast('欢迎回来 · ' + data.username, 'success');
      } catch (e) { showToast(e.message, 'error'); }
      authLoading.value = false;
    }
    async function doRegister() {
      authLoading.value = true;
      try {
        const data = await fetch(BASE + '/api/v1/auth/register', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify(authForm)
        }).then(r => r.json().then(d => { if (!r.ok) throw new Error(d.detail || '注册失败'); return d; }));
        token.value = data.token; username.value = data.username;
        localStorage.setItem('devbot_token', data.token);
        localStorage.setItem('devbot_username', data.username);
        showToast('注册成功 · 欢迎使用', 'success');
      } catch (e) { showToast(e.message, 'error'); }
      authLoading.value = false;
    }
    function doLogout() {
      token.value=''; username.value='';
      localStorage.removeItem('devbot_token');
      localStorage.removeItem('devbot_username');
    }

    // Review
    const reviewMode = ref('manual');
    const reviewForm = reactive({ repo:'', prNumber:'', diff:'', title:'', language:'python' });
    const fetchingPR = ref(false);
    const reviewing = ref(false);
    // Smart URL input state
    const reviewUrl = ref('');
    const diffData = ref(null);
    const urlLoading = ref(false);

    async function fetchFromUrl() {
      const u = reviewUrl.value.trim();
      if (!u) { showToast('请粘贴 PR 链接或 owner/repo#N', 'error'); return; }
      urlLoading.value = true;
      try {
        const r = await api('POST', '/api/v1/review/from-url', { url: u });
        // Normalize language for select
        const langMap = { python:'python', java:'java', go:'go', typescript:'typescript', javascript:'javascript', rust:'rust', 'c++':'cpp', cpp:'cpp' };
        const detected = (r.language || '').toLowerCase();
        r.language = langMap[detected] || '';
        diffData.value = r;
        reviewResult.value = null;
        taskId.value = null;
        showToast(`已拉取 ${r.stats.files} 个文件的 diff（+${r.stats.additions} / -${r.stats.deletions}）`, 'success');
      } catch (e) {
        showToast('拉取失败: ' + e.message, 'error');
      } finally {
        urlLoading.value = false;
      }
    }

    async function submitReview() {
      if (!diffData.value || !diffData.value.diff) {
        showToast('请先拉取 diff', 'error');
        return;
      }
      reviewing.value = true;
      reviewResult.value = null;
      taskId.value = null;
      taskStatus.value = '';
      try {
        const data = await api('POST', '/api/v1/review', {
          pr_id: diffData.value.pr_id,
          diff: diffData.value.diff,
          title: diffData.value.title || '',
          language: diffData.value.language || 'python',
        });
        taskId.value = data.task_id;
        taskStatus.value = data.status;
        showToast('评审任务已提交', 'success');
        pollTaskStatus();
      } catch (e) {
        showToast('提交失败: ' + e.message, 'error');
        reviewing.value = false;
      }
    }
    // Diff file upload state
    const diffFileInput = ref(null);
    const uploadedDiffFileName = ref('');
    const uploadedDiffFileSize = ref('');
    const diffDragOver = ref(false);

    function humanSize(n) {
      if (n < 1024) return n + ' B';
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
      return (n / 1024 / 1024).toFixed(2) + ' MB';
    }
    function clearDiffFile() {
      uploadedDiffFileName.value = '';
      uploadedDiffFileSize.value = '';
    }
    function onDiffFilePicked(e) {
      const f = e.target.files && e.target.files[0];
      if (f) acceptDiffFile(f);
      e.target.value = '';
    }
    function onDiffFileDropped(e) {
      diffDragOver.value = false;
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) acceptDiffFile(f);
    }
    function acceptDiffFile(f) {
      if (!/\.(diff|patch)$/i.test(f.name)) {
        showToast('请选择 .diff 或 .patch 文件', 'error');
        return;
      }
      if (f.size > 5 * 1024 * 1024) {
        showToast('文件大小不能超过 5MB', 'error');
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        reviewForm.diff = reader.result || '';
        uploadedDiffFileName.value = f.name;
        uploadedDiffFileSize.value = humanSize(f.size);
        if (!reviewForm.title) reviewForm.title = f.name;
        showToast('已加载 ' + f.name + '，可点击下方"开始评审"', 'success');
      };
      reader.onerror = () => showToast('读取文件失败', 'error');
      reader.readAsText(f, 'utf-8');
    }
    const taskId = ref(null);
    const taskStatus = ref('');
    const reviewResult = ref(null);
    let pollTimer = null;

    const criticNames = ['correctness','design','security','readability'];
    const criticLabels = { correctness:'正确性', design:'架构设计', security:'安全性', readability:'可读性' };
    const criticIcons  = { correctness:'C', design:'D', security:'S', readability:'R' };

    function riskColor(s){ if(s>=70) return '#f43f5e'; if(s>=40) return '#f59e0b'; return '#10b981'; }
    function riskGradient(s){
      if(s>=70) return '#ff3b30';
      if(s>=40) return '#ff9f0a';
      return '#34c759';
    }
    function riskGradientUrl(s){
      if(s>=70) return '#ff3b30';
      if(s>=40) return '#ff9f0a';
      return '#34c759';
    }
    function riskLevelLabel(l){ return ({LOW:'低风险 · 可合并', MEDIUM:'中风险 · 建议复核', HIGH:'高风险 · 需要人工审查'})[l] || l; }
    function totalFindings(r){ if(!r||!r.critics) return 0; return r.critics.reduce((a,c)=>a+(c.findings?c.findings.length:0),0); }

    function parseDiffLines(text){
      const lines = (text||'').split('\n');
      const out = [];
      let oldNo = 0, newNo = 0;
      for (const line of lines) {
        if (line.startsWith('@@')) {
          const m = line.match(/@@ -(\d+),?\d* \+(\d+),?\d* @@/);
          if (m) { oldNo = parseInt(m[1]) - 1; newNo = parseInt(m[2]) - 1; }
          out.push({ kind:'hunk', no:'', text:line });
        } else if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ') || line.startsWith('index ')) {
          out.push({ kind:'meta', no:'', text:line });
        } else if (line.startsWith('+') && !line.startsWith('+++')) {
          newNo++; out.push({ kind:'add', no:newNo, text:line });
        } else if (line.startsWith('-') && !line.startsWith('---')) {
          oldNo++; out.push({ kind:'del', no:oldNo, text:line });
        } else {
          oldNo++; newNo++; out.push({ kind:'ctx', no:newNo, text:line });
        }
      }
      return out;
    }

    async function fetchPR() {
      const repo = reviewForm.repo.trim();
      const pr = reviewForm.prNumber;
      if (!repo || !pr) { showToast('请填写仓库和 PR 编号', 'error'); return; }
      if (!repo.includes('/')) { showToast('仓库格式应为 owner/repo', 'error'); return; }
      fetchingPR.value = true;
      try {
        const data = await api('GET', `/api/v1/github/${repo}/pulls/${pr}`);
        if (data.error) throw new Error(data.error);
        reviewForm.diff = data.diff || '';
        reviewForm.title = data.title || '';
        showToast(`已拉取 PR #${pr}（+${data.additions||0} / -${data.deletions||0}）`, 'success');
      } catch(e) { showToast('拉取失败: '+e.message, 'error'); }
      fetchingPR.value = false;
    }

    
    const ghOwnerRepo = ref('');
    const ghRepo = ref(null);
    const ghPrs = ref([]);
    const ghCommits = ref([]);
    const ghBranches = ref([]);
    const ghTab = ref('prs');
    const ghLoading = ref(false);

    function fmtGhDate(s) {
      if (!s) return '';
      const d = new Date(s);
      const now = new Date();
      const ms = now - d;
      if (ms < 60000) return '刚刚';
      if (ms < 3600000) return Math.floor(ms/60000) + '分钟前';
      if (ms < 86400000) return Math.floor(ms/3600000) + '小时前';
      if (ms < 604800000) return Math.floor(ms/86400000) + '天前';
      return d.toISOString().slice(0,10);
    }

    async function loadGhRepo() {
      const v = ghOwnerRepo.value.trim();
      if (!v.includes('/')) { toast.value = '格式：owner/repo'; return; }
      const [owner, repo] = v.split('/');
      ghLoading.value = true;
      try {
        const info = await api('/api/v1/github/' + owner + '/' + repo + '/info');
        ghRepo.value = info;
        await setGhTab('prs');
      } catch(e) {
        toast.value = '加载失败: ' + e.message;
        ghRepo.value = null;
      } finally {
        ghLoading.value = false;
      }
    }

    async function setGhTab(tab) {
      ghTab.value = tab;
      if (!ghRepo.value) return;
      const [owner, repo] = ghOwnerRepo.value.trim().split('/');
      try {
        if (tab === 'prs') {
          const r = await api('/api/v1/github/' + owner + '/' + repo + '/prs?state=open');
          ghPrs.value = r.prs || [];
        } else if (tab === 'commits') {
          const r = await api('/api/v1/github/' + owner + '/' + repo + '/commits');
          ghCommits.value = r.commits || [];
        } else if (tab === 'branches') {
          const r = await api('/api/v1/github/' + owner + '/' + repo + '/branches');
          ghBranches.value = r.branches || [];
        }
      } catch(e) {
        toast.value = '加载失败: ' + e.message;
      }
    }

    async function reviewGhPr(pr) {
      const v = ghOwnerRepo.value.trim();
      try {
        const r = await api('/api/v1/review/from-url', {
          method: 'POST',
          body: JSON.stringify({ url: v + '#' + pr.number }),
        });
        await api('/api/v1/review', {
          method: 'POST',
          body: JSON.stringify({ pr_id: r.pr_id, diff: r.diff, title: r.title, language: r.language }),
        });
        toast.value = 'PR #' + pr.number + ' 已加入评审队列';
        setTimeout(() => toast.value = '', 3500);
      } catch(e) { toast.value = '提交失败: ' + e.message; }
    }

    async function reviewGhCommit(c) {
      const v = ghOwnerRepo.value.trim();
      try {
        const r = await api('/api/v1/review/from-url', {
          method: 'POST',
          body: JSON.stringify({ url: 'https://github.com/' + v + '/commit/' + c.sha }),
        });
        await api('/api/v1/review', {
          method: 'POST',
          body: JSON.stringify({ pr_id: r.pr_id, diff: r.diff, title: r.title, language: r.language }),
        });
        toast.value = 'Commit ' + c.short_sha + ' 已加入评审队列';
        setTimeout(() => toast.value = '', 3500);
      } catch(e) { toast.value = '提交失败: ' + e.message; }
    }

    async function startReview() {
      const diff = reviewForm.diff.trim();
      if (!diff) { showToast('请先填入 diff 内容', 'error'); return; }
      reviewing.value = true;
      reviewResult.value = null;
      taskId.value = null;
      taskStatus.value = '';
      const prId = (reviewForm.repo && reviewForm.prNumber) ? `${reviewForm.repo}#${reviewForm.prNumber}` : 'manual-review-' + Date.now().toString(36).slice(-4);
      try {
        const data = await api('POST', '/api/v1/review', {
          pr_id: prId, diff, title: reviewForm.title, language: reviewForm.language,
        });
        taskId.value = data.task_id;
        taskStatus.value = data.status;
        pollTaskStatus();
      } catch(e) {
        showToast('提交失败: ' + e.message, 'error');
        reviewing.value = false;
      }
    }

    function pollTaskStatus() {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(async () => {
        try {
          const data = await api('GET', `/api/v1/review/${taskId.value}/status`);
          taskStatus.value = data.status;
          if (data.status === 'done') {
            clearInterval(pollTimer); pollTimer = null;
            reviewResult.value = data.result;
            reviewing.value = false;
            showToast('评审完成', 'success');
          } else if (data.status === 'error') {
            clearInterval(pollTimer); pollTimer = null;
            reviewing.value = false;
            showToast('评审出错: ' + (data.error || '未知错误'), 'error');
          }
        } catch (e) {
          clearInterval(pollTimer); pollTimer = null;
          reviewing.value = false;
        }
      }, 1500);
    }

    // History
    const historyItems = ref([]);
    const historyTotal = ref(0);
    const historyPage = ref(1);
    const historyPageSize = ref(20);
    const historyLoading = ref(false);
    const expandedHistoryId = ref(null);
    const sortKey = ref('created_at');
    const sortAsc = ref(false);

    async function loadHistory() {
      historyLoading.value = true;
      try {
        const data = await api('GET', `/api/v1/reviews?page=${historyPage.value}&page_size=${historyPageSize.value}`);
        historyItems.value = data.items || [];
        historyTotal.value = data.total || 0;
      } catch(e) { showToast('加载历史失败: ' + e.message, 'error'); }
      historyLoading.value = false;
    }
    function setSort(k){ if (sortKey.value === k) sortAsc.value = !sortAsc.value; else { sortKey.value = k; sortAsc.value = true; } }
    const sortedHistory = computed(() => {
      const arr = [...historyItems.value];
      arr.sort((a, b) => {
        const av = a[sortKey.value]; const bv = b[sortKey.value];
        if (av === bv) return 0;
        const r = av > bv ? 1 : -1;
        return sortAsc.value ? r : -r;
      });
      return arr;
    });

    function formatDate(s){
      if (!s) return '';
      try {
        const d = new Date(s.replace(' ','T') + (s.includes('Z')?'':'Z'));
        const now = new Date();
        const diff = now - d;
        if (diff < 60000) return '刚刚';
        if (diff < 3600000) return Math.floor(diff/60000) + ' 分钟前';
        if (diff < 86400000) return Math.floor(diff/3600000) + ' 小时前';
        return d.toLocaleDateString('zh-CN', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' });
      } catch { return s; }
    }

    // Webhook
    const webhookTesting = ref(false);
    const webhookTestResult = ref(null);
    const webhookSteps = [
      '打开 GitHub 仓库 → <b>Settings</b> → <b>Webhooks</b> → <b>Add webhook</b>',
      '<b>Payload URL</b> 中填写上方的 Webhook 地址',
      '<b>Content type</b> 选择 <code>application/json</code>',
      '<b>Secret</b> 填写你配置的密钥（默认 <code>dev-secret</code>）',
      '在事件选择中，勾选 <b>Pull requests</b>',
      '点击 <b>Add webhook</b> 完成配置',
    ];
    function copyWebhookUrl(){
      navigator.clipboard.writeText('https://124.222.50.21/devbot/webhook/github').then(
        () => showToast('已复制到剪贴板', 'success'),
        () => showToast('复制失败', 'error')
      );
    }
    async function testWebhook(){
      webhookTesting.value = true; webhookTestResult.value = null;
      try {
        const r = await fetch(BASE + '/health');
        const data = await r.json();
        webhookTestResult.value = { ok: data.status === 'ok', message: data.status === 'ok' ? '服务运行正常，Webhook 端点已就绪' : '服务异常: ' + JSON.stringify(data) };
      } catch(e) { webhookTestResult.value = { ok:false, message:'连接失败: '+e.message }; }
      webhookTesting.value = false;
    }

    watch(currentPage, (p) => { if (p === 'history') loadHistory(); });

    onMounted(() => {
      if (token.value) api('GET','/api/v1/reviews?page=1&page_size=1').catch(()=>{});
    });

    return {
      token, username, authTab, authForm, authLoading,
      doLogin, doRegister, doLogout,
      currentPage, sidebarOpen, toasts, pageLabel, goPage,
      reviewMode, ghOwnerRepo, ghRepo, ghPrs, ghCommits, ghBranches, ghTab, ghLoading, loadGhRepo, setGhTab, reviewGhPr, reviewGhCommit, fmtGhDate, reviewForm, fetchingPR, reviewing,
      reviewUrl, diffData, urlLoading, fetchFromUrl, submitReview,
      diffFileInput, uploadedDiffFileName, uploadedDiffFileSize, diffDragOver,
      onDiffFilePicked, onDiffFileDropped, clearDiffFile,
      taskId, taskStatus, reviewResult,
      criticNames, criticLabels, criticIcons,
      fetchPR, startReview, parseDiffLines, totalFindings,
      historyItems, historyTotal, historyPage, historyPageSize, historyLoading, expandedHistoryId,
      sortKey, sortAsc, sortedHistory, setSort, loadHistory,
      webhookTesting, webhookTestResult, webhookSteps,
      copyWebhookUrl, testWebhook,
      riskColor, riskGradient, riskGradientUrl, riskLevelLabel, formatDate,
    };
  }
}).mount('#app');
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return SPA_HTML
