"""review → autofix 接线 —— 评审发现高危问题后,自动产出验证过的修复建议。

这是把 codegen(经 auto_fix 引擎)接进 review 的胶水:评审 aggregate 出 findings 后,
对**高于 severity 阈值**的 finding 逐个跑 auto_fix,按严重度降序**依次应用验证过的修复**
(后一个 finding 看到的是前一个修好的代码),最终汇总成可回写 PR 的 suggested change 列表。

接进 review_agent 只需一行:aggregate 出 findings 后调 ``auto_fix_review(...)``,
把返回的 suggestions 挂到 PR review 输出里回写。低于阈值 / 修不出可验证补丁的:只报问题、不贴补丁。
"""
from __future__ import annotations

from typing import Any

from .auto_fix import AutoFix, Finding, FixGenerator

SEVERITY_THRESHOLD = 7      # 只对高危 finding 自动修复
MAX_FIX = 5                 # 一次评审最多自动修几处(防回写刷屏)


def auto_fix_review(findings: list[Finding], code: str, check_code: str,
                    generator: FixGenerator, *,
                    severity_threshold: int = SEVERITY_THRESHOLD,
                    max_fix: int = MAX_FIX) -> dict[str, Any]:
    """评审 findings → 验证过的修复建议。按严重度降序依次应用。"""
    working = code
    suggestions: list[dict] = []
    skipped: list[dict] = []
    fixed_cnt = 0

    for f in sorted(findings, key=lambda x: x.severity, reverse=True):
        if f.severity < severity_threshold:
            skipped.append({"finding": f.message, "reason": "低于严重度阈值 %d" % severity_threshold})
            continue
        if fixed_cnt >= max_fix:
            skipped.append({"finding": f.message, "reason": "超过单次自动修复上限 %d" % max_fix})
            continue
        r = AutoFix(generator).run(f, working, check_code)
        if r.verified and r.suggestion:
            working = r.fixed_code           # 应用,后续 finding 看到修好的代码
            fixed_cnt += 1
            suggestions.append({"finding": f.message, "file": f.file, "line": f.line,
                                "suggestion": r.suggestion, "diff": r.diff,
                                "repair_rounds": r.repair_rounds})
        else:
            skipped.append({"finding": f.message, "reason": "未能产出可验证修复(只报问题)"})

    return {
        "original_code": code,
        "final_code": working,
        "auto_fixed": fixed_cnt,
        "suggestions": suggestions,            # 回写 PR 的 suggested change
        "skipped": skipped,
        "summary": "自动产出 %d 条验证过的修复建议,%d 条只报问题" % (fixed_cnt, len(skipped)),
    }
