"""双通道交叉校验 verify() 的红色测试（TDD，二期 PDF 线）。

只验证外部行为：维度键对齐的 agreed / conflicts / only_in_a / only_in_b 划分；
tolerance=0 与 tolerance>0 两种容差行为；给 queue 时 conflict / single_only 按
reason 与优先级入队、payload 含双方值与 locator；不给 queue 时零入队但统计照常；
幂等（同输入两次 verify 结果一致）。对应 PRD user stories 15 / 22。

契约阶段 verify() 体 raise NotImplementedError —— 因此每个用例都断言"实现后才
成立的外部行为"，红色阶段应全部 FAIL（不得 import / collection error，不得意外 PASS）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.verification.dual_channel_verifier import (
    PRIORITY_CONFLICT,
    PRIORITY_SINGLE_ONLY,
    REASON_CONFLICT,
    REASON_SINGLE_ONLY,
    ChannelFact,
    VerificationResult,
    verify,
)
from ragspine.ingestion.review.review_queue import STATUS_PENDING, ReviewQueue


# --------------------------------------------------------------------------- #
# 辅助：从 ground truth 测试向量构造 ChannelFact 列表 / 真实复核队列
# --------------------------------------------------------------------------- #
def _facts_from(records: list[dict]) -> list[ChannelFact]:
    """把 ground truth 中的扁平 dict 列表转成 ChannelFact 列表（字段一一对应）。"""
    return [ChannelFact(**rec) for rec in records]


def _mk(
    metric_code: str,
    value: float,
    channel_name: str,
    *,
    entity: str = "ACME_HK",
    period_type: str = "FY",
    period: str = "FY2024",
    channel: str = "TOTAL",
    source_locator: str | None = None,
) -> ChannelFact:
    """便捷构造一条 ChannelFact（默认共用同一维度，只变 metric / value / 来源）。"""
    return ChannelFact(
        metric_code=metric_code,
        entity=entity,
        period_type=period_type,
        period=period,
        channel=channel,
        value=value,
        source_locator=source_locator or f"loc_{metric_code}_{channel_name}",
        channel_name=channel_name,
    )


@pytest.fixture
def queue(tmp_db_path):
    """一个建好表的真实复核队列（验证入队后能 list_pending / get 取回）。"""
    q = ReviewQueue(tmp_db_path)
    q.init_schema()
    yield q
    q.close()


@pytest.fixture
def dual_vector(pdf_ground_truth) -> dict:
    """ground truth 中的 dual_channel 测试向量（facts_a / facts_b / expect）。"""
    return pdf_ground_truth["dual_channel"]


def _key_set(facts: list[ChannelFact]) -> set[tuple]:
    """把 ChannelFact 列表折叠成 dim_key 集合，便于无序比较。"""
    return {f.dim_key() for f in facts}


# --------------------------------------------------------------------------- #
# A. ground truth 测试向量：四象限划分正确（story #15）
# --------------------------------------------------------------------------- #
def test_ground_truth_vector_quadrant_split(dual_vector):
    """story #15 用 ground truth 向量校验：REVENUE->agreed、NEWSALES->conflict、

    PROFIT->only_in_a、ROE->only_in_b（维度键集合与真值逐一吻合）。
    """
    facts_a = _facts_from(dual_vector["facts_a"])
    facts_b = _facts_from(dual_vector["facts_b"])
    result = verify(facts_a, facts_b, tolerance=dual_vector["tolerance"])

    expect = dual_vector["expect"]
    assert _key_set(result.agreed) == {tuple(k) for k in expect["agreed_keys"]}
    assert {tuple(c["dim_key"]) for c in result.conflicts} == {
        tuple(k) for k in expect["conflict_keys"]
    }
    assert _key_set(result.only_in_a) == {tuple(k) for k in expect["only_in_a_keys"]}
    assert _key_set(result.only_in_b) == {tuple(k) for k in expect["only_in_b_keys"]}


def test_ground_truth_vector_counts(dual_vector):
    """story #15 用 ground truth 向量校验统计：n_auto_passed=1、入队 3 条。"""
    facts_a = _facts_from(dual_vector["facts_a"])
    facts_b = _facts_from(dual_vector["facts_b"])
    result = verify(facts_a, facts_b, tolerance=dual_vector["tolerance"])

    expect = dual_vector["expect"]
    assert result.n_auto_passed == expect["n_auto_passed"]
    assert len(result.agreed) == expect["n_auto_passed"]
    # 不给 queue 时只分类不入队。
    assert result.n_enqueued == 0


def test_ground_truth_vector_enqueues_three(dual_vector, queue):
    """story #15/#22 给 queue 时：ground truth 向量应入队 3 条（1 冲突 + 2 单通道独有）。"""
    facts_a = _facts_from(dual_vector["facts_a"])
    facts_b = _facts_from(dual_vector["facts_b"])
    result = verify(facts_a, facts_b, queue=queue, tolerance=dual_vector["tolerance"])

    assert result.n_enqueued == dual_vector["expect"]["n_enqueued"]
    assert len(queue.list_pending()) == dual_vector["expect"]["n_enqueued"]


# --------------------------------------------------------------------------- #
# B. 返回类型与默认 / 空输入（story #15 边界）
# --------------------------------------------------------------------------- #
def test_returns_verification_result_type():
    """story #15 verify 返回 VerificationResult 实例。"""
    result = verify([_mk("REVENUE", 1.0, "a")], [_mk("REVENUE", 1.0, "b")])
    assert isinstance(result, VerificationResult)


def test_empty_inputs_all_zero():
    """story #15 两侧皆空 -> 四象限均空、计数为 0（不抛异常）。"""
    result = verify([], [])
    assert result.agreed == []
    assert result.conflicts == []
    assert result.only_in_a == []
    assert result.only_in_b == []
    assert result.n_auto_passed == 0
    assert result.n_enqueued == 0


def test_empty_b_all_only_in_a(queue):
    """story #15 B 侧为空 -> A 的每条都落 only_in_a（无 agreed / conflict）。"""
    facts_a = [_mk("REVENUE", 1.0, "docling"), _mk("NEWSALES", 2.0, "docling")]
    result = verify(facts_a, [], queue=queue)
    assert _key_set(result.only_in_a) == _key_set(facts_a)
    assert result.only_in_b == []
    assert result.agreed == []
    assert result.conflicts == []
    assert result.n_enqueued == 2


def test_empty_a_all_only_in_b(queue):
    """story #15 A 侧为空 -> B 的每条都落 only_in_b。"""
    facts_b = [_mk("REVENUE", 1.0, "text"), _mk("NEWSALES", 2.0, "text")]
    result = verify([], facts_b, queue=queue)
    assert _key_set(result.only_in_b) == _key_set(facts_b)
    assert result.only_in_a == []
    assert result.n_enqueued == 2


# --------------------------------------------------------------------------- #
# C. agreed：维度键相同且值在容差内（story #15）
# --------------------------------------------------------------------------- #
def test_exact_match_auto_passes():
    """story #15 同维度键、值完全相等 -> agreed 自动放行，不入冲突。"""
    result = verify([_mk("REVENUE", 2680.0, "a")], [_mk("REVENUE", 2680.0, "b")])
    assert _key_set(result.agreed) == {_mk("REVENUE", 2680.0, "a").dim_key()}
    assert result.conflicts == []
    assert result.n_auto_passed == 1


def test_agreed_does_not_enqueue(queue):
    """story #15/#22 一致项自动放行 -> 不入队（队列保持为空）。"""
    result = verify([_mk("REVENUE", 2680.0, "a")], [_mk("REVENUE", 2680.0, "b")], queue=queue)
    assert result.n_enqueued == 0
    assert queue.list_pending() == []


def test_agreed_count_matches_len():
    """story #15 n_auto_passed 恒等于 agreed 列表长度。"""
    pairs = [("REVENUE", 1.0), ("NEWSALES", 2.0), ("PROFIT", 3.0)]
    facts_a = [_mk(m, v, "a") for m, v in pairs]
    facts_b = [_mk(m, v, "b") for m, v in pairs]
    result = verify(facts_a, facts_b)
    assert result.n_auto_passed == len(result.agreed) == 3


# --------------------------------------------------------------------------- #
# D. tolerance 容差行为：=0 与 >0（story #15）
# --------------------------------------------------------------------------- #
def test_tolerance_zero_tiny_diff_is_conflict():
    """story #15 tolerance=0 时任何非零差值都判冲突（REVENUE 差 1 -> conflict）。"""
    result = verify([_mk("REVENUE", 2680.0, "a")], [_mk("REVENUE", 2681.0, "b")], tolerance=0.0)
    assert result.agreed == []
    assert len(result.conflicts) == 1


def test_tolerance_positive_within_band_auto_passes():
    """story #15 tolerance>0 时差值落在容差带内 -> agreed（差 1 <= 容差 2）。"""
    result = verify([_mk("REVENUE", 2680.0, "a")], [_mk("REVENUE", 2681.0, "b")], tolerance=2.0)
    assert len(result.agreed) == 1
    assert result.conflicts == []


def test_tolerance_boundary_inclusive():
    """story #15 容差边界包含相等：差值恰等于 tolerance 仍算一致（<= 而非 <）。"""
    result = verify([_mk("REVENUE", 100.0, "a")], [_mk("REVENUE", 105.0, "b")], tolerance=5.0)
    assert len(result.agreed) == 1
    assert result.conflicts == []


def test_tolerance_just_over_boundary_is_conflict():
    """story #15 差值刚超过 tolerance -> 判冲突（5.0001 > 容差 5）。"""
    result = verify([_mk("REVENUE", 100.0, "a")], [_mk("REVENUE", 105.0001, "b")], tolerance=5.0)
    assert result.agreed == []
    assert len(result.conflicts) == 1


def test_tolerance_is_absolute_symmetric():
    """story #15 容差判定取绝对差，A<B 与 A>B 对称（B 比 A 小同样在带内）。"""
    result = verify([_mk("REVENUE", 105.0, "a")], [_mk("REVENUE", 100.0, "b")], tolerance=5.0)
    assert len(result.agreed) == 1
    assert result.conflicts == []


# --------------------------------------------------------------------------- #
# E. conflict 入队：reason / 优先级 / payload 含双方值与 locator（story #22）
# --------------------------------------------------------------------------- #
def test_conflict_enqueued_with_reason_and_priority(queue):
    """story #22 冲突项入队：reason='dual_channel_conflict'、priority=PRIORITY_CONFLICT。"""
    verify([_mk("NEWSALES", 4750.0, "docling")], [_mk("NEWSALES", 9999.0, "text")], queue=queue)
    pending = queue.list_pending()
    assert len(pending) == 1
    assert pending[0].reason == REASON_CONFLICT
    assert pending[0].priority == PRIORITY_CONFLICT
    assert pending[0].status == STATUS_PENDING


def test_conflict_payload_carries_both_values_and_locators(queue):
    """story #22 冲突队列项 payload 含双方值与各自 locator / channel_name（回链原文）。"""
    fa = _mk("NEWSALES", 4750.0, "docling", source_locator="page1_table1!R3C4")
    fb = _mk("NEWSALES", 9999.0, "text_layer", source_locator="p1t1_text!newsales_fy2024")
    verify([fa], [fb], queue=queue)

    payload = queue.list_pending()[0].payload
    # 双方数值都必须出现在载荷中（不假设具体嵌套结构，做集合包含断言）。
    blob = repr(payload)
    assert "4750" in blob and "9999" in blob
    assert "page1_table1!R3C4" in blob
    assert "p1t1_text!newsales_fy2024" in blob
    assert "docling" in blob and "text_layer" in blob


def test_conflict_detail_recorded_in_result(queue):
    """story #22 result.conflicts 每项记录维度键与双方值（供上层展示）。"""
    fa = _mk("NEWSALES", 4750.0, "docling")
    fb = _mk("NEWSALES", 9999.0, "text_layer")
    result = verify([fa], [fb], queue=queue)

    assert len(result.conflicts) == 1
    detail = result.conflicts[0]
    blob = repr(detail)
    assert tuple(detail["dim_key"]) == fa.dim_key()
    assert "4750" in blob and "9999" in blob


# --------------------------------------------------------------------------- #
# F. single_only 入队：reason / 更低优先级（story #22）
# --------------------------------------------------------------------------- #
def test_single_only_enqueued_with_reason_and_priority(queue):
    """story #22 单通道独有项入队：reason='single_channel_only'、priority=PRIORITY_SINGLE_ONLY。"""
    verify([_mk("PROFIT", 2210.0, "docling")], [], queue=queue)
    pending = queue.list_pending()
    assert len(pending) == 1
    assert pending[0].reason == REASON_SINGLE_ONLY
    assert pending[0].priority == PRIORITY_SINGLE_ONLY


def test_single_only_payload_carries_value_and_locator(queue):
    """story #22 单通道独有项 payload 含该侧值与 locator / channel_name。"""
    fb = _mk("ROE", 14.0, "text_layer", source_locator="p1t1_text!roe_fy2024")
    verify([], [fb], queue=queue)
    payload = queue.list_pending()[0].payload
    blob = repr(payload)
    assert "14" in blob
    assert "p1t1_text!roe_fy2024" in blob
    assert "text_layer" in blob


def test_conflict_priority_higher_than_single_only(queue):
    """story #22 冲突优先于单通道独有（priority 更小）-> list_pending 冲突排在前。"""
    facts_a = [_mk("NEWSALES", 4750.0, "docling"), _mk("PROFIT", 2210.0, "docling")]
    facts_b = [_mk("NEWSALES", 9999.0, "text"), _mk("REVENUE", 1.0, "text")]
    verify(facts_a, facts_b, queue=queue)

    pending = queue.list_pending()
    # list_pending 按 priority 升序：冲突(10) 必排在单通道独有(50) 之前。
    assert pending[0].reason == REASON_CONFLICT
    assert pending[0].priority < pending[-1].priority
    assert PRIORITY_CONFLICT < PRIORITY_SINGLE_ONLY


# --------------------------------------------------------------------------- #
# G. queue=None：只分类不入队，但统计照常（story #15）
# --------------------------------------------------------------------------- #
def test_no_queue_classifies_but_zero_enqueued():
    """story #15 不给 queue：四象限照常划分、n_enqueued=0（统计不依赖入队副作用）。"""
    facts_a = [_mk("NEWSALES", 4750.0, "a"), _mk("PROFIT", 2210.0, "a")]
    facts_b = [_mk("NEWSALES", 9999.0, "b"), _mk("ROE", 14.0, "b")]
    result = verify(facts_a, facts_b)

    assert len(result.conflicts) == 1
    assert len(result.only_in_a) == 1
    assert len(result.only_in_b) == 1
    assert result.n_enqueued == 0


def test_no_queue_does_not_raise_on_conflict_and_single():
    """story #15 即便存在冲突 / 单通道独有，queue=None 也不应报错（入队是可选副作用）。"""
    result = verify([_mk("NEWSALES", 1.0, "a")], [_mk("NEWSALES", 2.0, "b")])
    assert len(result.conflicts) == 1
    assert result.n_enqueued == 0


# --------------------------------------------------------------------------- #
# H. n_enqueued 与入队总数一致（story #22）
# --------------------------------------------------------------------------- #
def test_n_enqueued_equals_conflicts_plus_single_only(queue):
    """story #22 n_enqueued == 冲突数 + 单通道独有数（agreed 不计入）。"""
    facts_a = [
        _mk("REVENUE", 2680.0, "a"),   # agreed
        _mk("NEWSALES", 4750.0, "a"),    # conflict
        _mk("PROFIT", 2210.0, "a"),   # only_in_a
    ]
    facts_b = [
        _mk("REVENUE", 2680.0, "b"),   # agreed
        _mk("NEWSALES", 9999.0, "b"),    # conflict
        _mk("ROE", 14.0, "b"),      # only_in_b
    ]
    result = verify(facts_a, facts_b, queue=queue)

    expected = len(result.conflicts) + len(result.only_in_a) + len(result.only_in_b)
    assert result.n_enqueued == expected
    assert len(queue.list_pending()) == result.n_enqueued


# --------------------------------------------------------------------------- #
# I. 幂等：同输入两次 verify 结果一致（story #15）
# --------------------------------------------------------------------------- #
def test_idempotent_same_inputs_same_result(dual_vector):
    """story #15 同输入两次 verify -> 四象限计数与键集合完全一致（纯函数，无隐藏状态）。"""
    facts_a = _facts_from(dual_vector["facts_a"])
    facts_b = _facts_from(dual_vector["facts_b"])

    r1 = verify(facts_a, facts_b, tolerance=dual_vector["tolerance"])
    r2 = verify(facts_a, facts_b, tolerance=dual_vector["tolerance"])

    assert _key_set(r1.agreed) == _key_set(r2.agreed)
    assert {tuple(c["dim_key"]) for c in r1.conflicts} == {
        tuple(c["dim_key"]) for c in r2.conflicts
    }
    assert _key_set(r1.only_in_a) == _key_set(r2.only_in_a)
    assert _key_set(r1.only_in_b) == _key_set(r2.only_in_b)
    assert (r1.n_auto_passed, r1.n_enqueued) == (r2.n_auto_passed, r2.n_enqueued)


def test_does_not_mutate_input_lists(dual_vector):
    """story #15 verify 不得改动入参列表（不增删元素），保证可重复调用。"""
    facts_a = _facts_from(dual_vector["facts_a"])
    facts_b = _facts_from(dual_vector["facts_b"])
    len_a, len_b = len(facts_a), len(facts_b)

    verify(facts_a, facts_b, tolerance=dual_vector["tolerance"])

    assert len(facts_a) == len_a
    assert len(facts_b) == len_b


# --------------------------------------------------------------------------- #
# J. 维度键对齐刁钻边界（story #15）
# --------------------------------------------------------------------------- #
def test_same_metric_different_period_not_aligned(queue):
    """story #15 同 metric 不同 period 维度键不同 -> 不对齐，各自落单通道独有。"""
    fa = _mk("REVENUE", 2350.0, "a", period="FY2023")
    fb = _mk("REVENUE", 2680.0, "b", period="FY2024")
    result = verify([fa], [fb], queue=queue)

    assert result.agreed == []
    assert result.conflicts == []
    assert _key_set(result.only_in_a) == {fa.dim_key()}
    assert _key_set(result.only_in_b) == {fb.dim_key()}
    assert result.n_enqueued == 2


def test_same_metric_different_channel_dim_not_aligned():
    """story #15 同 metric 不同 channel 维度（TOTAL vs AGENCY）-> 不对齐。"""
    fa = _mk("REVENUE", 100.0, "a", channel="TOTAL")
    fb = _mk("REVENUE", 100.0, "b", channel="AGENCY")
    result = verify([fa], [fb])
    assert result.agreed == []
    assert _key_set(result.only_in_a) == {fa.dim_key()}
    assert _key_set(result.only_in_b) == {fb.dim_key()}


def test_multiple_metrics_independent_alignment():
    """story #15 多 metric 各自按维度键独立对齐，互不串台。"""
    facts_a = [_mk("REVENUE", 100.0, "a"), _mk("NEWSALES", 200.0, "a"), _mk("PROFIT", 300.0, "a")]
    facts_b = [_mk("REVENUE", 100.0, "b"), _mk("NEWSALES", 999.0, "b"), _mk("ROE", 14.0, "b")]
    result = verify(facts_a, facts_b)

    assert _key_set(result.agreed) == {_mk("REVENUE", 100.0, "a").dim_key()}
    assert {tuple(c["dim_key"]) for c in result.conflicts} == {
        _mk("NEWSALES", 0, "a").dim_key()
    }
    assert _key_set(result.only_in_a) == {_mk("PROFIT", 0, "a").dim_key()}
    assert _key_set(result.only_in_b) == {_mk("ROE", 0, "b").dim_key()}
