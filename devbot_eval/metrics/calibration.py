"""校准指标(calibration)—— ECE(Expected Calibration Error)。

ECE 量化"模型自报置信度"与"实际正确率"的差距:置信度说 0.9 就该 90% 对。
越低越好。devbot 的对抗集上 ECE 曾高(过自信),Platt 后置校准把留出 ECE 0.37→0.24。

算法(与 review_agent 的置信度口径对齐):
- 逐 prediction、逐 critic 取一个样本点:
    hit        = (critic.risk_score >= 50) == (expected_risk_level ∈ {MED,HIGH,CRITICAL})
    confidence = critic.confidence
- 把 confidence 落进 10 个等宽桶 [0,0.1),[0.1,0.2),...,[0.9,1.0];1.0 归到最后一桶。
- ECE = Σ_bucket (bucket_weight · |avg_conf − avg_acc|),bucket_weight = 桶内样本数 / 总样本数。

仅用 stdlib;不依赖 numpy。
"""
from __future__ import annotations

from devbot_eval.domain import PRReviewOutput, RiskLevel
from devbot_eval.metrics.base import MetricResult
from devbot_eval.sample import EvalSample

_NUM_BINS = 10
_RISKY_LEVELS = {RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL}
_RISK_SCORE_THRESHOLD = 50


def _bin_index(conf: float, num_bins: int = _NUM_BINS) -> int:
    """把 [0,1] 的置信度映射到桶下标 [0, num_bins-1];越界做截断,1.0 归最后一桶。"""
    if conf <= 0.0:
        return 0
    if conf >= 1.0:
        return num_bins - 1
    idx = int(conf * num_bins)
    if idx >= num_bins:  # 浮点边界保护
        idx = num_bins - 1
    return idx


def _ece_from_points(points: list[tuple[float, int]], num_bins: int = _NUM_BINS):
    """从 (confidence, hit) 样本点算 ECE。返回 (ece, bins_detail)。"""
    if not points:
        return 0.0, []

    total = len(points)
    # 每桶累计:[sum_conf, sum_hit, count]
    bins = [[0.0, 0, 0] for _ in range(num_bins)]
    for conf, hit in points:
        b = _bin_index(conf, num_bins)
        bins[b][0] += conf
        bins[b][1] += hit
        bins[b][2] += 1

    ece = 0.0
    detail = []
    for b, (sum_conf, sum_hit, count) in enumerate(bins):
        if count == 0:
            detail.append({
                "bin": b,
                "range": [round(b / num_bins, 2), round((b + 1) / num_bins, 2)],
                "count": 0, "avg_conf": None, "avg_acc": None,
            })
            continue
        avg_conf = sum_conf / count
        avg_acc = sum_hit / count
        weight = count / total
        ece += weight * abs(avg_conf - avg_acc)
        detail.append({
            "bin": b,
            "range": [round(b / num_bins, 2), round((b + 1) / num_bins, 2)],
            "count": count,
            "avg_conf": round(avg_conf, 4),
            "avg_acc": round(avg_acc, 4),
        })
    return ece, detail


class EceMetric:
    """Expected Calibration Error(越低越好)。

    value = 全局 ECE(所有 critic 样本点合在一起分桶);
    breakdown 给 per-critic ECE(按 critic 名分别分桶),extras 给桶内细节。
    """

    name = "ece"

    def evaluate(self, samples: list[EvalSample],
                 predictions: list[PRReviewOutput]) -> MetricResult:
        all_points: list[tuple[float, int]] = []
        per_critic_points: dict[str, list[tuple[float, int]]] = {}

        for sample, pred in zip(samples, predictions):
            gt = sample.ground_truth
            if gt is None:
                continue
            truth_risky = gt.expected_risk_level in _RISKY_LEVELS
            for critic in pred.critics:
                pred_risky = critic.risk_score >= _RISK_SCORE_THRESHOLD
                hit = 1 if (pred_risky == truth_risky) else 0
                conf = float(critic.confidence)
                point = (conf, hit)
                all_points.append(point)
                per_critic_points.setdefault(critic.critic, []).append(point)

        global_ece, global_detail = _ece_from_points(all_points)

        per_critic_ece = {}
        per_critic_detail = {}
        for critic_name, points in sorted(per_critic_points.items()):
            ece_c, detail_c = _ece_from_points(points)
            per_critic_ece[critic_name] = round(ece_c, 4)
            per_critic_detail[critic_name] = detail_c

        return MetricResult(
            name=self.name,
            value=round(global_ece, 4),
            breakdown={
                "ece": round(global_ece, 4),
                "per_critic": per_critic_ece,
                "n_points": len(all_points),
            },
            extras={
                "bins": global_detail,
                "per_critic_bins": per_critic_detail,
            },
        )
