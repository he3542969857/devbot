"""Meta-eval —— 校验"裁判(judge)本身是否可信"。

judge 给的分若要可信,必须与人工/真值标签相关:
- Pearson r:judge 连续打分 vs 参考连续值(如人工 0-5)的线性相关。
- Cohen's κ:judge 离散判定 vs 参考离散标签的一致性(复用 domain.cohen_kappa)。

meta_eval(judge_scores, reference_labels) -> {"pearson": r, "kappa": κ}。
两者输入等长;κ 把连续分四舍五入成整数类(若已是离散标签则直接比)。
纯标准库,Pearson 手算。
"""
from __future__ import annotations

import math
from typing import Any, Sequence

from devbot_eval.domain import cohen_kappa


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson 相关系数 r,手算。

    r = Σ(x-x̄)(y-ȳ) / sqrt(Σ(x-x̄)² · Σ(y-ȳ)²)
    退化情形(空 / 不等长 / 任一方零方差)返回 0.0。
    """
    n = len(xs)
    if n == 0 or n != len(ys):
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sxx = syy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mx
        dy = y - my
        cov += dx * dy
        sxx += dx * dx
        syy += dy * dy
    denom = math.sqrt(sxx * syy)
    if denom == 0.0:
        return 0.0
    r = cov / denom
    # 浮点误差夹回 [-1, 1]
    return round(max(-1.0, min(1.0, r)), 6)


def _to_discrete(values: Sequence[Any]) -> list[Any]:
    """把可能为连续分的序列离散化成可比标签:数值四舍五入成 int,其余原样。"""
    out: list[Any] = []
    for v in values:
        if isinstance(v, bool):
            out.append(v)
        elif isinstance(v, (int, float)):
            out.append(int(round(v)))
        else:
            out.append(v)
    return out


def meta_eval(judge_scores: Sequence[float], reference_labels: Sequence[Any]) -> dict:
    """校验 judge:返回 {pearson, kappa, n}。

    - pearson:judge 连续分 vs 参考(参考须可转 float,否则该项为 0.0)。
    - kappa:judge 与参考都离散化后的 Cohen's κ。
    """
    n = len(judge_scores)
    if n == 0 or n != len(reference_labels):
        return {"pearson": 0.0, "kappa": 0.0, "n": 0}

    # Pearson 需要双方都是数值
    try:
        rx = [float(v) for v in judge_scores]
        ry = [float(v) for v in reference_labels]
        r = pearson(rx, ry)
    except (TypeError, ValueError):
        r = 0.0

    k = cohen_kappa(_to_discrete(judge_scores), _to_discrete(reference_labels))
    return {"pearson": r, "kappa": round(k, 6), "n": n}


def meta_eval_dimensions(judge_outputs: Sequence[dict],
                         reference: Sequence[dict],
                         dimensions: Sequence[str] = ("relevance", "actionability", "novelty"),
                         ) -> dict:
    """逐维度校验 judge:对 relevance/actionability/novelty 各算 pearson+kappa。

    judge_outputs / reference 为等长的 dict 列表(每元素含各维度分)。
    返回 {dim: {pearson, kappa, n}, ...}。
    """
    out: dict[str, dict] = {}
    for dim in dimensions:
        js = [o.get(dim, 0.0) for o in judge_outputs]
        rs = [r.get(dim, 0.0) for r in reference]
        out[dim] = meta_eval(js, rs)
    return out
