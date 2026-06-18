"""抽取评测：分通道指标 + 回归门禁（PRD「评测指标分通道报告」）。

run_eval 比对抽取出的 facts 与 ground truth，分通道报告准确率（单元格值 / 颜色映射 /
表头归属，本期 Excel 线三通道；OCR / 复核拦截通道二三期扩充）。compare_to_baseline 以
「任一通道指标低于基线」为失败条件，输出 pass/fail 与逐项差异，驱动「一条命令跑全量
回归、低于基线即失败」（user story 21）。

输入约定（与 fixtures_ground_truth.json 的 cells 结构对齐）：
    ground truth / facts 均为逐格记录 list[dict]，每项含 sheet / cell_ref / value /
    tags / merge_span 等。run_eval 按 sheet!cell_ref 对齐两侧，分三通道比对：
        cell_value          —— value 是否一致。
        color_mapping       —— 颜色翻译出的 tags 是否一致。
        header_attribution  —— 合并表头归属（merge_span）是否一致。
"""

from dataclasses import dataclass, field

CELL_VALUE = "cell_value"
COLOR_MAPPING = "color_mapping"
HEADER_ATTRIBUTION = "header_attribution"

# 通道名 -> 该通道比对的事实字段
_CHANNEL_FIELDS: dict[str, str] = {
    CELL_VALUE: "value",
    COLOR_MAPPING: "tags",
    HEADER_ATTRIBUTION: "merge_span",
}


@dataclass
class ChannelMetric:
    """单通道评测结果。

    字段：
        name:      通道名（'cell_value' / 'color_mapping' / 'header_attribution'）。
        total:     该通道应评的样本数。
        correct:   正确数。
        accuracy:  correct / total（total=0 时约定为 1.0）。
        mismatches: 差异明细列表（每项含 locator / expected / actual）。
    """

    name: str
    total: int = 0
    correct: int = 0
    accuracy: float = 0.0
    mismatches: list[dict[str, object]] = field(default_factory=list)


@dataclass
class EvalReport:
    """一次评测的分通道汇总。

    字段：
        channels:  通道名 -> ChannelMetric。
        n_facts:   参与评测的事实数。
    """

    channels: dict[str, ChannelMetric] = field(default_factory=dict)
    n_facts: int = 0


@dataclass
class BaselineComparison:
    """与基线比对结果（回归门禁）。

    字段：
        passed:    是否全部通道 >= 基线。
        regressions: 退化明细列表（每项含 channel / baseline / current / delta）。
    """

    passed: bool = False
    regressions: list[dict[str, object]] = field(default_factory=list)


def _gt_cells(
    ground_truth: dict[str, object] | list[dict[str, object]],
) -> list[dict[str, object]]:
    """取逐格真值列表：ground_truth 可为带 'cells' 的 dict，也可直接为 list[dict]。"""
    if isinstance(ground_truth, dict):
        cells = ground_truth.get("cells", [])
        return cells if isinstance(cells, list) else []
    return list(ground_truth)


def _locator(cell: dict[str, object]) -> str:
    """定位字符串：sheet!cell_ref。"""
    return f"{cell.get('sheet')}!{cell.get('cell_ref')}"


def run_eval(
    facts: list[dict[str, object]],
    ground_truth: dict[str, object] | list[dict[str, object]],
) -> EvalReport:
    """比对 facts 与 ground truth，分通道计算准确率，返回 EvalReport。

    通道：
        cell_value          —— 单元格数值准确率。
        color_mapping       —— 颜色翻译出的 tags 准确率。
        header_attribution  —— 合并表头归属（维度归属）准确率。

    按 sheet!cell_ref 对齐两侧；只评测两侧都存在的格。
    """
    gt_cells = _gt_cells(ground_truth)
    facts_by_key = {_locator(f): f for f in facts}

    channels: dict[str, ChannelMetric] = {
        name: ChannelMetric(name=name) for name in _CHANNEL_FIELDS
    }

    for gt in gt_cells:
        key = _locator(gt)
        fact = facts_by_key.get(key)
        if fact is None:
            continue
        for name, field_name in _CHANNEL_FIELDS.items():
            ch = channels[name]
            ch.total += 1
            expected = gt.get(field_name)
            actual = fact.get(field_name)
            if actual == expected:
                ch.correct += 1
            else:
                ch.mismatches.append(
                    {"locator": key, "expected": expected, "actual": actual}
                )

    for ch in channels.values():
        ch.accuracy = 1.0 if ch.total == 0 else ch.correct / ch.total

    return EvalReport(channels=channels, n_facts=len(facts))


def compare_to_baseline(metrics: EvalReport, baseline: dict[str, float]) -> BaselineComparison:
    """与基线比对：任一通道 accuracy 低于基线即 passed=False，并记录退化明细。

    只检查 baseline 中列出的通道；恰好等于基线视为通过。
    """
    regressions: list[dict[str, object]] = []
    for name, threshold in baseline.items():
        ch = metrics.channels.get(name)
        if ch is None:
            continue
        current = ch.accuracy
        if current < threshold:
            regressions.append(
                {
                    "channel": name,
                    "baseline": threshold,
                    "current": current,
                    "delta": current - threshold,
                }
            )

    return BaselineComparison(passed=not regressions, regressions=regressions)
