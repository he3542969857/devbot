# -*- coding: utf-8 -*-
"""devbot 跨 Critic 去重 / severity / 置信度校准 纯逻辑单测。
被测 API(待实现于 devbot/findings.py):
    line_close(a,b,w) / severity_rank(sev) / dedup_entries(entries,window) / calibrated_confidence(risk,a,b)
entries: list[(critic_idx, finding_dict)];finding 含 file/line/severity。
"""
import pytest
from devbot.findings import line_close, severity_rank, dedup_entries, calibrated_confidence


def test_line_close():
    assert line_close(10, 12, 3) is True        # 窗口内
    assert line_close(10, 20, 3) is False       # 窗口外
    assert line_close(None, None, 3) is True     # 两个无行号 -> 视为同簇
    assert line_close(5, None, 3) is False       # 一有一无 -> 不聚
    assert line_close("x", 5, 3) is False        # 非法 -> False


def test_severity_rank():
    assert severity_rank("error") > severity_rank("warn") > severity_rank("info")
    assert severity_rank("warning") == severity_rank("warn")
    assert severity_rank(None) == severity_rank("info")  # 默认 info
    assert severity_rank("garbage") == 1


def _f(file, line, sev):
    return {"file": file, "line": line, "severity": sev, "message": "x"}


def test_dedup_same_location_keeps_highest_severity():
    # 同文件、行 10/12(窗口3内)、两 Critic 各报一条 -> 聚成 1,留 error
    entries = [(0, _f("a.py", 10, "warn")), (1, _f("a.py", 12, "error"))]
    kept, dropped = dedup_entries(entries, 3)
    assert dropped == 1 and len(kept) == 1
    assert kept[0][1]["severity"] == "error"


def test_dedup_far_lines_not_merged():
    entries = [(0, _f("a.py", 10, "warn")), (1, _f("a.py", 20, "warn"))]
    kept, dropped = dedup_entries(entries, 3)
    assert dropped == 0 and len(kept) == 2


def test_dedup_different_files_not_merged():
    entries = [(0, _f("a.py", 10, "warn")), (1, _f("b.py", 10, "warn"))]
    kept, dropped = dedup_entries(entries, 3)
    assert dropped == 0 and len(kept) == 2


def test_dedup_none_lines_same_file_merge():
    entries = [(0, _f("a.py", None, "info")), (1, _f("a.py", None, "warn"))]
    kept, dropped = dedup_entries(entries, 3)
    assert dropped == 1 and kept[0][1]["severity"] == "warn"


def test_dedup_empty():
    assert dedup_entries([], 3) == ([], 0)


def test_dedup_does_not_mutate_input():
    e0 = _f("a.py", 10, "warn")
    entries = [(0, e0)]
    dedup_entries(entries, 3)
    assert entries == [(0, e0)]  # 入参不被改


def test_calibrated_confidence_monotonic_and_bounded():
    lo = calibrated_confidence(0, 3.82, -1.42)
    hi = calibrated_confidence(100, 3.82, -1.42)
    assert 0.0 <= lo < hi <= 1.0
    assert hi == pytest.approx(1 / (1 + pow(2.718281828, -(3.82 * 1.0 - 1.42))), abs=1e-3)
