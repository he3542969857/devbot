"""遥测与漂移 —— PSI(Population Stability Index)衡量两分布的偏移 + 跨 run 指标记录。

PSI 是线上监控常用的分布漂移指标:把 baseline(expected)分桶得到占比,
用同样的桶切 actual,逐桶算 (a-e)·ln(a/e) 求和。
经验阈值:<0.1 稳定;0.1~0.25 轻微漂移;>0.25 显著漂移需告警。

Telemetry 记录每次 run 的各指标值,设定 baseline 后可对任一指标算 vs baseline 的 PSI。
纯标准库实现(不依赖 numpy),math.log 手算。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# PSI 经验判读阈值
PSI_STABLE = 0.10
PSI_MODERATE = 0.25


def _quantile_edges(values: list[float], buckets: int) -> list[float]:
    """按 baseline 的分位数切桶边界(等频),返回 buckets+1 个升序边界。

    等频分桶比等宽更稳:稀疏尾部不会产生空桶。重复值导致边界相等时去重,
    保证边界严格递增,空桶交由平滑项兜底。
    """
    xs = sorted(values)
    n = len(xs)
    edges = [xs[0]]
    for i in range(1, buckets):
        pos = i / buckets * (n - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        edges.append(xs[lo] + (xs[hi] - xs[lo]) * frac)
    edges.append(xs[-1])
    # 去重保持严格递增(把右端略微抬高)
    dedup = [edges[0]]
    for e in edges[1:]:
        if e <= dedup[-1]:
            e = dedup[-1] + 1e-9
        dedup.append(e)
    return dedup


def _bucket_fractions(values: list[float], edges: list[float]) -> list[float]:
    """用给定边界把 values 分桶,返回每桶占比(含两端开区间归入首尾桶)。"""
    nb = len(edges) - 1
    counts = [0] * nb
    for v in values:
        # 找 v 落在哪个桶:edges[k] <= v < edges[k+1],末桶闭区间
        placed = False
        for k in range(nb):
            lo, hi = edges[k], edges[k + 1]
            if (lo <= v < hi) or (k == nb - 1 and v <= hi) or (k == 0 and v < lo):
                counts[k] += 1
                placed = True
                break
        if not placed:  # v 超出右端
            counts[-1] += 1
    total = len(values) or 1
    return [c / total for c in counts]


def psi(expected: list[float], actual: list[float], buckets: int = 10) -> float:
    """Population Stability Index:expected(baseline)vs actual 的分布漂移。

    返回 >= 0 的标量;约定空输入返回 0.0。每桶占比加 1e-6 平滑避免 ln(0)/除零。
    """
    if not expected or not actual:
        return 0.0
    buckets = max(1, buckets)
    # baseline 全相同值时无法分桶,退化为单桶 —— 分布无差异返回 0
    if len(set(expected)) == 1:
        same = sum(1 for v in actual if v == expected[0]) / len(actual)
        # 全落同点则无漂移,否则用伯努利两桶近似
        if same >= 1.0:
            return 0.0
        e = [1.0, 1e-6]
        a = [same, max(1e-6, 1.0 - same)]
        return _psi_from_fractions(e, a)
    edges = _quantile_edges(expected, buckets)
    e_frac = _bucket_fractions(expected, edges)
    a_frac = _bucket_fractions(actual, edges)
    return _psi_from_fractions(e_frac, a_frac)


def _psi_from_fractions(e_frac: list[float], a_frac: list[float]) -> float:
    eps = 1e-6
    total = 0.0
    for e, a in zip(e_frac, a_frac):
        e = max(e, eps)
        a = max(a, eps)
        total += (a - e) * math.log(a / e)
    return round(total, 6)


def drift_label(score: float) -> str:
    """把 PSI 值翻译成人读的漂移等级。"""
    if score < PSI_STABLE:
        return "stable"
    if score < PSI_MODERATE:
        return "moderate"
    return "significant"


@dataclass
class RunRecord:
    """单次评测 run 的指标快照:metric 名 -> 标量值。"""
    run_id: str
    metrics: dict[str, float] = field(default_factory=dict)
    # 可选:某指标的逐样本分布(给 PSI 用),metric 名 -> [值...]
    distributions: dict[str, list[float]] = field(default_factory=dict)


class Telemetry:
    """跨 run 遥测:记录每次 run 的指标(及可选分布),并对某指标算 vs baseline 的漂移。

    典型用法:
        t = Telemetry()
        t.record("2026-06-01", {"risk_accuracy": 0.69}, {"risk_score": [...]})
        t.set_baseline("2026-06-01")
        t.record("2026-06-05", {"risk_accuracy": 0.55}, {"risk_score": [...]})
        t.drift("risk_score")          # -> PSI 标量
        t.metric_delta("risk_accuracy")  # -> 当前相对 baseline 的差值
    """

    def __init__(self) -> None:
        self._runs: list[RunRecord] = []
        self._baseline_id: Optional[str] = None

    def record(self, run_id: str, metrics: dict[str, float],
               distributions: Optional[dict[str, list[float]]] = None) -> RunRecord:
        rec = RunRecord(run_id=run_id, metrics=dict(metrics),
                        distributions={k: list(v) for k, v in (distributions or {}).items()})
        self._runs.append(rec)
        if self._baseline_id is None:
            self._baseline_id = run_id
        return rec

    def set_baseline(self, run_id: str) -> None:
        if not any(r.run_id == run_id for r in self._runs):
            raise KeyError(f"unknown run_id: {run_id}")
        self._baseline_id = run_id

    def _get(self, run_id: str) -> RunRecord:
        for r in reversed(self._runs):  # 同 id 取最近一次
            if r.run_id == run_id:
                return r
        raise KeyError(f"unknown run_id: {run_id}")

    @property
    def baseline(self) -> Optional[RunRecord]:
        if self._baseline_id is None:
            return None
        return self._get(self._baseline_id)

    @property
    def latest(self) -> Optional[RunRecord]:
        return self._runs[-1] if self._runs else None

    def drift(self, metric: str, run_id: Optional[str] = None, buckets: int = 10) -> float:
        """某 metric 的分布在 (run_id 或最新) 相对 baseline 的 PSI。无分布数据返回 0.0。"""
        base = self.baseline
        cur = self._get(run_id) if run_id else self.latest
        if base is None or cur is None:
            return 0.0
        exp = base.distributions.get(metric)
        act = cur.distributions.get(metric)
        if not exp or not act:
            return 0.0
        return psi(exp, act, buckets=buckets)

    def metric_delta(self, metric: str, run_id: Optional[str] = None) -> float:
        """某标量 metric 当前(或指定 run)相对 baseline 的差值(current - baseline)。"""
        base = self.baseline
        cur = self._get(run_id) if run_id else self.latest
        if base is None or cur is None:
            return 0.0
        return round(cur.metrics.get(metric, 0.0) - base.metrics.get(metric, 0.0), 6)

    def report(self, run_id: Optional[str] = None, buckets: int = 10) -> dict:
        """汇总当前 run vs baseline:每个分布指标的 PSI + 漂移等级,每个标量指标的 delta。"""
        base = self.baseline
        cur = self._get(run_id) if run_id else self.latest
        if base is None or cur is None:
            return {"baseline": None, "run": None, "drift": {}, "delta": {}}
        drift = {}
        for m in cur.distributions:
            if m in base.distributions:
                p = self.drift(m, run_id=cur.run_id, buckets=buckets)
                drift[m] = {"psi": p, "label": drift_label(p)}
        delta = {m: self.metric_delta(m, run_id=cur.run_id)
                 for m in cur.metrics if m in base.metrics}
        return {"baseline": base.run_id, "run": cur.run_id, "drift": drift, "delta": delta}
