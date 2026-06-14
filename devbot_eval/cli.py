"""devbot-eval 命令行入口。

用法示例:
    devbot-eval run --suite edge
    devbot-eval run --suite all --real --out result.json

run 子命令:
    --suite  regression / adversarial / drift / edge / all(默认 all)
    --real   用 FunctionEvaluator(包真 review_pr)+ LlmBackedJudge 真跑;
             不加则用 MockEvaluator + DeterministicJudge,确定性可复现进 CI。
    --out    把完整结果(每个 metric 的 MetricResult)写到该 json 路径。

默认 mock 跑(快 / 可复现 / 不烧 API),--real 出真质量数。所有重依赖惰性导入。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

SUITE_CHOICES = ["regression", "adversarial", "drift", "edge", "all"]


def _load_samples(suite: str) -> list:
    """按 --suite 取样本;'all' 合并所有 suite。"""
    from devbot_eval.sample import load_all_suites, load_suite
    if suite == "all":
        return load_all_suites()
    return load_suite(suite)


def _build_evaluator_and_judge(real: bool):
    """据 --real 选被测系统与裁判;惰性导入,避免无谓拉起真 LLM 依赖。"""
    if real:
        from devbot_eval.evaluator import FunctionEvaluator
        from devbot_eval.judges import LlmBackedJudge
        return FunctionEvaluator(), LlmBackedJudge()
    from devbot_eval.evaluator import MockEvaluator
    from devbot_eval.judges import DeterministicJudge
    return MockEvaluator(), DeterministicJudge()


def _format_table(results: dict[str, Any]) -> str:
    """把 {metric: result-dict} 渲染成对齐的两列表格字符串。"""
    rows = []
    for name, mr in results.items():
        value = mr.get("value") if isinstance(mr, dict) else getattr(mr, "value", None)
        try:
            cell = f"{float(value):.4f}"
        except (TypeError, ValueError):
            cell = str(value)
        rows.append((name, cell))
    if not rows:
        return "(no metrics)"
    name_w = max(len("metric"), max(len(n) for n, _ in rows))
    val_w = max(len("value"), max(len(v) for _, v in rows))
    sep = "-" * (name_w + val_w + 7)
    lines = [
        f"| {'metric'.ljust(name_w)} | {'value'.rjust(val_w)} |",
        sep,
    ]
    for n, v in rows:
        lines.append(f"| {n.ljust(name_w)} | {v.rjust(val_w)} |")
    return "\n".join(lines)


def _cmd_run(args: argparse.Namespace) -> int:
    """执行 run 子命令:加载样本 → 跑 evaluator+metrics → 打表 → (可选)写 json。"""
    from devbot_eval.registry import get_default_metrics
    from devbot_eval.runner import run as run_eval

    samples = _load_samples(args.suite)
    if not samples:
        print(f"[devbot-eval] no samples for suite={args.suite!r}", file=sys.stderr)
        return 2

    evaluator, judge = _build_evaluator_and_judge(args.real)
    metrics = get_default_metrics()
    mode = "real" if args.real else "mock"
    print(f"[devbot-eval] suite={args.suite} samples={len(samples)} mode={mode}")

    results = run_eval(samples, evaluator, metrics, judge=judge)

    print(_format_table(results))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(
                {"suite": args.suite, "mode": mode,
                 "n_samples": len(samples), "metrics": results},
                fh, ensure_ascii=False, indent=2, default=str,
            )
        print(f"[devbot-eval] wrote {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器(run 子命令 + 其旗标)。"""
    parser = argparse.ArgumentParser(
        prog="devbot-eval",
        description="devbot PR-review evaluation harness",
    )
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="run the eval harness on a suite")
    p_run.add_argument("--suite", choices=SUITE_CHOICES, default="all",
                       help="which suite to evaluate (default: all)")
    p_run.add_argument("--real", action="store_true",
                       help="use real review_pr + LLM judge instead of mock")
    p_run.add_argument("--out", default=None,
                       help="write full results as JSON to this path")
    p_run.set_defaults(func=_cmd_run)

    return parser


def run_cli(argv: Optional[list[str]] = None) -> int:
    """程序化入口:解析 argv、分派子命令,返回退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    return args.func(args)


def main() -> None:
    """控制台脚本入口(setuptools console_scripts 指向这里)。"""
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
