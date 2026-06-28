"""W9 查询变换（opt-in，默认关）单测：HyDE / RAG-Fusion / step-back + RRF 融合 + 工厂选型 + 降级。

变换经注入的 transform 才生效，默认 None / 未注入 provider = 行为字节不变。隔离继承自 base
（只对 base.retrieve 输出融合取舍）。LLM 故障 / 解析失败一律降级为 [原查询]（普通检索）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion, Choice, ProviderError, ResponseMessage

from ragspine.agent.query_transform import (
    QUERY_TRANSFORM_ENV,
    HyDETransform,
    QueryTransformRetriever,
    RAGFusionTransform,
    StepBackTransform,
    make_query_transform,
    make_query_transform_retriever,
)


def _text_response(text: str) -> ChatCompletion:
    msg = ResponseMessage(role="assistant", content=text)
    return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="stop"),))


class ScriptedProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        return self._responses.pop(0)


class BoomProvider:
    def chat(self, messages, *, tools=None):
        raise ProviderError("boom")


class FakeBase:
    """按 query→snippets 脚本返回的 base NarrativeRetriever 替身（记录调用查询序）。"""

    def __init__(self, by_query):
        self.by_query = by_query
        self.queries = []

    def retrieve(self, query, *, filters=None, top_k=50):
        self.queries.append(query)
        return list(self.by_query.get(query, []))


def _snip(cid):
    return {"chunk_id": cid, "text": f"片段{cid}", "doc_id": f"{cid}.pdf"}


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(QUERY_TRANSFORM_ENV, raising=False)


# --- 各变换产出的查询表 ---


def test_hyde_returns_original_plus_hypothetical_doc():
    p = ScriptedProvider([_text_response("香港收入下降，主因银保渠道调整与 MCV 客群收缩。")])
    out = HyDETransform(p).transform("香港为什么收入下降")
    assert out[0] == "香港为什么收入下降"
    assert any("银保" in q for q in out)
    assert len(out) == 2


def test_hyde_pure_mode_drops_original():
    p = ScriptedProvider([_text_response("假设文档内容")])
    out = HyDETransform(p, include_original=False).transform("q")
    assert out == ["假设文档内容"]


def test_rag_fusion_parses_variants_and_dedups():
    p = ScriptedProvider([_text_response('["香港收入下滑", "HK revenue decline", "香港收入下滑"]')])
    out = RAGFusionTransform(p, n_variants=4).transform("香港收入下降")
    assert out[0] == "香港收入下降"  # 原问题恒在首位
    assert "香港收入下滑" in out and "HK revenue decline" in out
    assert len(out) == len(set(out)), "去重后无重复"


def test_rag_fusion_bounds_variants():
    p = ScriptedProvider([_text_response('["v1","v2","v3","v4","v5","v6"]')])
    out = RAGFusionTransform(p, n_variants=2).transform("q")
    # 原问题 + 至多 2 个变体。
    assert out[0] == "q"
    assert len(out) <= 3


def test_step_back_returns_original_plus_abstract():
    p = ScriptedProvider([_text_response("亚洲地区保险公司的收入驱动因素有哪些？")])
    out = StepBackTransform(p).transform("香港 ACME FY2024 收入为什么下降")
    assert out[0] == "香港 ACME FY2024 收入为什么下降"
    assert any("驱动因素" in q for q in out)


@pytest.mark.parametrize(
    "transform_cls", [HyDETransform, RAGFusionTransform, StepBackTransform]
)
def test_provider_error_degrades_to_original_query(transform_cls):
    """provider 故障 -> 回落 [原查询]（退化普通检索，不崩）。"""
    out = transform_cls(BoomProvider()).transform("原始问题")
    assert out == ["原始问题"]


def test_rag_fusion_bad_json_degrades():
    p = ScriptedProvider([_text_response("不是 JSON 数组")])
    assert RAGFusionTransform(p).transform("q") == ["q"]


# --- QueryTransformRetriever RRF 融合 ---


def test_single_query_is_identity_passthrough():
    base = FakeBase({"q": [_snip("a"), _snip("b")]})

    class OneQuery:
        def transform(self, query, *, reference_date=None):
            return [query]

    wrapped = QueryTransformRetriever(base, OneQuery())
    out = wrapped.retrieve("q", filters={"entity": "X"}, top_k=7)
    assert [s["chunk_id"] for s in out] == ["a", "b"]
    assert base.queries == ["q"]  # 单查询，未做多检索


def test_multi_query_rrf_fusion_ranks_shared_hits_higher():
    """两个查询都召回的片段，RRF 分更高，排到前面。"""
    base = FakeBase({
        "q1": [_snip("shared"), _snip("only1")],
        "q2": [_snip("only2"), _snip("shared")],
    })

    class TwoQueries:
        def transform(self, query, *, reference_date=None):
            return ["q1", "q2"]

    wrapped = QueryTransformRetriever(base, TwoQueries())
    out = wrapped.retrieve("orig")
    ids = [s["chunk_id"] for s in out]
    assert ids[0] == "shared", "两查询共命中的片段 RRF 分最高"
    assert set(ids) == {"shared", "only1", "only2"}
    assert base.queries == ["q1", "q2"]


def test_empty_query_list_returns_empty():
    base = FakeBase({})

    class NoQuery:
        def transform(self, query, *, reference_date=None):
            return []

    assert QueryTransformRetriever(base, NoQuery()).retrieve("q") == []


def test_retriever_deterministic():
    base = FakeBase({
        "q1": [_snip("a"), _snip("b")],
        "q2": [_snip("b"), _snip("c")],
    })

    class TwoQueries:
        def transform(self, query, *, reference_date=None):
            return ["q1", "q2"]

    w = QueryTransformRetriever(base, TwoQueries())
    a = [s["chunk_id"] for s in w.retrieve("o")]
    b = [s["chunk_id"] for s in w.retrieve("o")]
    assert a == b


# --- make_query_transform / make_query_transform_retriever 工厂 ---


@pytest.mark.parametrize("spec", [None, "none", "NONE"])
def test_make_none_returns_none(spec):
    assert make_query_transform(spec, provider=ScriptedProvider([])) is None


def test_make_resolves_each_transform():
    p = ScriptedProvider([])
    assert isinstance(make_query_transform("hyde", provider=p), HyDETransform)
    assert isinstance(make_query_transform("rag_fusion", provider=p), RAGFusionTransform)
    assert isinstance(make_query_transform("fusion", provider=p), RAGFusionTransform)
    assert isinstance(make_query_transform("step-back", provider=p), StepBackTransform)


def test_make_without_provider_degrades_to_none():
    """所有变换都 LLM 驱动：未注入 provider -> None（诚实降级为不变换）。"""
    assert make_query_transform("hyde", provider=None) is None
    assert make_query_transform("rag_fusion", provider=None) is None


def test_make_unknown_spec_raises():
    with pytest.raises(ValueError):
        make_query_transform("bogus", provider=ScriptedProvider([]))


def test_make_reads_env(monkeypatch):
    monkeypatch.setenv(QUERY_TRANSFORM_ENV, "hyde")
    assert isinstance(make_query_transform(provider=ScriptedProvider([])), HyDETransform)


def test_retriever_factory_none_returns_base_byte_identical():
    base = FakeBase({})
    assert make_query_transform_retriever(base, "none", provider=ScriptedProvider([])) is base
    # 未注入 provider 也回落 base 本身（字节不变）。
    assert make_query_transform_retriever(base, "hyde", provider=None) is base


def test_retriever_factory_wraps_when_selected():
    base = FakeBase({})
    wrapped = make_query_transform_retriever(base, "hyde", provider=ScriptedProvider([]))
    assert isinstance(wrapped, QueryTransformRetriever)
    assert wrapped.base is base
