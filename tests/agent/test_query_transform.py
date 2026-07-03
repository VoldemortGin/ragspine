"""W9 查询变换（opt-in，默认关，字节不变）测试。

四个能力全部 opt-in、需注入 provider 才生效；未选用时 make_query_transform 返回 base 本身、
make_adaptive_decomposer 返回 None，主流程逐位字节不变。

安全/反编造继承（reverse-proof）：
- HyDE 的假想文档只作检索探针，绝不进答案/引用（真 chunk 才是结果）；
- RAG-Fusion / step-back 生成的每个变体/退一步问题都过确定性安全门——竞品变体被剔除、绝不检索
  （spy base 从未收到竞品 query，证明断言有牙）；
- 三个 wrapper 只对 base.retrieve(...) 的输出取舍/融合，隔离继承自 base 出口（RESTRICTED 永不出域）。
"""

import json
import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion, Choice, ProviderError, ResponseMessage

from ragspine.agent.query_transform import (
    COMPLEXITY_MULTI,
    COMPLEXITY_SIMPLE,
    COMPLEXITY_SINGLE,
    AdaptiveDecomposer,
    HeuristicComplexityClassifier,
    HyDERetriever,
    LLMComplexityClassifier,
    RAGFusionRetriever,
    StepBackRetriever,
    make_adaptive_decomposer,
    make_query_transform,
)
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.corrective import RESTRICTED_SENSITIVITY
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever

REF = date(2026, 6, 12)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

def _snip(cid: str) -> dict[str, object]:
    return {
        "chunk_id": cid,
        "text": f"chunk {cid} body",
        "doc_id": f"{cid}.pptx",
        "source_locator": f"loc-{cid}",
        "sensitivity": "INTERNAL",
    }


class FakeBase:
    """duck-typed NarrativeRetriever：按 query→chunk_id 列表返回片段，记录收到的 query。"""

    def __init__(self, mapping: dict[str, list[str]]):
        self.mapping = mapping
        self.seen: list[str] = []

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]:
        self.seen.append(query)
        return [_snip(cid) for cid in self.mapping.get(query, [])]


class ScriptedProvider:
    """按脚本依次吐响应的测试 provider。"""

    def __init__(self, responses: list[ChatCompletion]):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        return self._responses.pop(0)


class BoomProvider:
    """chat 一律抛 ProviderError（网络/API 故障模拟）。"""

    def chat(self, messages, *, tools=None):
        raise ProviderError("boom")


def _text_response(text: str) -> ChatCompletion:
    msg = ResponseMessage(role="assistant", content=text)
    return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="stop"),))


def _ids(snips: list[dict[str, object]]) -> list[object]:
    return [s["chunk_id"] for s in snips]


# ---------------------------------------------------------------------------
# HyDE：用假想文档检索；假想文档绝不进结果
# ---------------------------------------------------------------------------

HYPO = "假设答案：香港FY2025收入约为一千七百亿。FABRICATED_MARKER 9999"


def test_hyde_retrieves_with_hypothetical_document():
    base = FakeBase({HYPO: ["A", "B"]})
    hyde = HyDERetriever(base, ScriptedProvider([_text_response(HYPO)]))
    out = hyde.retrieve("香港FY2025REVENUE多少")
    # 用假想文档（而非原 query）作检索探针
    assert base.seen == [HYPO]
    assert _ids(out) == ["A", "B"]


def test_hyde_hypothetical_document_never_enters_snippets():
    """假想文档只作检索探针——其编造内容绝不出现在返回片段（真 chunk 才是结果）。"""
    base = FakeBase({HYPO: ["A"]})
    hyde = HyDERetriever(base, ScriptedProvider([_text_response(HYPO)]))
    out = hyde.retrieve("香港FY2025REVENUE多少")
    assert out
    assert all("FABRICATED_MARKER" not in str(s.get("text", "")) for s in out)
    assert all("9999" not in str(s.get("text", "")) for s in out)


def test_hyde_degrades_to_original_query_on_provider_error():
    """provider 故障 → 回退用原 query 检索（诚实降级，不崩、不编造）。"""
    base = FakeBase({"原问句": ["A"]})
    hyde = HyDERetriever(base, BoomProvider())
    out = hyde.retrieve("原问句")
    assert base.seen == ["原问句"]
    assert _ids(out) == ["A"]


# ---------------------------------------------------------------------------
# RAG-Fusion：LLM 生成 N 变体 → 各自检索 → RRF 融合
# ---------------------------------------------------------------------------

def test_rag_fusion_generates_variants_and_rrf_fuses():
    orig = "香港REVENUE趋势"
    base = FakeBase({
        orig: ["A", "B", "C"],
        "香港营收变化": ["B", "C", "D"],
        "香港收入走势": ["C", "D", "A"],
    })
    provider = ScriptedProvider([_text_response('["香港营收变化", "香港收入走势"]')])
    fusion = RAGFusionRetriever(base, provider)
    out = fusion.retrieve(orig)
    # C 在三个 query 里都靠前 → RRF 后升到榜首；D 只在变体里出现，也被融合进结果。
    assert _ids(out) == ["C", "B", "A", "D"]
    # 原 query + 两个变体都被检索（none 被安全门剔除）。
    assert set(base.seen) == {orig, "香港营收变化", "香港收入走势"}


def test_rag_fusion_degrades_to_single_on_provider_error():
    orig = "香港REVENUE趋势"
    base = FakeBase({orig: ["A", "B"]})
    fusion = RAGFusionRetriever(base, BoomProvider())
    out = fusion.retrieve(orig)
    assert base.seen == [orig]
    assert _ids(out) == ["A", "B"]


def test_rag_fusion_competitor_variant_is_screened_out():
    """安全门继承（reverse-proof）：竞品变体被剔除、绝不检索；home 变体照常。"""
    orig = "香港REVENUE趋势"
    base = FakeBase({
        orig: ["A"],
        "香港营收变化": ["B"],
        "竞安FY2025REVENUE多少": ["X"],  # 竞品变体——若未剔除会被检索
    })
    provider = ScriptedProvider([_text_response('["香港营收变化", "竞安FY2025REVENUE多少"]')])
    fusion = RAGFusionRetriever(base, provider)
    out = fusion.retrieve(orig)
    # 竞品变体从未到达 base（被确定性安全门剔除）——牙齿：X 在 mapping 里但绝不出现。
    assert "竞安FY2025REVENUE多少" not in base.seen
    assert "X" not in _ids(out)
    # home 变体照常检索、融合。
    assert "香港营收变化" in base.seen
    assert set(_ids(out)) == {"A", "B"}


# ---------------------------------------------------------------------------
# step-back：LLM 生成更抽象的退一步问题，原 + 退一步都检索再合并
# ---------------------------------------------------------------------------

def test_step_back_generates_abstract_question_and_merges():
    orig = "香港2025年上半年新单增长几何"
    stepback = "香港近年整体业务表现如何"
    base = FakeBase({orig: ["A", "B"], stepback: ["B", "C"]})
    provider = ScriptedProvider([_text_response(stepback)])
    sb = StepBackRetriever(base, provider)
    out = sb.retrieve(orig)
    assert set(base.seen) == {orig, stepback}
    # 合并了具体（A,B）与更宽背景（B,C）；B 两处命中应靠前。
    assert set(_ids(out)) == {"A", "B", "C"}
    assert out[0]["chunk_id"] == "B"


def test_step_back_degrades_on_provider_error():
    orig = "香港新单增长几何"
    base = FakeBase({orig: ["A"]})
    sb = StepBackRetriever(base, BoomProvider())
    out = sb.retrieve(orig)
    assert base.seen == [orig]
    assert _ids(out) == ["A"]


def test_step_back_competitor_question_is_screened_out():
    """退一步问题若是竞品越权，则剔除、仅用原 query（安全门继承）。"""
    orig = "香港新单增长几何"
    base = FakeBase({orig: ["A"], "竞安整体表现如何": ["X"]})
    provider = ScriptedProvider([_text_response("竞安整体表现如何")])
    sb = StepBackRetriever(base, provider)
    out = sb.retrieve(orig)
    assert "竞安整体表现如何" not in base.seen
    assert _ids(out) == ["A"]


# ---------------------------------------------------------------------------
# Adaptive-RAG：复杂度分类 + 路由（确定性启发式默认 + opt-in LLM）
# ---------------------------------------------------------------------------

def test_heuristic_classifier_simple_single_multi():
    clf = HeuristicComplexityClassifier()
    # 纯查数单槽位 → simple（可直接结构化，无需检索）
    assert clf.classify("香港去年REVENUE多少", reference_date=REF) == COMPLEXITY_SIMPLE
    # 单一叙事问题 → single（单跳检索）
    assert clf.classify("为什么香港REVENUE下降", reference_date=REF) == COMPLEXITY_SINGLE
    # 多实体对比 → multi（多跳，交给分解）
    assert clf.classify("对比香港和中国的REVENUE", reference_date=REF) == COMPLEXITY_MULTI


class FakeInner:
    """记录是否被调用的内层 decomposer。"""

    def __init__(self, subs: list[str]):
        self.subs = subs
        self.called = False

    def decompose(self, question: str, *, reference_date: date | None = None) -> list[str]:
        self.called = True
        return list(self.subs)


class FixedClassifier:
    def __init__(self, label: str):
        self.label = label

    def classify(self, question: str, *, reference_date: date | None = None) -> str:
        return self.label


def test_adaptive_decomposer_multi_delegates_to_inner():
    inner = FakeInner(["子问题1", "子问题2"])
    ad = AdaptiveDecomposer(FixedClassifier(COMPLEXITY_MULTI), inner)
    assert ad.decompose("复杂问题", reference_date=REF) == ["子问题1", "子问题2"]
    assert inner.called


def test_adaptive_decomposer_simple_single_no_fanout():
    """simple / single → 返回单元素表（不 fan-out，主流程正常单发路由）。"""
    inner = FakeInner(["不该被用"])
    for label in (COMPLEXITY_SIMPLE, COMPLEXITY_SINGLE):
        inner.called = False
        ad = AdaptiveDecomposer(FixedClassifier(label), inner)
        assert ad.decompose("问题", reference_date=REF) == ["问题"]
        assert not inner.called


def test_adaptive_decomposer_multi_without_inner_no_fanout():
    """multi 但未注入内层分解器（无 provider）→ 诚实回退单元素表。"""
    ad = AdaptiveDecomposer(FixedClassifier(COMPLEXITY_MULTI), None)
    assert ad.decompose("问题", reference_date=REF) == ["问题"]


def test_llm_complexity_classifier_parses_and_degrades():
    clf = LLMComplexityClassifier(ScriptedProvider([_text_response("multi")]))
    assert clf.classify("x", reference_date=REF) == COMPLEXITY_MULTI
    # provider 故障 → 确定性回退启发式
    boom = LLMComplexityClassifier(BoomProvider())
    assert boom.classify("香港去年REVENUE多少", reference_date=REF) == COMPLEXITY_SIMPLE


# ---------------------------------------------------------------------------
# make_query_transform 工厂 + env 选型（默认 none = 返回 base 本身、字节不变）
# ---------------------------------------------------------------------------

def test_make_query_transform_none_returns_base_identity():
    base = FakeBase({})
    assert make_query_transform(base, None) is base
    assert make_query_transform(base, "none") is base


def test_make_query_transform_builds_wrappers():
    base = FakeBase({})
    p = ScriptedProvider([])
    assert isinstance(make_query_transform(base, "hyde", provider=p), HyDERetriever)
    assert isinstance(make_query_transform(base, "rag_fusion", provider=p), RAGFusionRetriever)
    assert isinstance(make_query_transform(base, "fusion", provider=p), RAGFusionRetriever)
    assert isinstance(make_query_transform(base, "step_back", provider=p), StepBackRetriever)


def test_make_query_transform_needs_provider_degrades_to_base():
    """选了 LLM 变换但未注入 provider → 诚实回退 base（不空跑）。"""
    base = FakeBase({})
    assert make_query_transform(base, "hyde", provider=None) is base
    assert make_query_transform(base, "rag_fusion", provider=None) is base


def test_make_query_transform_env(monkeypatch):
    base = FakeBase({})
    p = ScriptedProvider([])
    monkeypatch.setenv("RAGSPINE_QUERY_TRANSFORM", "hyde")
    assert isinstance(make_query_transform(base, provider=p), HyDERetriever)
    monkeypatch.setenv("RAGSPINE_QUERY_TRANSFORM", "none")
    assert make_query_transform(base, provider=p) is base


def test_make_query_transform_unknown_raises():
    with pytest.raises(ValueError):
        make_query_transform(FakeBase({}), "nope", provider=ScriptedProvider([]))


def test_make_adaptive_decomposer():
    from ragspine.agent.llm_provider import MockProvider

    assert make_adaptive_decomposer(None) is None
    assert make_adaptive_decomposer("none") is None
    ad = make_adaptive_decomposer("heuristic", provider=MockProvider())
    assert isinstance(ad, AdaptiveDecomposer)
    # llm 无 provider → None（诚实降级）
    assert make_adaptive_decomposer("llm", provider=None) is None
    with pytest.raises(ValueError):
        make_adaptive_decomposer("nope")


def test_make_adaptive_decomposer_env(monkeypatch):
    from ragspine.agent.llm_provider import MockProvider

    monkeypatch.setenv("RAGSPINE_ADAPTIVE", "heuristic")
    assert isinstance(make_adaptive_decomposer(provider=MockProvider()), AdaptiveDecomposer)
    monkeypatch.setenv("RAGSPINE_ADAPTIVE", "none")
    assert make_adaptive_decomposer(provider=MockProvider()) is None


# ---------------------------------------------------------------------------
# 隔离 conformance：wrapper 的 RESTRICTED 隔离【继承】自被包裹的 base（真索引集成 + reverse-proof）
# ---------------------------------------------------------------------------

NORMAL_TEXT = "香港 REVENUE 下降 MCV 客群 收缩 与 银保 渠道 调整。"
SECRET_TEXT = "香港 REVENUE 下降 背后 的 高管 PR 评级 SECRET_TOKEN 讨论。"
QUERY = "香港 REVENUE 下降 MCV 客群 收缩"


def test_query_transform_inherits_restricted_isolation_from_base(tmp_path):
    """真索引集成：RAGFusionRetriever 输出无任何 RESTRICTED 块（隔离继承自 base 出口）。"""
    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    try:
        index = NarrativeIndex(store)
        index.ingest(NORMAL_TEXT, DocumentMeta(doc_id="HK_QBR.pptx", entity="ACME_HK"))
        index.ingest(
            SECRET_TEXT,
            DocumentMeta(doc_id="EXCO.pptx", entity="ACME_HK", sensitivity="RESTRICTED"),
        )
        base = NarrativeIndexRetriever(index)
        # 变体是 home query（会检索到普通块）；provider 生成一个 home 变体。
        provider = ScriptedProvider([_text_response('["香港 REVENUE 下降 客群"]')])
        fusion = RAGFusionRetriever(base, provider)
        out = fusion.retrieve(QUERY)

        assert out, "普通块应被召回（输出非空）"
        assert all(
            str(s.get("sensitivity")).upper() != RESTRICTED_SENSITIVITY for s in out
        )
        assert all("SECRET_TOKEN" not in str(s.get("text", "")) for s in out)
        assert all(s.get("doc_id") != "EXCO.pptx" for s in out)

        # reverse-proof：RESTRICTED 块确实在库中——输出干净是 base 剔除之功，而非数据缺失。
        stored = store.iter_chunks(doc_id="EXCO.pptx", include_inactive=True)
        assert stored
        assert any("SECRET_TOKEN" in c.text for c in stored)
        assert any(c.sensitivity.upper() == RESTRICTED_SENSITIVITY for c in stored)
    finally:
        store.close()
