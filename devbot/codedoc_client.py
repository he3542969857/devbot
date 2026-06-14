"""Codedoc 客户端 —— server-to-server 复用 codedoc 的检索能力(search / get_body / impact)。

http 模式打 codedoc 的 `/tools/*`(内部 key 鉴权);**任何错误都优雅降级**(返回空 / mock),
绝不让 devbot 因 codedoc 抖动而崩。mock 模式用于离线测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import CodedocCfg, get_settings


@dataclass
class ImpactResult:
    affected_nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


class CodedocClient:
    def __init__(self, cfg: CodedocCfg | None = None):
        self.cfg = cfg or get_settings().codedoc

    def _headers(self) -> dict:
        return {"x-internal-key": getattr(self.cfg, "internal_key", "CHANGE_ME_INTERNAL_KEY")}

    def _post(self, path: str, payload: dict) -> dict:
        import httpx
        # trust_env=False:本机 server-to-server,绝不走任何 HTTP(S)_PROXY,避免被代理误伤
        resp = httpx.post(f"{self.cfg.base_url}{path}", json=payload,
                          headers=self._headers(), timeout=self.cfg.timeout_seconds,
                          trust_env=False)
        resp.raise_for_status()
        return resp.json()

    # ---- 语义+全文检索:找相似实现 / 相关符号(codegen 接地用) ----
    def search(self, repo: str, query: str, top_k: int = 8) -> list[dict]:
        if self.cfg.mode == "mock" or not repo:
            return []
        try:
            return self._post("/tools/search",
                              {"repo": repo, "query": query, "top_k": top_k}).get("items", [])
        except Exception:
            return []

    # ---- 按 node_id 取真实函数体(当 API / 风格参考) ----
    def get_body(self, repo: str, node_id: str, max_lines: int = 50) -> str:
        if self.cfg.mode == "mock" or not repo or not node_id:
            return ""
        try:
            d = self._post("/tools/get_body",
                           {"repo": repo, "node_id": node_id, "max_lines": max_lines})
            return d.get("body", "") if d.get("ok", True) else ""
        except Exception:
            return ""

    # ---- 变更影响子图(review / codegen 集成点) ----
    def get_impact(self, files: list[str], repo: str = "") -> ImpactResult:
        if self.cfg.mode == "mock":
            return self._mock_impact(files)
        try:
            data = self._post("/tools/impact", {"repo": repo, "files": files})
            return ImpactResult(data.get("items", []), data.get("edges", []), data.get("summary", ""))
        except Exception:
            return ImpactResult([], [], "")

    def _mock_impact(self, files: list[str]) -> ImpactResult:
        nodes = [{"id": f"mock_{f.replace('/', '_')}", "kind": "class",
                  "qualified_name": f, "file": f} for f in files[:10]]
        return ImpactResult(nodes, [], f"mock impact: {len(files)} files, {len(nodes)} nodes")
