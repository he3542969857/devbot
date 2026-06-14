"""PromptRegistry —— 版本化 prompt 模板 + active/canary 确定性灰度。

从 META.yaml 加载每个 prompt 的 {active, canary, canary_pct, versions}。
get(name, key) 用 hash(name+":"+key) 把 key 稳定映射到 [0,1),
落入 canary_pct 区间则返回 canary 版本,否则 active 版本 —— 同 key 永远同版本,
便于 A/B 复现与按 pr_id 归因。canary 为空或 canary_pct=0 时恒返回 active。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

_DEFAULT_META = os.path.join(os.path.dirname(__file__), "META.yaml")


@dataclass
class PromptSpec:
    """单个 prompt 名的版本声明。"""
    name: str
    active: str
    canary: Optional[str] = None
    canary_pct: float = 0.0
    versions: dict[str, str] = field(default_factory=dict)

    def template_for(self, version: str) -> str:
        if version not in self.versions:
            raise KeyError(f"prompt '{self.name}' has no version '{version}'")
        return self.versions[version]


def _bucket(name: str, key: str) -> float:
    """把 (name, key) 稳定映射到 [0, 1)。用 md5 取前 8 hex 位归一,跨进程一致。"""
    h = hashlib.md5(f"{name}:{key}".encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


class PromptRegistry:
    """版本化 prompt 注册表。

    用法:
        reg = PromptRegistry()                      # 默认读包内 META.yaml
        tmpl = reg.get("critic_security", key=pr_id)  # 确定性 active/canary
        text = reg.render("critic_security", key=pr_id, title=..., diff=...)
    """

    def __init__(self, meta_path: Optional[str] = None, specs: Optional[dict] = None):
        if specs is not None:
            self._specs = specs
        else:
            self._specs = self._load(meta_path or _DEFAULT_META)

    @staticmethod
    def _load(path: str) -> dict[str, PromptSpec]:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        specs: dict[str, PromptSpec] = {}
        for name, cfg in raw.items():
            cfg = cfg or {}
            versions = cfg.get("versions", {}) or {}
            active = cfg.get("active")
            if active is None and versions:
                active = next(iter(versions))  # 缺 active 取首版兜底
            specs[name] = PromptSpec(
                name=name,
                active=active,
                canary=cfg.get("canary"),
                canary_pct=float(cfg.get("canary_pct", 0.0) or 0.0),
                versions=versions,
            )
        return specs

    def names(self) -> list[str]:
        return list(self._specs.keys())

    def spec(self, name: str) -> PromptSpec:
        if name not in self._specs:
            raise KeyError(f"unknown prompt: {name}")
        return self._specs[name]

    def choose_version(self, name: str, key: str = "") -> str:
        """确定性选 active 或 canary 版本号。"""
        spec = self.spec(name)
        if spec.canary and spec.canary_pct > 0.0:
            if _bucket(name, str(key)) < spec.canary_pct:
                return spec.canary
        return spec.active

    def get(self, name: str, key: str = "") -> str:
        """返回该 key 命中的版本模板字符串(active 或 canary)。"""
        version = self.choose_version(name, key)
        return self.spec(name).template_for(version)

    def render(self, name: str, key: str = "", **vars) -> str:
        """选版本 + .format(**vars) 渲染。缺占位符时原样返回未渲染模板(不抛)。"""
        tmpl = self.get(name, key)
        try:
            return tmpl.format(**vars)
        except (KeyError, IndexError):
            return tmpl
