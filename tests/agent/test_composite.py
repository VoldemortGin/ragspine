"""Composite 多指标/多实体/多期间对比测试（TDD 红色阶段）。

覆盖：
- 多槽位解析：ParsedIntent 新增 metrics/entities/periods 列表字段（只增不改：
  既有单槽位字段 metric/entity/period 语义逐字节不变）；
- 子任务展开 expand_subtasks：只在用户明确列举（len>1）的轴上做笛卡尔展开，
  未列举的轴用单值/默认值固定，不做全笛卡尔积；
- agent 多子任务执行：确定性执行多次 query_metric，合成对比式回答——每个数
  都带血缘；任一 not_found 子项明确说查不到、绝不编造、不拖垮其他子项；
  多子任务数字通路不调用 LLM（SentinelProvider 断言）。

全部离线确定性，reference_date 固定 2026-06-12（去年=FY2025）。
"""

import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import answer_question
from ragspine.storage.fact_store import Fact, SqliteFactStore
from ragspine.agent.intent import (
    CLARIFY_ANSWER_WITH_ASSUMPTIONS,
    ROUTE_COMPOSITE,
    ROUTE_STRUCTURED,
    SubTask,
    expand_subtasks,
    parse_intent,
)
from ragspine.agent.llm_provider import MockProvider

REF = date(2026, 6, 12)


def _fact(metric, entity, geography, period, value, doc, locator, period_type="FY"):
    return Fact(
        metric_code=metric, entity=entity, geography=geography, channel="TOTAL",
        period_type=period_type, period=period, value=value, unit="USD_M",
        source_doc_id=doc, source_locator=locator,
    )


FACTS = [
    _fact("REVENUE", "ACME_HK", "HK", "2025", 1702.0,
          "ACME_FY2025_Results.pptx", "slide=5,table=1,row=2,col=3"),
    _fact("REVENUE", "ACME_CN", "CN", "2025", 800.0,
          "ACME_FY2025_Results.pptx", "slide=6,table=1,row=2,col=3"),
    _fact("NEWSALES", "ACME_HK", "HK", "2025", 2500.0,
          "ACME_FY2025_Results.pptx", "slide=7,table=1,row=2,col=3"),
    _fact("REVENUE", "ACME_HK", "HK", "2024", 1400.0,
          "ACME_FY2024_Results.pptx", "slide=5,table=1,row=2,col=3"),
]


@pytest.fixture
def store(tmp_db_path):
    fs = SqliteFactStore(tmp_db_path)
    fs.init_schema()
    fs.upsert_facts(FACTS)
    yield fs
    fs.close()


class SentinelProvider:
    """一旦被调用即失败：断言多子任务数字通路不触发 LLM。"""

    def chat(self, messages, *, tools=None):
        raise AssertionError("provider 不应被调用")


class FakeRetriever:
    """duck-typed NarrativeRetriever：返回固定片段。"""

    def retrieve(self, query, *, filters=None, top_k=50):
        return [{
            "text": "香港 REVENUE 下降主因是 MCV 客群收缩。",
            "doc_id": "HK_QBR_2025Q4.pptx",
            "locator": "slide=12",
        }]


# ---------------------------------------------------------------------------
# 多槽位解析：metrics / entities / periods 列表字段
# ---------------------------------------------------------------------------

def test_parse_multi_entities():
    intent = parse_intent("香港和中国去年REVENUE各是多少", reference_date=REF)
    assert intent.entities == ["ACME_HK", "ACME_CN"]
    assert intent.metrics == ["REVENUE"]
    assert intent.periods == [("FY", "2025")]
    assert intent.route == ROUTE_STRUCTURED
    # 既有单槽位字段语义不变
    assert intent.metric == "REVENUE"
    assert intent.period == ("FY", "2025")


def test_parse_multi_metrics():
    intent = parse_intent("ACME HK 的 REVENUE 和 NEWSALES 多少", reference_date=REF)
    assert intent.metrics == ["REVENUE", "NEWSALES"]
    assert intent.entities == ["ACME_HK"]  # 'acme hk' 不得再叠出 ACME_GROUP


def test_parse_multi_periods():
    intent = parse_intent("香港REVENUE 2024和2025对比", reference_date=REF)
    assert intent.periods == [("FY", "2024"), ("FY", "2025")]
    assert intent.entities == ["ACME_HK"]
    assert intent.metrics == ["REVENUE"]
    assert intent.route == ROUTE_STRUCTURED


def test_parse_single_question_lists_are_singletons():
    intent = parse_intent("香港去年REVENUE多少", reference_date=REF)
    assert intent.metrics == ["REVENUE"]
    assert intent.entities == ["ACME_HK"]
    assert intent.periods == [("FY", "2025")]


def test_parse_dedupes_synonyms_of_same_code():
    intent = parse_intent("香港和ACME香港的REVENUE多少", reference_date=REF)
    assert intent.entities == ["ACME_HK"]


def test_parse_missing_axes_give_empty_lists():
    intent = parse_intent("REVENUE多少", reference_date=REF)
    assert intent.metrics == ["REVENUE"]
    assert intent.entities == []
    assert intent.periods == []


def test_parse_mixed_relative_and_absolute_periods():
    intent = parse_intent("香港REVENUE去年和2023年对比", reference_date=REF)
    assert intent.periods == [("FY", "2025"), ("FY", "2023")]


# ---------------------------------------------------------------------------
# 子任务展开：只在明确列举的轴上展开
# ---------------------------------------------------------------------------

def test_expand_multi_entity_single_axis():
    intent = parse_intent("香港和中国去年REVENUE各是多少", reference_date=REF)
    tasks = expand_subtasks(intent)
    assert tasks == [
        SubTask(metric="REVENUE", entity="ACME_HK", period=("FY", "2025"), channel="TOTAL"),
        SubTask(metric="REVENUE", entity="ACME_CN", period=("FY", "2025"), channel="TOTAL"),
    ]


def test_expand_cartesian_only_on_enumerated_axes():
    """实体、指标两轴都明确列举 → 2x2；period 单值固定，不参与扩张。"""
    intent = parse_intent("香港和中国2025年的REVENUE和NEWSALES各是多少", reference_date=REF)
    tasks = expand_subtasks(intent)
    assert [(t.entity, t.metric) for t in tasks] == [
        ("ACME_HK", "REVENUE"), ("ACME_HK", "NEWSALES"),
        ("ACME_CN", "REVENUE"), ("ACME_CN", "NEWSALES"),
    ]
    assert all(t.period == ("FY", "2025") for t in tasks)


def test_expand_single_question_yields_one_task():
    intent = parse_intent("香港去年REVENUE多少", reference_date=REF)
    tasks = expand_subtasks(intent)
    assert tasks == [
        SubTask(metric="REVENUE", entity="ACME_HK", period=("FY", "2025"), channel="TOTAL"),
    ]


def test_expand_uses_defaults_for_missing_axes():
    intent = parse_intent("REVENUE和NEWSALES多少", reference_date=REF)
    tasks = expand_subtasks(
        intent, default_entity="ACME_GROUP", default_period=("FY", "2025")
    )
    assert [(t.metric, t.entity, t.period) for t in tasks] == [
        ("REVENUE", "ACME_GROUP", ("FY", "2025")),
        ("NEWSALES", "ACME_GROUP", ("FY", "2025")),
    ]


def test_expand_keeps_channel():
    intent = parse_intent("香港和中国去年代理渠道REVENUE各是多少", reference_date=REF)
    tasks = expand_subtasks(intent)
    assert len(tasks) == 2
    assert all(t.channel == "AGENCY" for t in tasks)


# ---------------------------------------------------------------------------
# agent 多子任务执行：确定性、带血缘、not_found 不拖垮、不调 LLM
# ---------------------------------------------------------------------------

def test_agent_multi_entity_compare_all_found(store):
    result = answer_question(
        "香港和中国去年REVENUE各是多少", store, SentinelProvider(), reference_date=REF
    )
    assert result.route == ROUTE_STRUCTURED
    assert "1702" in result.answer and "800" in result.answer
    assert "slide=5,table=1,row=2,col=3" in result.answer
    assert "slide=6,table=1,row=2,col=3" in result.answer
    assert [r["status"] for r in result.tool_results] == ["found", "found"]
    assert len(result.sources) == 2


def test_agent_multi_metric_compare(store):
    result = answer_question(
        "ACME HK 去年的 REVENUE 和 NEWSALES 各是多少", store, SentinelProvider(),
        reference_date=REF,
    )
    assert "1702" in result.answer and "2500" in result.answer
    assert [r["status"] for r in result.tool_results] == ["found", "found"]


def test_agent_period_compare_with_delta(store):
    result = answer_question(
        "香港REVENUE 2024和2025对比", store, SentinelProvider(), reference_date=REF
    )
    assert "1400" in result.answer and "1702" in result.answer
    assert "ACME_FY2024_Results.pptx" in result.answer
    assert "ACME_FY2025_Results.pptx" in result.answer
    assert "302" in result.answer  # 两期可比 → 给出确定性差值


def test_agent_multi_partial_not_found_isolated(store):
    """CN 没有 NEWSALES：该子项明确说查不到且不编造，HK 子项照常作答。"""
    result = answer_question(
        "香港和中国去年NEWSALES各是多少", store, SentinelProvider(), reference_date=REF
    )
    assert "2500" in result.answer
    assert "查不到" in result.answer
    assert [r["status"] for r in result.tool_results] == ["found", "not_found"]
    assert [s["doc"] for s in result.sources] == ["ACME_FY2025_Results.pptx"]


def test_agent_multi_with_assumed_period(store):
    """多实体但缺期间：默认先答（FY2025）+ 显式暴露假设，澄清网关行为不变。"""
    result = answer_question(
        "香港和中国的REVENUE是多少", store, SentinelProvider(), reference_date=REF
    )
    assert result.clarification.mode == CLARIFY_ANSWER_WITH_ASSUMPTIONS
    assert "假设" in result.answer
    assert "1702" in result.answer and "800" in result.answer


def test_agent_composite_multi_plus_narrative(store):
    """多实体数字 + 归因叙事在同一回答中汇合（composite 路）。"""
    result = answer_question(
        "香港和中国去年REVENUE各是多少，为什么香港下降了", store,
        MockProvider(reference_date=REF), reference_date=REF,
        narrative_retriever=FakeRetriever(),
    )
    assert result.route == ROUTE_COMPOSITE
    assert "1702" in result.answer and "800" in result.answer
    assert "归因分析" in result.answer
    assert "HK_QBR_2025Q4.pptx" in result.answer


def test_agent_single_question_path_unchanged(store):
    """单槽位问题仍走既有 tool use 循环（MockProvider 行为逐字节不变）。"""
    result = answer_question(
        "香港去年REVENUE多少", store, MockProvider(reference_date=REF),
        reference_date=REF,
    )
    assert "1702" in result.answer
    assert "ACME_FY2025_Results.pptx" in result.answer
    assert len(result.tool_results) == 1
