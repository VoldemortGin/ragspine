"""抽取评测模块测试（src/extraction_eval.py）。

对应 PRD「评测指标分通道报告」与回归门禁：
  - story #21：一条命令跑全量抽取回归，低于基线即失败。
  - story #34：答案中每个数字都来自确定性抽取且经过质量门（分通道准确率即质量门指标）。

红色阶段约定：run_eval / compare_to_baseline 当前 raise NotImplementedError，
本文件全部用例应「收集成功 + 全部 FAIL」，断言只针对外部行为（返回的 EvalReport /
BaselineComparison 的字段与数值），不触碰内部实现。

输入约定（与 fixtures_ground_truth.json 的 cells 结构对齐）：
  ground truth 是逐格真值（list[dict]，每项含 sheet/cell_ref/value/resolved_rgb/
  tags/merge_span 等），facts 是抽取器产出的同构逐格记录。run_eval 按
  sheet!cell_ref 对齐两侧，分三通道比对：
    cell_value         —— value 是否一致。
    color_mapping      —— 颜色翻译出的 tags 是否一致。
    header_attribution —— 合并表头归属（merge_span / 是否合并起点）是否一致。
"""

import copy
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.eval.extraction_eval import (
    BaselineComparison,
    ChannelMetric,
    EvalReport,
    compare_to_baseline,
    run_eval,
)

CELL_VALUE = "cell_value"
COLOR_MAPPING = "color_mapping"
HEADER_ATTRIBUTION = "header_attribution"
CHANNELS = (CELL_VALUE, COLOR_MAPPING, HEADER_ATTRIBUTION)


# --------------------------------------------------------------------------- #
# 辅助：从 ground truth 派生 facts，并注入已知偏差。                            #
# --------------------------------------------------------------------------- #


def _gt_cells(ground_truth) -> list[dict]:
    """从原始 ground truth 结构取逐格真值列表（深拷贝，互不污染）。"""
    return copy.deepcopy(ground_truth["cells"])


def _perfect_facts(ground_truth) -> list[dict]:
    """与 ground truth 完全一致的 facts（无任何偏差）。"""
    return _gt_cells(ground_truth)


def _locate(cells: list[dict], sheet: str, cell_ref: str) -> dict:
    """取某格记录（按 sheet!cell_ref 定位）。"""
    for c in cells:
        if c["sheet"] == sheet and c["cell_ref"] == cell_ref:
            return c
    raise AssertionError(f"fixture 缺少 {sheet}!{cell_ref}")


# --------------------------------------------------------------------------- #
# run_eval：返回结构与三通道存在性                                              #
# --------------------------------------------------------------------------- #


def test_run_eval_returns_eval_report(ground_truth):
    """story #21：run_eval 返回 EvalReport，包含全部三通道指标。"""
    report = run_eval(_perfect_facts(ground_truth), ground_truth)
    assert isinstance(report, EvalReport)
    for name in CHANNELS:
        assert name in report.channels
        assert isinstance(report.channels[name], ChannelMetric)
        assert report.channels[name].name == name


def test_run_eval_perfect_extraction_all_channels_full_accuracy(ground_truth):
    """story #34：无偏差抽取，三通道准确率全部为 1.0。"""
    report = run_eval(_perfect_facts(ground_truth), ground_truth)
    for name in CHANNELS:
        ch = report.channels[name]
        assert ch.accuracy == 1.0
        assert ch.correct == ch.total
        assert ch.mismatches == []


def test_run_eval_n_facts_counts_input(ground_truth):
    """story #21：报告记录参与评测的事实数。"""
    facts = _perfect_facts(ground_truth)
    report = run_eval(facts, ground_truth)
    assert report.n_facts == len(facts)


# --------------------------------------------------------------------------- #
# cell_value 通道：值准确率计算                                                 #
# --------------------------------------------------------------------------- #


def test_cell_value_channel_counts_one_wrong_value(ground_truth):
    """story #34：注入 1 个错值，cell_value 通道 correct 少 1 且准确率下降。"""
    facts = _perfect_facts(ground_truth)
    cell = _locate(facts, "HK_Performance", "B2")  # 真值 2100.0
    cell["value"] = 9999.0

    report = run_eval(facts, ground_truth)
    ch = report.channels[CELL_VALUE]
    assert ch.total >= 1
    assert ch.correct == ch.total - 1
    assert ch.accuracy == pytest.approx(ch.correct / ch.total)
    assert ch.accuracy < 1.0


def test_cell_value_mismatch_records_locator_and_values(ground_truth):
    """story #34：错值进入 mismatches，含定位与期望/实际值。"""
    facts = _perfect_facts(ground_truth)
    cell = _locate(facts, "HK_Performance", "B2")  # 真值 2100.0
    cell["value"] = 9999.0

    report = run_eval(facts, ground_truth)
    mismatches = report.channels[CELL_VALUE].mismatches
    assert len(mismatches) == 1
    item = mismatches[0]
    assert "B2" in str(item["locator"])
    assert item["expected"] == 2100.0
    assert item["actual"] == 9999.0


def test_cell_value_other_channels_unaffected_by_value_error(ground_truth):
    """story #21：纯值错误不应污染 color_mapping / header_attribution 通道。"""
    facts = _perfect_facts(ground_truth)
    _locate(facts, "HK_Performance", "B2")["value"] = 9999.0

    report = run_eval(facts, ground_truth)
    assert report.channels[COLOR_MAPPING].accuracy == 1.0
    assert report.channels[HEADER_ATTRIBUTION].accuracy == 1.0


# --------------------------------------------------------------------------- #
# color_mapping 通道：颜色翻译出的 tags 准确率                                  #
# --------------------------------------------------------------------------- #


def test_color_mapping_channel_counts_one_wrong_tag(ground_truth):
    """story #34：注入 1 个错 tag（new->mature），color_mapping 通道下降。"""
    facts = _perfect_facts(ground_truth)
    cell = _locate(facts, "HK_Performance", "B2")  # 真值 tags={"product_line":"new"}
    cell["tags"] = {"product_line": "mature"}

    report = run_eval(facts, ground_truth)
    ch = report.channels[COLOR_MAPPING]
    assert ch.total >= 1
    assert ch.correct == ch.total - 1
    assert ch.accuracy < 1.0


def test_color_mapping_mismatch_records_expected_actual_tags(ground_truth):
    """story #34：错 tag 进入 mismatches，含期望/实际 tags 与定位。"""
    facts = _perfect_facts(ground_truth)
    cell = _locate(facts, "HK_Performance", "B3")  # 真值 tags={"product_line":"mature"}
    cell["tags"] = {"product_line": "new"}

    report = run_eval(facts, ground_truth)
    mismatches = report.channels[COLOR_MAPPING].mismatches
    assert len(mismatches) == 1
    item = mismatches[0]
    assert "B3" in str(item["locator"])
    assert item["expected"] == {"product_line": "mature"}
    assert item["actual"] == {"product_line": "new"}


def test_color_mapping_value_correct_but_tag_wrong(ground_truth):
    """story #34：值对但属性错（PRD 核心痛点）——cell_value 满分、color_mapping 扣分。"""
    facts = _perfect_facts(ground_truth)
    cell = _locate(facts, "HK_Performance", "C2")  # value 2350.0, tag new
    cell["tags"] = {"product_line": "mature"}  # 只改 tag，不动 value

    report = run_eval(facts, ground_truth)
    assert report.channels[CELL_VALUE].accuracy == 1.0
    assert report.channels[COLOR_MAPPING].accuracy < 1.0


# --------------------------------------------------------------------------- #
# header_attribution 通道：合并表头归属准确率                                   #
# --------------------------------------------------------------------------- #


def test_header_attribution_channel_counts_one_wrong_span(ground_truth):
    """story #34：注入 1 个错的合并跨度，header_attribution 通道下降。"""
    facts = _perfect_facts(ground_truth)
    cell = _locate(facts, "MergedHeader", "A1")  # 真值 merge_span=[1,6]
    cell["merge_span"] = [1, 3]

    report = run_eval(facts, ground_truth)
    ch = report.channels[HEADER_ATTRIBUTION]
    assert ch.total >= 1
    assert ch.correct == ch.total - 1
    assert ch.accuracy < 1.0


def test_header_attribution_mismatch_records_expected_actual(ground_truth):
    """story #34：错的归属进入 mismatches，含期望/实际 span 与定位。"""
    facts = _perfect_facts(ground_truth)
    cell = _locate(facts, "MergedHeader", "D2")  # 真值 merge_span=[1,3]
    cell["merge_span"] = None

    report = run_eval(facts, ground_truth)
    mismatches = report.channels[HEADER_ATTRIBUTION].mismatches
    assert len(mismatches) == 1
    item = mismatches[0]
    assert "D2" in str(item["locator"])
    assert item["expected"] == [1, 3]
    assert item["actual"] is None


# --------------------------------------------------------------------------- #
# run_eval：边界与刁钻形态                                                      #
# --------------------------------------------------------------------------- #


def test_run_eval_empty_facts_empty_ground_truth():
    """story #21：空输入约定准确率为 1.0（total=0 时 accuracy=1.0），不报错。"""
    report = run_eval([], {"cells": []})
    for name in CHANNELS:
        ch = report.channels[name]
        assert ch.total == 0
        assert ch.accuracy == 1.0
    assert report.n_facts == 0


def test_run_eval_accepts_list_ground_truth(ground_truth):
    """story #21：ground_truth 既可为 dict（带 cells）也可直接为 list[dict]。"""
    cells = _gt_cells(ground_truth)
    report = run_eval(_perfect_facts(ground_truth), cells)
    assert isinstance(report, EvalReport)
    for name in CHANNELS:
        assert report.channels[name].accuracy == 1.0


def test_run_eval_multiple_errors_across_channels(ground_truth):
    """story #34：三通道各注入 1 处错误，互不串台、各自扣 1。"""
    facts = _perfect_facts(ground_truth)
    _locate(facts, "HK_Performance", "D2")["value"] = -1.0            # cell_value
    _locate(facts, "HK_Performance", "B4")["tags"] = {}              # color_mapping
    _locate(facts, "MergedHeader", "A2")["merge_span"] = [1, 6]      # header_attribution

    report = run_eval(facts, ground_truth)
    for name in CHANNELS:
        ch = report.channels[name]
        assert ch.correct == ch.total - 1
        assert len(ch.mismatches) == 1


def test_run_eval_half_wrong_cell_values_accuracy(ground_truth):
    """story #21：把 HK_Performance 全部数值格改错，cell_value 准确率严格 <1 且与计数自洽。"""
    facts = _perfect_facts(ground_truth)
    n_changed = 0
    for c in facts:
        if c["sheet"] == "HK_Performance" and isinstance(c["value"], (int, float)):
            c["value"] = float(c["value"]) + 1.0
            n_changed += 1
    assert n_changed > 0

    report = run_eval(facts, ground_truth)
    ch = report.channels[CELL_VALUE]
    assert ch.total - ch.correct == n_changed
    assert ch.accuracy == pytest.approx(ch.correct / ch.total)


# --------------------------------------------------------------------------- #
# compare_to_baseline：回归门禁判定                                             #
# --------------------------------------------------------------------------- #


def test_compare_to_baseline_pass_when_meets_baseline(ground_truth):
    """story #21：所有通道 >= 基线 -> passed=True 且无退化项。"""
    report = run_eval(_perfect_facts(ground_truth), ground_truth)
    baseline = {name: 0.9 for name in CHANNELS}

    result = compare_to_baseline(report, baseline)
    assert isinstance(result, BaselineComparison)
    assert result.passed is True
    assert result.regressions == []


def test_compare_to_baseline_pass_when_exactly_equal(ground_truth):
    """story #21：恰好等于基线（边界）应视为通过，不算退化。"""
    report = run_eval(_perfect_facts(ground_truth), ground_truth)
    baseline = {name: 1.0 for name in CHANNELS}  # 完美抽取 accuracy=1.0，恰好相等

    result = compare_to_baseline(report, baseline)
    assert result.passed is True
    assert result.regressions == []


def test_compare_to_baseline_fail_when_one_channel_below(ground_truth):
    """story #21：仅一个通道低于基线即整体失败（任一通道低于基线即 fail）。"""
    facts = _perfect_facts(ground_truth)
    _locate(facts, "HK_Performance", "B2")["value"] = 0.0  # 拉低 cell_value
    report = run_eval(facts, ground_truth)
    baseline = {name: 1.0 for name in CHANNELS}

    result = compare_to_baseline(report, baseline)
    assert result.passed is False


def test_compare_to_baseline_regression_detail_for_failing_channel(ground_truth):
    """story #21：退化明细指向出问题的通道，含 baseline / current / delta。"""
    facts = _perfect_facts(ground_truth)
    _locate(facts, "HK_Performance", "B2")["value"] = 0.0
    report = run_eval(facts, ground_truth)
    baseline = {name: 1.0 for name in CHANNELS}

    result = compare_to_baseline(report, baseline)
    channels = {r["channel"] for r in result.regressions}
    assert CELL_VALUE in channels
    assert COLOR_MAPPING not in channels
    assert HEADER_ATTRIBUTION not in channels

    reg = next(r for r in result.regressions if r["channel"] == CELL_VALUE)
    assert reg["baseline"] == 1.0
    current = report.channels[CELL_VALUE].accuracy
    assert reg["current"] == pytest.approx(current)
    assert reg["delta"] == pytest.approx(current - 1.0)
    assert reg["delta"] < 0


def test_compare_to_baseline_only_checks_channels_in_baseline(ground_truth):
    """story #21：基线只列出部分通道时，未列出的通道不参与门禁判定。"""
    facts = _perfect_facts(ground_truth)
    _locate(facts, "HK_Performance", "B2")["tags"] = {"product_line": "mature"}  # 只伤 color_mapping
    report = run_eval(facts, ground_truth)

    # 基线只关心 cell_value（仍 1.0），color_mapping 不在门禁内 -> 通过
    result = compare_to_baseline(report, {CELL_VALUE: 1.0})
    assert result.passed is True
    assert result.regressions == []


def test_compare_to_baseline_multiple_regressions(ground_truth):
    """story #21：多个通道同时低于基线，退化明细逐通道列出。"""
    facts = _perfect_facts(ground_truth)
    _locate(facts, "HK_Performance", "B2")["value"] = 0.0                # cell_value
    _locate(facts, "HK_Performance", "B3")["tags"] = {}                 # color_mapping
    report = run_eval(facts, ground_truth)
    baseline = {name: 1.0 for name in CHANNELS}

    result = compare_to_baseline(report, baseline)
    assert result.passed is False
    channels = {r["channel"] for r in result.regressions}
    assert {CELL_VALUE, COLOR_MAPPING} <= channels
    assert HEADER_ATTRIBUTION not in channels
