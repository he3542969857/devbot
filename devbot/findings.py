# -*- coding: utf-8 -*-
"""评审 finding 的纯逻辑:severity 排序、行邻近、跨 Critic 去重、置信度 Platt 校准。

抽成纯函数便于单测与复用(review_agent 的去重/校准都走这里)。
"""
from __future__ import annotations

import math

SEVERITY_ORDER = {"error": 3, "warn": 2, "warning": 2, "info": 1}


def severity_rank(sev) -> int:
    """severity 字符串 -> 序(error>warn>info);未知/None 当 info。"""
    return SEVERITY_ORDER.get((sev or "info").lower(), 1)


def line_close(a, b, w: int) -> bool:
    """两个行号是否在窗口 w 内邻近;两者都无行号视为同簇,一有一无不聚。"""
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(int(a) - int(b)) <= w
    except Exception:
        return False


def dedup_entries(entries, window: int):
    """跨 Critic 去重(纯函数,不改入参)。
    entries: list[(critic_idx, finding_dict)];按 file + line±window 聚簇,每簇留最高 severity 一条。
    返回 (kept: list[(critic_idx, finding_dict)], dropped: int)。
    """
    entries = list(entries)
    used = [False] * len(entries)
    kept = []
    dropped = 0
    for i, (ci, f) in enumerate(entries):
        if used[i]:
            continue
        cluster = [(i, ci, f)]
        for j in range(i + 1, len(entries)):
            if used[j]:
                continue
            cj, fj = entries[j]
            if fj.get("file") == f.get("file") and line_close(f.get("line"), fj.get("line"), window):
                cluster.append((j, cj, fj))
        best = max(cluster, key=lambda t: severity_rank(t[2].get("severity")))
        for idx, _, _ in cluster:
            used[idx] = True
        kept.append((best[1], best[2]))
        dropped += len(cluster) - 1
    return kept, dropped


def sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def calibrated_confidence(risk_score: float, a: float, b: float) -> float:
    """Platt 后置校准:sigmoid(a · risk/100 + b)。"""
    return round(sigmoid(a * (risk_score / 100.0) + b), 4)
