"""意图&范围解析测试：相对期间解析（glossary 增量）、槽位抽取、三路分流、澄清网关。

所有相对期间测试显式注入 reference_date（2026-06-12 → 去年=FY2025），绝不依赖系统时钟。
"""

import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.common.glossary import resolve_relative_period
from ragspine.agent.intent import (
    CLARIFY_ANSWER_WITH_ASSUMPTIONS,
    CLARIFY_ASK_FIRST,
    CLARIFY_NONE,
    ROUTE_COMPOSITE,
    ROUTE_NARRATIVE,
    ROUTE_STRUCTURED,
    ClarificationResult,
    ParsedIntent,
    clarify_scope,
    parse_intent,
)

REF = date(2026, 6, 12)


# ---------------------------------------------------------------------------
# 相对期间解析（src/glossary.py 增量扩展，只加不改）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("去年", ("FY", "2025")),
        ("上年", ("FY", "2025")),
        ("last year", ("FY", "2025")),
        ("今年", ("FY", "2026")),
        ("本年", ("FY", "2026")),
        ("this year", ("FY", "2026")),
        ("前年", ("FY", "2024")),
        ("上半年", ("HY", "2026H1")),
        ("下半年", ("HY", "2026H2")),
        ("今年上半年", ("HY", "2026H1")),
        ("去年上半年", ("HY", "2025H1")),
        ("去年下半年", ("HY", "2025H2")),
        ("本季度", ("QUARTER", "2026Q2")),
        ("这个季度", ("QUARTER", "2026Q2")),
        ("上季度", ("QUARTER", "2026Q1")),
        ("上个季度", ("QUARTER", "2026Q1")),
    ],
)
def test_resolve_relative_period(raw, expected):
    assert resolve_relative_period(raw, reference_date=REF) == expected


def test_resolve_relative_period_quarter_rollover():
    """跨年：2026Q1 的上季度是 2025Q4。"""
    assert resolve_relative_period("上季度", reference_date=date(2026, 2, 1)) == (
        "QUARTER",
        "2025Q4",
    )


@pytest.mark.parametrize("raw", [None, "", "garbage", "FY2024", "2024H1"])
def test_resolve_relative_period_rejects_non_relative(raw):
    """绝对期间与垃圾输入都不归相对解析管，返回 None。"""
    assert resolve_relative_period(raw, reference_date=REF) is None


def test_glossary_existing_behavior_untouched():
    """增量约束：normalize_period 原有行为不得改变。"""
    from ragspine.common.glossary import normalize_period

    assert normalize_period("FY2024") == ("FY", "2024")
    assert normalize_period("去年") is None  # 相对期间不进 normalize_period


# ---------------------------------------------------------------------------
# 槽位抽取
# ---------------------------------------------------------------------------

def test_parse_full_question():
    intent = parse_intent("香港去年REVENUE多少", reference_date=REF)
    assert intent.metric == "REVENUE"
    assert intent.entity == "ACME_HK"
    assert intent.period == ("FY", "2025")
    assert intent.channel == "TOTAL"
    assert intent.route == ROUTE_STRUCTURED


def test_parse_chinese_metric_synonym():
    intent = parse_intent("ACME中国今年的营收是多少", reference_date=REF)
    assert intent.metric == "REVENUE"
    assert intent.entity == "ACME_CN"
    assert intent.period == ("FY", "2026")


def test_parse_absolute_period_fy():
    intent = parse_intent("集团 2024 年 PROFIT 多少", reference_date=REF)
    assert intent.metric == "PROFIT"
    assert intent.entity == "ACME_GROUP"
    assert intent.period == ("FY", "2024")


def test_parse_absolute_period_half_year():
    intent = parse_intent("香港2024年上半年NEWSALES多少", reference_date=REF)
    assert intent.metric == "NEWSALES"
    assert intent.period == ("HY", "2024H1")


def test_parse_relative_half_year_in_question():
    intent = parse_intent("香港去年上半年REVENUE多少", reference_date=REF)
    assert intent.period == ("HY", "2025H1")


def test_parse_channel():
    intent = parse_intent("香港去年代理渠道REVENUE多少", reference_date=REF)
    assert intent.channel == "AGENCY"


def test_parse_missing_slots_are_none():
    intent = parse_intent("REVENUE多少", reference_date=REF)
    assert intent.metric == "REVENUE"
    assert intent.entity is None
    assert intent.period is None


def test_parse_english_question():
    intent = parse_intent("What is the REVENUE of ACME Hong Kong in FY2025?", reference_date=REF)
    assert intent.metric == "REVENUE"
    assert intent.entity == "ACME_HK"
    assert intent.period == ("FY", "2025")


# ---------------------------------------------------------------------------
# 三路分流
# ---------------------------------------------------------------------------

def test_route_structured():
    assert parse_intent("香港去年REVENUE多少", reference_date=REF).route == ROUTE_STRUCTURED


def test_route_narrative_no_metric():
    intent = parse_intent("香港最近有什么监管动态", reference_date=REF)
    assert intent.route == ROUTE_NARRATIVE


def test_route_narrative_why_without_metric():
    assert parse_intent("为什么竞品增长这么快", reference_date=REF).route == ROUTE_NARRATIVE


def test_route_composite():
    intent = parse_intent("香港去年REVENUE多少，为什么下降了", reference_date=REF)
    assert intent.route == ROUTE_COMPOSITE
    assert intent.metric == "REVENUE"


def test_route_structured_numeric_cue_without_metric():
    """有"多少"数字意图但识别不出指标 → 仍归 structured（由澄清网关前置反问）。"""
    intent = parse_intent("香港去年多少", reference_date=REF)
    assert intent.route == ROUTE_STRUCTURED
    assert intent.metric is None


# ---------------------------------------------------------------------------
# 澄清网关：默认先答 + 显式暴露假设 + 一键收窄；仅实质歧义才前置单选
# ---------------------------------------------------------------------------

def test_clarify_none_when_scope_complete():
    intent = parse_intent("香港去年REVENUE多少", reference_date=REF)
    clar = clarify_scope(intent, reference_date=REF)
    assert clar.mode == CLARIFY_NONE
    assert clar.assumed_slots == {}


def test_clarify_assumes_entity_and_period():
    """缺实体+期间：默认集团口径 + 最近完整财年，先答并暴露假设。"""
    intent = parse_intent("REVENUE多少", reference_date=REF)
    clar = clarify_scope(intent, reference_date=REF)
    assert clar.mode == CLARIFY_ANSWER_WITH_ASSUMPTIONS
    assert clar.assumed_slots["entity"] == "ACME_GROUP"
    assert clar.assumed_slots["period"] == ("FY", "2025")
    assert clar.assumption_note  # 非空说明文字
    assert len(clar.narrowing_options) >= 2  # 一键收窄选项


def test_clarify_assumes_period_only():
    intent = parse_intent("香港REVENUE多少", reference_date=REF)
    clar = clarify_scope(intent, reference_date=REF)
    assert clar.mode == CLARIFY_ANSWER_WITH_ASSUMPTIONS
    assert "entity" not in clar.assumed_slots
    assert clar.assumed_slots["period"] == ("FY", "2025")


def test_clarify_ask_first_when_metric_missing():
    """指标缺失会导致实质错误 → 前置单选，绝不瞎猜指标。"""
    intent = parse_intent("香港去年多少", reference_date=REF)
    clar = clarify_scope(intent, reference_date=REF)
    assert clar.mode == CLARIFY_ASK_FIRST
    assert clar.question
    assert "REVENUE" in clar.question  # 给出可选指标


def test_clarify_narrative_passthrough():
    """叙事路线不强制结构化槽位。"""
    intent = parse_intent("香港最近有什么监管动态", reference_date=REF)
    clar = clarify_scope(intent, reference_date=REF)
    assert clar.mode == CLARIFY_NONE


def test_clarification_result_shape():
    """结构化澄清结果：字段齐备，可序列化给前端做澄清 chips。"""
    clar = ClarificationResult(
        mode=CLARIFY_NONE, assumed_slots={}, assumption_note=None,
        narrowing_options=[], question=None,
    )
    assert clar.mode == CLARIFY_NONE
