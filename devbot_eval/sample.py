"""评测样本 + suite 加载。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from devbot_eval.domain import Finding, GroundTruth, RiskLevel

SUITES_DIR = os.path.join(os.path.dirname(__file__), "suites")


@dataclass
class EvalSample:
    pr_id: str
    diff: str
    title: str = ""
    language: str = "java"
    suite: str = "regression"
    ground_truth: Optional[GroundTruth] = None


def _gt_from_json(pr_id: str, d: dict) -> GroundTruth:
    return GroundTruth(
        pr_id=pr_id,
        expected_risk_level=RiskLevel(d.get("expected_risk_level", "medium")),
        expected_findings=[
            Finding(file=f.get("file", ""), line=f.get("line"),
                    severity=f.get("severity", "warn"), message=f.get("message", ""))
            for f in d.get("expected_findings", [])
        ],
        notes=d.get("notes", ""),
    )


def load_suite(name: str) -> list[EvalSample]:
    path = os.path.join(SUITES_DIR, name + ".json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for s in data:
        gt = s.get("ground_truth")
        out.append(EvalSample(
            pr_id=s["pr_id"], diff=s.get("diff", ""), title=s.get("title", ""),
            language=s.get("language", "java"), suite=name,
            ground_truth=_gt_from_json(s["pr_id"], gt) if gt else None,
        ))
    return out


def load_all_suites() -> list[EvalSample]:
    out: list[EvalSample] = []
    for name in ("regression", "adversarial", "drift", "edge"):
        try:
            out.extend(load_suite(name))
        except FileNotFoundError:
            pass
    return out
