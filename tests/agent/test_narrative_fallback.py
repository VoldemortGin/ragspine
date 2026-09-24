"""路由回落（ADR 0023）：结构化缺指标 / 实体或指标不在 profile / 结构化零命中 → 先回落叙事，
叙事也无依据才回答“查不到”（缺指标则回到原反问）。

反捏造的精确化：回落答案必须带来源，且必须含至少一个出现在检索片段原文里的数字（问句里已有的
数字不算）；模型输出 NO_ANSWER / 检索为空 / 无 retriever → 原结构化结果逐字节不变。
"""

import json
import logging
import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion, Choice, FunctionCall, ResponseMessage, ToolCall

from ragspine.agent.agent import (
    FALLBACK_MISSING_METRIC,
    FALLBACK_STRUCTURED_NO_HIT,
    NARRATIVE_FALLBACK_ENV,
    NARRATIVE_NO_ANSWER,
    answer_question,
)
from ragspine.agent.intent import (
    CLARIFY_ASK_FIRST,
    CLARIFY_NONE,
    CLARIFY_OUT_OF_SCOPE_ENTITY,
)
from ragspine.agent.llm_provider import MockProvider
from ragspine.storage.fact_store import Fact, SqliteFactStore

REF = date(2026, 6, 12)

REVENUE_HK_FY2025 = Fact(
    metric_code="REVENUE",
    entity="ACME_HK",
    geography="HK",
    channel="TOTAL",
    period_type="FY",
    period="2025",
    value=1702.0,
    unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx",
    source_locator="slide=5,table=1,row=2,col=3",
)

VONB_SNIPPET = {
    "text": "VONB grew 19% to US$4,712 million in 2024, driven by agency productivity.",
    "doc_id": "RESULTS_2024.md",
    "locator": "page=4",
}
ROE_SNIPPET = {
    "text": "集团去年 ROE 达到 17.5%，创历史新高。",
    "doc_id": "GROUP_QBR_2025.pptx",
    "locator": "slide=3",
}
NO_NUMBER_SNIPPET = {
    "text": "香港 REVENUE 下降主因是 MCV 客群收缩与银保渠道调整。",
    "doc_id": "HK_QBR_2025Q4.pptx",
    "locator": "slide=12",
}


@pytest.fixture
def store(tmp_db_path):
    fs = SqliteFactStore(tmp_db_path)
    fs.init_schema()
    fs.upsert_facts([REVENUE_HK_FY2025])
    yield fs
    fs.close()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(NARRATIVE_FALLBACK_ENV, raising=False)


class ScriptedProvider:
    def __init__(self, responses: list[ChatCompletion]):
        self._responses = list(responses)
        self.messages: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.messages.append(messages)
        return self._responses.pop(0)


class SentinelProvider:
    def chat(self, messages, *, tools=None):
        raise AssertionError("provider 不应被调用")


class FakeRetriever:
    def __init__(self, snippets: list[dict]):
        self.snippets = snippets
        self.calls: list[dict] = []

    def retrieve(self, query: str, *, filters: dict | None = None, top_k: int = 50):
        self.calls.append({"query": query, "filters": filters, "top_k": top_k})
        return [dict(s) for s in self.snippets]


class ExplodingRetriever:
    def retrieve(self, query, *, filters=None, top_k=50):
        raise AssertionError("retriever 不应被调用")


def _tool_use_response(input_: dict) -> ChatCompletion:
    tc = ToolCall(
        id="toolu_1",
        function=FunctionCall(
            name="query_metric", arguments=json.dumps(input_, ensure_ascii=False)
        ),
    )
    msg = ResponseMessage(role="assistant", content=None, tool_calls=(tc,))
    return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="tool_calls"),))


def _text_response(text: str) -> ChatCompletion:
    msg = ResponseMessage(role="assistant", content=text)
    return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="stop"),))


# ---------------------------------------------------------------------------
# 回落成功
# ---------------------------------------------------------------------------


def test_missing_metric_falls_back_to_narrative(store):
    """VONB 不在 profile 指标词表 → 以前反问；现在回落叙事，答案带来源。"""
    retriever = FakeRetriever([VONB_SNIPPET])
    provider = ScriptedProvider([_text_response("VONB grew 19% in 2024.")])
    result = answer_question(
        "What was the VONB growth in 2024?",
        store,
        provider,
        reference_date=REF,
        narrative_retriever=retriever,
    )
    assert result.route == "narrative"
    assert result.fallback == FALLBACK_MISSING_METRIC
    assert result.clarification is not None
    assert result.clarification.mode == CLARIFY_NONE
    assert "19%" in result.answer
    assert "RESULTS_2024.md" in result.answer  # 来源强制
    assert result.sources == [{"doc": "RESULTS_2024.md", "locator": "page=4"}]
    assert result.answer_plain == "VONB grew 19% in 2024."
    assert retriever.calls[0]["query"] == "What was the VONB growth in 2024?"
    assert retriever.calls[0]["filters"] == {"period": "2024"}
    # 回落时 system prompt 要求无依据输出 NO_ANSWER；user prompt 与普通叙事同形。
    assert NARRATIVE_NO_ANSWER in provider.messages[0][0]["content"]
    assert NARRATIVE_NO_ANSWER not in provider.messages[0][-1]["content"]


def test_entity_not_in_profile_falls_back(store):
    """模型把 profile 外实体（AIA）传给工具 → unrecognized_param → 回落叙事。"""
    retriever = FakeRetriever([ROE_SNIPPET])
    provider = ScriptedProvider(
        [
            _tool_use_response({"metric": "ROE", "entity": "AIA", "period": "FY2025"}),
            _text_response("无法识别实体。"),
            _text_response("去年 ROE 为 17.5%。"),
        ]
    )
    result = answer_question(
        "What was AIA's ROE last year?",
        store,
        provider,
        reference_date=REF,
        narrative_retriever=retriever,
    )
    assert result.tool_results[0]["status"] == "unrecognized_param"
    assert result.route == "narrative"
    assert result.fallback == FALLBACK_STRUCTURED_NO_HIT
    assert "17.5%" in result.answer
    assert "GROUP_QBR_2025.pptx" in result.answer
    assert "无法识别" not in result.answer


def test_structured_not_found_falls_back(store):
    """默认实体查事实表 not_found → 先回落叙事（MockProvider 回显片段，数字出自片段）。"""
    retriever = FakeRetriever([ROE_SNIPPET])
    result = answer_question(
        "集团去年ROE多少",
        store,
        MockProvider(reference_date=REF),
        reference_date=REF,
        narrative_retriever=retriever,
    )
    assert result.tool_results[0]["status"] == "not_found"
    assert result.route == "narrative"
    assert result.fallback == FALLBACK_STRUCTURED_NO_HIT
    assert "17.5%" in result.answer
    assert "查不到" not in result.answer
    assert result.sources == [{"doc": "GROUP_QBR_2025.pptx", "locator": "slide=3"}]


def test_fallback_trace_records_reason_not_content(store, caplog):
    retriever = FakeRetriever([ROE_SNIPPET])
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        answer_question(
            "集团去年ROE多少",
            store,
            MockProvider(reference_date=REF),
            reference_date=REF,
            narrative_retriever=retriever,
        )
    traces = [r for r in caplog.records if r.name == "ragspine.trace"]
    assert len(traces) == 1
    assert getattr(traces[0], "narrative_fallback", None) == {
        "reason": FALLBACK_STRUCTURED_NO_HIT,
        "grounded": True,
    }
    # 回落有依据 → 没有发生“查不到”改写。
    assert getattr(traces[0], "fabrication_guard_triggered", None) is False


# ---------------------------------------------------------------------------
# 回落无依据 → 查不到（缺指标 → 原反问）
# ---------------------------------------------------------------------------


def _off(question, store, provider, **kw):
    return answer_question(
        question, store, provider, reference_date=REF, narrative_fallback=False, **kw
    )


def test_fallback_empty_retrieval_keeps_not_found(store):
    retriever = FakeRetriever([])
    result = answer_question(
        "集团去年ROE多少",
        store,
        MockProvider(reference_date=REF),
        reference_date=REF,
        narrative_retriever=retriever,
    )
    baseline = _off("集团去年ROE多少", store, MockProvider(reference_date=REF))
    assert result.route == "structured"
    assert result.fallback is None
    assert "查不到" in result.answer
    assert result.answer == baseline.answer
    assert result.answer_plain == baseline.answer_plain
    assert result.tool_results == baseline.tool_results
    assert result.sources == []


def test_fallback_model_says_no_answer_keeps_not_found(store, caplog):
    retriever = FakeRetriever([ROE_SNIPPET])
    provider = ScriptedProvider(
        [
            _tool_use_response({"metric": "ROE", "entity": "ACME_CN", "period": "FY2025"}),
            _text_response("查不到。"),
            _text_response(NARRATIVE_NO_ANSWER),
        ]
    )
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        result = answer_question(
            "中国去年ROE多少", store, provider, reference_date=REF, narrative_retriever=retriever
        )
    assert result.route == "structured"
    assert result.answer.startswith("查不到：ROE / ACME_CN")
    assert NARRATIVE_NO_ANSWER not in result.answer
    assert result.sources == []
    traces = [r for r in caplog.records if r.name == "ragspine.trace"]
    assert getattr(traces[0], "fabrication_guard_triggered", None) is True
    assert getattr(traces[0], "narrative_fallback", None) == {
        "reason": FALLBACK_STRUCTURED_NO_HIT,
        "grounded": False,
    }


def test_fallback_fabricated_number_is_rejected(store):
    """模型空口给出片段里没有的数字 → 视为无依据 → 查不到，编造数字不外泄。"""
    retriever = FakeRetriever([NO_NUMBER_SNIPPET])
    provider = ScriptedProvider(
        [
            _tool_use_response({"metric": "ROE", "entity": "ACME_CN", "period": "FY2025"}),
            _text_response("查不到。"),
            _text_response("ACME 中国去年 ROE 为 9999%。"),
        ]
    )
    result = answer_question(
        "中国去年ROE多少", store, provider, reference_date=REF, narrative_retriever=retriever
    )
    assert "查不到" in result.answer
    assert "9999" not in result.answer
    assert "9999" not in result.answer_plain
    assert result.route == "structured"


def test_fallback_number_only_from_question_is_not_grounding(store):
    """答案里的数字只来自问句（年份）+ 引用标号 → 不算有依据。"""
    retriever = FakeRetriever([VONB_SNIPPET])
    provider = ScriptedProvider([_text_response("片段 [1] 没有给出 2024 年的相关数字。")])
    result = answer_question(
        "What was the VONB growth in 2024?",
        store,
        provider,
        reference_date=REF,
        narrative_retriever=retriever,
    )
    assert result.fallback is None
    assert result.clarification.mode == CLARIFY_ASK_FIRST
    assert result.answer.startswith("查不到")
    assert "2024" not in result.answer


def test_missing_metric_ungrounded_says_not_found_and_keeps_options(store):
    """缺指标且叙事无依据（片段无数字）→ 回答“查不到”，不编造；仍保留 ask_first 澄清对象与
    指标选项（原反问文本附在后面），sources / tool_results 为空。"""
    retriever = FakeRetriever([NO_NUMBER_SNIPPET])
    result = answer_question(
        "香港去年多少",
        store,
        MockProvider(reference_date=REF),
        reference_date=REF,
        narrative_retriever=retriever,
    )
    baseline = _off("香港去年多少", store, SentinelProvider(), narrative_retriever=retriever)
    assert result.clarification == baseline.clarification
    assert result.clarification.mode == CLARIFY_ASK_FIRST
    assert result.answer.startswith("查不到")
    assert result.answer.endswith(baseline.answer)
    assert result.answer_plain == result.answer
    assert result.route == baseline.route
    assert result.fallback is None
    assert result.sources == [] and result.tool_results == []


def test_no_retriever_means_no_fallback_and_no_llm_call(store):
    """未接叙事检索 → 不尝试回落：缺指标仍直接反问、不调 provider。"""
    result = answer_question("香港去年多少", store, SentinelProvider(), reference_date=REF)
    assert result.clarification.mode == CLARIFY_ASK_FIRST
    assert result.fallback is None


# ---------------------------------------------------------------------------
# 不受影响的路径
# ---------------------------------------------------------------------------


def test_found_path_unchanged_and_never_retrieves(store):
    on = answer_question(
        "香港去年REVENUE多少",
        store,
        MockProvider(reference_date=REF),
        reference_date=REF,
        narrative_retriever=ExplodingRetriever(),
    )
    off = _off("香港去年REVENUE多少", store, MockProvider(reference_date=REF))
    assert on == off
    assert "1702" in on.answer
    assert on.route == "structured"
    assert on.fallback is None


def test_competitor_still_refused_first(store):
    result = answer_question(
        "竞安去年REVENUE多少",
        store,
        SentinelProvider(),
        reference_date=REF,
        narrative_retriever=ExplodingRetriever(),
    )
    assert result.clarification.mode == CLARIFY_OUT_OF_SCOPE_ENTITY
    assert result.fallback is None
    assert result.tool_results == [] and result.sources == []


def test_competitor_missing_metric_still_refused_first(store):
    result = answer_question(
        "竞安去年多少",
        store,
        SentinelProvider(),
        reference_date=REF,
        narrative_retriever=ExplodingRetriever(),
    )
    assert result.clarification.mode == CLARIFY_OUT_OF_SCOPE_ENTITY


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    ["What was the VONB growth in 2024?", "香港去年多少", "集团去年ROE多少", "中国去年ROE多少"],
)
def test_switch_off_never_touches_narrative(store, question, caplog):
    """off：结构化缺指标 / 零命中都不检索，结果与引入回落前一致（反问 / 查不到）。"""
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        result = _off(
            question,
            store,
            MockProvider(reference_date=REF),
            narrative_retriever=ExplodingRetriever(),
        )
    assert result.fallback is None
    assert result.route == "structured"
    assert result.sources == []
    if result.clarification.mode == CLARIFY_ASK_FIRST:
        assert result.answer.startswith("想查询哪个指标？")
    else:
        assert "查不到" in result.answer
    traces = [r for r in caplog.records if r.name == "ragspine.trace"]
    assert not hasattr(traces[0], "narrative_fallback")


def test_env_switch_off_equals_param_off(store, monkeypatch):
    monkeypatch.setenv(NARRATIVE_FALLBACK_ENV, "OFF")
    via_env = answer_question(
        "集团去年ROE多少",
        store,
        MockProvider(reference_date=REF),
        reference_date=REF,
        narrative_retriever=ExplodingRetriever(),
    )
    via_param = _off("集团去年ROE多少", store, MockProvider(reference_date=REF))
    assert via_env == via_param


def test_env_switch_default_is_on(store):
    retriever = FakeRetriever([ROE_SNIPPET])
    result = answer_question(
        "集团去年ROE多少",
        store,
        MockProvider(reference_date=REF),
        reference_date=REF,
        narrative_retriever=retriever,
    )
    assert result.fallback == FALLBACK_STRUCTURED_NO_HIT


def test_env_switch_rejects_unknown_value(store, monkeypatch):
    monkeypatch.setenv(NARRATIVE_FALLBACK_ENV, "maybe")
    with pytest.raises(ValueError, match=NARRATIVE_FALLBACK_ENV):
        answer_question(
            "集团去年ROE多少", store, MockProvider(reference_date=REF), reference_date=REF
        )
