"""Q&A 评测闭环 CLI：四命门指标分别报告 + 基线门禁。

用法（从项目根目录）：
    .venv/bin/python scripts/run_qa_eval.py --mode tool
    .venv/bin/python scripts/run_qa_eval.py --mode agent --report out/qa_report.json
    .venv/bin/python scripts/run_qa_eval.py --mode tool --update-baseline

门禁语义（仿 scripts 既有回归模式）：基线按 mode 分键存于 data/golden/qa_baseline.json；
该 mode 无基线时首跑自动生成并放行；有基线时任一命门指标退化（或编造数字增加）
即退出码 1；--update-baseline 用当前结果显式重写基线。
"""

import argparse
import json
from pathlib import Path

import rootutils

from ragspine.eval.qa_eval import (
    EVAL_MODES,
    FABRICATION,
    compare_to_baseline,
    make_baseline_entry,
    run_qa_eval,
)

ROOT_DIR = rootutils.find_root(Path(__file__), indicator=".project-root")

DEFAULT_GOLDEN = ROOT_DIR / "data" / "golden" / "qa_golden_set.jsonl"
DEFAULT_BASELINE = ROOT_DIR / "data" / "golden" / "qa_baseline.json"


def _print_report(report) -> None:
    print(f"=== QA 评测报告（mode={report.mode}，{report.n_cases} cases）===")
    for name, metric in report.metrics.items():
        print(f"  {name}: {metric.passed}/{metric.total} "
              f"(pass_rate={metric.pass_rate:.4f})")
        for failure in metric.failures:
            print(f"    FAIL {failure['id']}: 期望={failure['expected']} "
                  f"实际={failure['actual']}")
    print(f"  {FABRICATION}: {report.fabrication_count} 例编造"
          f"（拒答类样本 {report.fabrication.total} 条，目标 0）")
    for failure in report.fabrication.failures:
        print(f"    FAIL {failure['id']}: {failure['actual']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QA 评测闭环：四命门指标 + 基线门禁")
    parser.add_argument("--mode", choices=EVAL_MODES, default="tool",
                        help="tool=绕过 LLM 的确定性直测；agent=answer_question+MockProvider")
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN),
                        help="golden set JSONL 路径")
    parser.add_argument("--report", default=None, help="评测报告 JSON 输出路径")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE),
                        help="基线 JSON 路径（按 mode 分键）")
    parser.add_argument("--update-baseline", action="store_true",
                        help="用当前结果重写该 mode 的基线")
    args = parser.parse_args(argv)

    report = run_qa_eval(args.golden, mode=args.mode)
    _print_report(report)

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"报告已写入：{report_path}")

    baseline_path = Path(args.baseline)
    baselines: dict = {}
    if baseline_path.exists():
        baselines = json.loads(baseline_path.read_text(encoding="utf-8"))

    def _save_baseline() -> None:
        baselines[args.mode] = make_baseline_entry(report)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baselines, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.update_baseline or args.mode not in baselines:
        _save_baseline()
        action = "重写" if args.update_baseline else "首跑生成"
        print(f"基线已{action}（mode={args.mode}）：{baseline_path}")
        return 0

    comparison = compare_to_baseline(report, baselines[args.mode])
    if comparison.passed:
        print("门禁 PASS：全部命门指标不低于基线。")
        return 0
    print("门禁 FAIL：检测到指标退化——")
    for regression in comparison.regressions:
        print(f"  {regression['metric']}: 基线={regression['baseline']} "
              f"当前={regression['current']}（delta={regression['delta']:+}）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
