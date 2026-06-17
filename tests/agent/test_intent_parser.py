"""IntentParser 协议 + 注入 seam 规格（TDD，ADR 0010）。

ADR 0010 把"意图抽取"做成可插拔的 `IntentParser` Protocol：默认零-LLM 规则实现
（`RuleIntentParser`），可换 LLM 后端（extra）。本模块钉死：
    1) 规则实现与既有 `parse_intent` 完全等价（行为不变）；
    2) `answer_question` 接受注入的 parser 并真正使用它；
    3) 关键不变量——安全门独立于注入的 parser：即便 parser 故意漏判竞品，
       编排层仍确定性拒答（"安全确定，意图灵活"）。
"""

import os
from dataclasses import dataclass, field
from datetime import date

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.agent.intent import (
    CLARIFY_OUT_OF_SCOPE_ENTITY,
    ROUTE_NARRATIVE,
    ROUTE_STRUCTURED,
    IntentParser,
    ParsedIntent,
    RuleIntentParser,
    parse_intent,
)
from ragspine.agent.llm_provider import MockProvider
from ragspine.storage.fact_store import FactStore

REF = date(2026, 6, 12)


class _SentinelProvider:
    """任何对 LLM 的调用都判为越权前门失守——直接炸测试。"""

    def create_message(self, *, system, messages, tools):  # noqa: D401
        raise AssertionError("LLM 不应被调用：越权应在任何 LLM/工具调用前拒答")


@dataclass
class _RecordingParser:
    """记录调用、返回一个固定 ParsedIntent 的假 parser（验证注入生效）。"""

    intent: ParsedIntent
    calls: list[str] = field(default_factory=list)

    def parse(self, question: str, *, reference_date: date | None = None) -> ParsedIntent:
        self.calls.append(question)
        return self.intent


# --------------------------------------------------------------------------
# 1) 规则实现等价 + 满足 Protocol
# --------------------------------------------------------------------------

def test_rule_parser_matches_parse_intent():
    parser = RuleIntentParser()
    for q in (
        "香港去年REVENUE多少",
        "ACME中国今年的营收是多少",
        "香港最近有什么监管动态",
        "竞安去年REVENUE多少",
    ):
        got = parser.parse(q, reference_date=REF)
        want = parse_intent(q, reference_date=REF)
        assert (got.route, got.metric, got.entity, got.period, got.channel,
                got.external_entity, got.raw_question) == (
            want.route, want.metric, want.entity, want.period, want.channel,
            want.external_entity, want.raw_question)


def test_rule_parser_satisfies_protocol():
    assert isinstance(RuleIntentParser(), IntentParser)


# --------------------------------------------------------------------------
# 2) answer_question 使用注入的 parser
# --------------------------------------------------------------------------

def test_answer_question_uses_injected_parser(tmp_db_path):
    store = FactStore(str(tmp_db_path))
    store.init_schema()
    fake_intent = ParsedIntent(
        route=ROUTE_NARRATIVE, metric=None, entity=None, period=None,
        channel="TOTAL", raw_question="香港最近有什么监管动态",
    )
    parser = _RecordingParser(intent=fake_intent)

    # narrative 路 + 无 retriever → 坦白降级，不触达 provider。
    result = answer_question(
        "随便问点什么", store, _SentinelProvider(),
        reference_date=REF, intent_parser=parser,
    )
    assert isinstance(result, AgentResult)
    assert parser.calls == ["随便问点什么"]  # 注入的 parser 被调用
    assert result.route == ROUTE_NARRATIVE  # 路由来自注入 parser 的产出


# --------------------------------------------------------------------------
# 3) 安全门独立于注入 parser（核心解耦不变量）
# --------------------------------------------------------------------------

def test_security_gate_independent_of_pluggable_parser(tmp_db_path):
    """注入一个【故意漏判竞品】的 parser（external_entity=None），但 raw_question
    含竞品；编排层仍必须确定性拒答——安全门从 raw_question 独立复核，不信任 parser。"""
    store = FactStore(str(tmp_db_path))
    store.init_schema()
    # 假 parser：把竞品问句当成普通结构化查询，external_entity 留空。
    blind_intent = ParsedIntent(
        route=ROUTE_STRUCTURED, metric="REVENUE", entity="ACME_HK",
        period=("FY", "2025"), channel="TOTAL",
        raw_question="竞安去年REVENUE多少", external_entity=None,
    )
    parser = _RecordingParser(intent=blind_intent)

    result = answer_question(
        "竞安去年REVENUE多少", store, _SentinelProvider(),
        reference_date=REF, intent_parser=parser,
    )
    assert result.clarification is not None
    assert result.clarification.mode == CLARIFY_OUT_OF_SCOPE_ENTITY
    assert result.tool_results == []
    assert "竞安" in result.answer
    # provider 未被调用（_SentinelProvider 会炸），即拒答先于任何 LLM/工具。


def test_default_answer_question_still_refuses_competitor(tmp_db_path):
    """不注入 parser 时，默认 RuleIntentParser 行为不变：竞品照常拒答。"""
    store = FactStore(str(tmp_db_path))
    store.init_schema()
    result = answer_question(
        "竞安去年REVENUE多少", store, MockProvider(), reference_date=REF
    )
    assert result.clarification is not None
    assert result.clarification.mode == CLARIFY_OUT_OF_SCOPE_ENTITY
    assert result.tool_results == []
