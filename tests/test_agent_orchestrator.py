"""Agent 编排层测试：三路分流执行、tool use 循环、防编造硬约束、血缘回指、澄清网关接线。

NarrativeRetriever 用 fake 注入（duck-typed Protocol），不依赖另一条线的检索实现。
"""

import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.storage.fact_store import Fact, FactStore
from ragspine.agent.intent import CLARIFY_ANSWER_WITH_ASSUMPTIONS, CLARIFY_ASK_FIRST
from ragspine.agent.llm_provider import MockProvider, ProviderResponse, ToolCall

REF = date(2026, 6, 12)

REVENUE_HK_FY2025 = Fact(
    metric_code="REVENUE", entity="ACME_HK", geography="HK", channel="TOTAL",
    period_type="FY", period="2025", value=1702.0, unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx", source_locator="slide=5,table=1,row=2,col=3",
)


@pytest.fixture
def store(tmp_db_path):
    fs = FactStore(tmp_db_path)
    fs.init_schema()
    fs.upsert_facts([REVENUE_HK_FY2025])
    yield fs
    fs.close()


class ScriptedProvider:
    """按脚本依次吐响应的测试 provider（含试图编造数字的对抗脚本）。"""

    def __init__(self, responses: list[ProviderResponse]):
        self._responses = list(responses)
        self.calls = 0

    def create_message(self, *, system, messages, tools):
        self.calls += 1
        return self._responses.pop(0)


class SentinelProvider:
    """一旦被调用即失败：用于断言某些路径不触发 LLM。"""

    def create_message(self, *, system, messages, tools):
        raise AssertionError("provider 不应被调用")


class FakeRetriever:
    """duck-typed NarrativeRetriever：记录调用参数，返回固定片段。"""

    def __init__(self, snippets: list[dict] | None = None):
        self.snippets = snippets if snippets is not None else [{
            "text": "香港 REVENUE 下降主因是 MCV 客群收缩与银保渠道调整。",
            "doc_id": "HK_QBR_2025Q4.pptx",
            "locator": "slide=12",
        }]
        self.calls: list[dict] = []

    def retrieve(self, query: str, *, filters: dict | None = None, top_k: int = 50):
        self.calls.append({"query": query, "filters": filters, "top_k": top_k})
        return self.snippets


def _tool_use_response(input_: dict) -> ProviderResponse:
    return ProviderResponse(
        text="", stop_reason="tool_use",
        tool_calls=[ToolCall(id="toolu_1", name="query_metric", input=input_)],
        raw_content=[{"type": "tool_use", "id": "toolu_1",
                      "name": "query_metric", "input": input_}],
    )


def _text_response(text: str) -> ProviderResponse:
    return ProviderResponse(text=text, stop_reason="end_turn", tool_calls=[],
                            raw_content=[{"type": "text", "text": text}])


# ---------------------------------------------------------------------------
# structured：端到端 found / not_found / unrecognized
# ---------------------------------------------------------------------------

def test_structured_found_with_lineage(store):
    result = answer_question(
        "香港去年REVENUE多少", store, MockProvider(reference_date=REF), reference_date=REF
    )
    assert isinstance(result, AgentResult)
    assert result.route == "structured"
    assert "1702" in result.answer
    assert "ACME_FY2025_Results.pptx" in result.answer  # 血缘必须出现在回答里
    assert result.tool_results[0]["status"] == "found"
    assert result.sources == [{"doc": "ACME_FY2025_Results.pptx",
                               "locator": "slide=5,table=1,row=2,col=3"}]


def test_structured_not_found_says_so(store):
    result = answer_question(
        "中国去年ROE多少", store, MockProvider(reference_date=REF), reference_date=REF
    )
    assert result.tool_results[0]["status"] == "not_found"
    assert "查不到" in result.answer
    assert result.sources == []


def test_not_found_hard_constraint_overrides_fabrication(store):
    """硬约束：即使模型在 not_found 后编造数字，编排层也必须拦截改写。"""
    provider = ScriptedProvider([
        _tool_use_response({"metric": "ROE", "entity": "ACME_CN", "period": "FY2024"}),
        _text_response("ACME 中国 FY2024 ROE 为 9999%，表现亮眼。"),  # 对抗：编造
    ])
    result = answer_question("中国2024年ROE多少", store, provider, reference_date=REF)
    assert "查不到" in result.answer
    assert "9999" not in result.answer


def test_found_answer_gets_lineage_appended_if_model_omits(store):
    """模型最终文本没带来源时，编排层补上血缘。"""
    provider = ScriptedProvider([
        _tool_use_response({"metric": "REVENUE", "entity": "ACME_HK", "period": "FY2025"}),
        _text_response("香港 FY2025 REVENUE 为 1702 百万美元。"),  # 无来源
    ])
    result = answer_question("香港2025年REVENUE多少", store, provider, reference_date=REF)
    assert "1702" in result.answer
    assert "ACME_FY2025_Results.pptx" in result.answer
    assert "slide=5,table=1,row=2,col=3" in result.answer


def test_tool_executor_resolves_relative_period(store):
    """真实模型可能直接把"去年"塞进 period 参数：执行器按 reference_date 解析。"""
    provider = ScriptedProvider([
        _tool_use_response({"metric": "REVENUE", "entity": "ACME_HK", "period": "去年"}),
        _text_response("香港去年 REVENUE 为 1702 百万美元。"),
    ])
    result = answer_question("香港去年REVENUE多少", store, provider, reference_date=REF)
    assert result.tool_results[0]["status"] == "found"
    assert result.tool_results[0]["period"] == "2025"


def test_tool_executor_unrecognized_param_state(store):
    """真实模型把无法归一的指标塞进工具参数 → unrecognized 态，回答明确说无法识别。"""
    provider = ScriptedProvider([
        _tool_use_response({"metric": "净现金流量", "entity": "ACME_HK", "period": "FY2025"}),
        _text_response("净现金流量为 888 百万美元。"),  # 对抗：编造
    ])
    # 问题本身能过澄清网关（REVENUE 可识别），但模型自作主张查了别的指标
    result = answer_question("香港2025年REVENUE多少", store, provider, reference_date=REF)
    assert result.tool_results[0]["status"] == "unrecognized_param"
    assert "无法识别" in result.answer
    assert "888" not in result.answer


def test_structured_unrecognized_param(store):
    result = answer_question(
        "香港去年净现金流量多少啊到底", store, MockProvider(reference_date=REF),
        reference_date=REF,
    )
    # 识别不出指标 → 前置澄清，不乱查
    assert result.clarification is not None
    assert result.clarification.mode == CLARIFY_ASK_FIRST


# ---------------------------------------------------------------------------
# 澄清网关接线
# ---------------------------------------------------------------------------

def test_ask_first_returns_without_llm_call(store):
    """指标缺失 → 直接返回前置单选问题，绝不调用 provider。"""
    result = answer_question(
        "香港去年多少", store, SentinelProvider(), reference_date=REF
    )
    assert result.clarification.mode == CLARIFY_ASK_FIRST
    assert result.answer  # 反问文本即回答
    assert "REVENUE" in result.answer
    assert result.tool_results == []


def test_assumed_slots_answer_with_note(store):
    """缺实体/期间 → 默认先答 + 暴露假设 + 收窄选项（docs/02 §2 原则）。"""
    fact = Fact(
        metric_code="REVENUE", entity="ACME_GROUP", geography="ASIA", channel="TOTAL",
        period_type="FY", period="2025", value=4500.0, unit="USD_M",
        source_doc_id="ACME_FY2025_Results.pptx", source_locator="slide=3,table=1",
    )
    store.upsert_facts([fact])
    result = answer_question(
        "REVENUE多少", store, MockProvider(reference_date=REF), reference_date=REF
    )
    assert result.clarification.mode == CLARIFY_ANSWER_WITH_ASSUMPTIONS
    assert "4500" in result.answer  # 先答
    assert "假设" in result.answer  # 暴露假设
    assert result.clarification.narrowing_options  # 一键收窄


# ---------------------------------------------------------------------------
# narrative / composite
# ---------------------------------------------------------------------------

def test_narrative_route_uses_injected_retriever(store):
    retriever = FakeRetriever()
    result = answer_question(
        "香港最近有什么监管动态", store, MockProvider(reference_date=REF),
        reference_date=REF, narrative_retriever=retriever,
    )
    assert result.route == "narrative"
    assert len(retriever.calls) == 1
    assert retriever.calls[0]["top_k"] == 50
    assert {"doc": "HK_QBR_2025Q4.pptx", "locator": "slide=12"} in result.sources
    assert "HK_QBR_2025Q4.pptx" in result.answer


def test_narrative_without_retriever_degrades_honestly(store):
    result = answer_question(
        "香港最近有什么监管动态", store, SentinelProvider(),
        reference_date=REF, narrative_retriever=None,
    )
    assert result.route == "narrative"
    assert "未接入" in result.answer  # 坦白降级，不装答


def test_narrative_empty_retrieval_says_no_material(store):
    retriever = FakeRetriever(snippets=[])
    result = answer_question(
        "香港最近有什么监管动态", store, SentinelProvider(),
        reference_date=REF, narrative_retriever=retriever,
    )
    assert "未检索到" in result.answer


def test_composite_combines_number_and_narrative(store):
    retriever = FakeRetriever()
    result = answer_question(
        "香港去年REVENUE多少，为什么下降了", store, MockProvider(reference_date=REF),
        reference_date=REF, narrative_retriever=retriever,
    )
    assert result.route == "composite"
    assert "1702" in result.answer                      # 数字子任务
    assert "ACME_FY2025_Results.pptx" in result.answer   # 数字血缘
    assert "HK_QBR_2025Q4.pptx" in result.answer        # 归因子任务来源
    assert result.tool_results[0]["status"] == "found"
    docs = {s["doc"] for s in result.sources}
    assert {"ACME_FY2025_Results.pptx", "HK_QBR_2025Q4.pptx"} <= docs
