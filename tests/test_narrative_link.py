"""A/B 两线集成测试（TDD 红色阶段）：src/narrative_link 适配层 + CLI 接线。

覆盖：
- NarrativeIndexRetriever：协议字段映射、top_k/filters 透传、未知/空 filter 丢弃、
  过滤为空时的去过滤重试（可关）、RESTRICTED 块绝不出适配器出口；
- ProviderListwiseJudge：fake provider 成功路径（prompt 构造 + 下标解析）、乱回文
  退化、provider 抛异常时经 listwise_rerank 退化为 RRF 序（绝不抛死）；
- composite / narrative 端到端：FactStore 数字 + ChunkStore 叙事 + MockProvider，
  最终回答同时含确定数值（带血缘）与叙事内容（带来源）；
- Restricted 端到端：敏感文本既不进任何发给 provider 的内容，也不进最终回答/来源；
- scripts/ask.py --chunk-db 接线：提供时 composite 走真实检索，不提供时行为不变。

全部 fake/mock 注入，零网络、零真实模型。
"""

import json
import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from scripts.ask import main as ask_main
from ragspine.agent.agent import answer_question
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.storage.fact_store import Fact, FactStore
from ragspine.agent.llm_provider import MockProvider, ProviderResponse
from ragspine.retrieval.link.narrative_link import (
    NarrativeIndexRetriever,
    ProviderListwiseJudge,
    build_narrative_retriever,
)
from ragspine.retrieval.lexical.retrieval import NarrativeIndex

REF = date(2026, 6, 12)

REVENUE_HK_FY2025 = Fact(
    metric_code="REVENUE", entity="ACME_HK", geography="HK", channel="TOTAL",
    period_type="FY", period="2025", value=1702.0, unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx", source_locator="slide=5,table=1,row=2,col=3",
)

NARRATIVE_DOC = DocumentMeta(
    doc_id="HK_QBR_2025Q4.pptx", title="HK QBR 2025Q4", topic="FIN",
    entity="ACME_HK", geography="HK", period="2025", language="zh",
)
NARRATIVE_TEXT = "香港 REVENUE 下降主因是 MCV 客群收缩与银保渠道调整。"


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class ScriptedProvider:
    """按脚本依次吐文本响应的 LLMProvider 替身，并记录全部调用。"""

    def __init__(self, texts: list[str]):
        self._texts = list(texts)
        self.calls: list[dict] = []

    def create_message(self, *, system, messages, tools):
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        text = self._texts.pop(0)
        return ProviderResponse(text=text, raw_content=[{"type": "text", "text": text}])


class RaisingProvider:
    """一调用就抛错的 LLMProvider 替身（模拟网络/网关故障）。"""

    def create_message(self, *, system, messages, tools):
        raise RuntimeError("provider down")


class RecordingProvider:
    """透传给内层 provider，同时记录所有进出文本（敏感泄漏断言用）。"""

    def __init__(self, inner):
        self.inner = inner
        self.seen: list[str] = []

    def create_message(self, *, system, messages, tools):
        self.seen.append(json.dumps([system, messages], ensure_ascii=False, default=str))
        return self.inner.create_message(system=system, messages=messages, tools=tools)


class SpyIndex:
    """duck-typed NarrativeIndex 替身：记录 retrieve 调用参数，返回预设结果。"""

    def __init__(self, results=None):
        self.results = results if results is not None else []
        self.calls: list[dict] = []

    def retrieve(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return self.results


def _build_index(tmp_path, *, judge=None) -> tuple[NarrativeIndex, ChunkStore]:
    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    return NarrativeIndex(store, judge=judge), store


@pytest.fixture
def fact_db(tmp_path):
    db_path = tmp_path / "fact_metric.db"
    fs = FactStore(db_path)
    fs.init_schema()
    fs.upsert_facts([REVENUE_HK_FY2025])
    fs.close()
    return db_path


# ===========================================================================
# 适配器：字段映射 / 透传 / 降级 / Restricted 出口拦截
# ===========================================================================

def test_adapter_maps_chunk_fields_to_protocol_snippets(tmp_path):
    """真实 NarrativeIndex（无向量后端=纯 BM25）→ A 线协议要求的 dict 字段。"""
    index, store = _build_index(tmp_path)
    try:
        index.ingest(NARRATIVE_TEXT, NARRATIVE_DOC)
        adapter = NarrativeIndexRetriever(index)
        snippets = adapter.retrieve("香港REVENUE为什么下降")
        assert snippets, "纯 BM25 模式应能召回"
        s = snippets[0]
        assert s["text"] == NARRATIVE_TEXT
        assert s["doc_id"] == "HK_QBR_2025Q4.pptx"
        assert s["title"] == "HK QBR 2025Q4"
        assert s["source_locator"] == "HK_QBR_2025Q4.pptx#para1"
        # 元数据与可解释得分
        assert s["entity"] == "ACME_HK"
        assert s["period"] == "2025"
        assert s["sensitivity"] == "INTERNAL"
        assert s["scores"]["bm25"] > 0.0
        assert s["scores"]["vector"] == 0.0  # 无向量通道：不许伪装有真实向量
    finally:
        store.close()


def test_adapter_passes_top_k_and_maps_known_filters():
    """filters dict → NarrativeIndex kwargs：只透传已知键，None/空值与未知键丢弃。"""
    spy = SpyIndex()
    adapter = NarrativeIndexRetriever(spy, retry_without_filters=False)
    adapter.retrieve(
        "q",
        filters={"entity": "ACME_HK", "period": "2025", "unknown": "x", "topic": None},
        top_k=7,
    )
    assert spy.calls == [
        {"query": "q", "entity": "ACME_HK", "period": "2025", "top_k": 7}
    ]


def test_adapter_default_top_k_is_50():
    spy = SpyIndex()
    NarrativeIndexRetriever(spy).retrieve("q")
    assert spy.calls[0]["top_k"] == 50


def test_adapter_metadata_filter_narrows_results(tmp_path):
    index, store = _build_index(tmp_path)
    try:
        index.ingest(NARRATIVE_TEXT, NARRATIVE_DOC)
        index.ingest(
            "中国 REVENUE 下降与银保渠道调整相关。",
            DocumentMeta(doc_id="CN_QBR.pptx", entity="ACME_CN", period="2025"),
        )
        adapter = NarrativeIndexRetriever(index)
        snippets = adapter.retrieve("REVENUE 下降 银保", filters={"entity": "ACME_CN"})
        assert snippets
        assert {s["doc_id"] for s in snippets} == {"CN_QBR.pptx"}
    finally:
        store.close()


def test_adapter_retries_without_filters_when_empty(tmp_path):
    """过滤条件把候选全滤光时，默认去过滤重试一次（召回优先，可解释）。"""
    index, store = _build_index(tmp_path)
    try:
        index.ingest(NARRATIVE_TEXT, NARRATIVE_DOC)  # entity=ACME_HK
        adapter = NarrativeIndexRetriever(index)
        snippets = adapter.retrieve(
            "香港REVENUE为什么下降", filters={"entity": "ACME_TH"}
        )
        assert snippets  # 回退后召回
        assert snippets[0]["doc_id"] == "HK_QBR_2025Q4.pptx"
    finally:
        store.close()


def test_adapter_strict_mode_keeps_empty_on_filter_miss(tmp_path):
    index, store = _build_index(tmp_path)
    try:
        index.ingest(NARRATIVE_TEXT, NARRATIVE_DOC)
        adapter = NarrativeIndexRetriever(index, retry_without_filters=False)
        assert adapter.retrieve("香港REVENUE为什么下降",
                                filters={"entity": "ACME_TH"}) == []
    finally:
        store.close()


def test_adapter_drops_restricted_chunks_at_egress(tmp_path):
    """RESTRICTED 块绝不出适配器出口：下游 A 线会把 snippet 文本发给 LLM。"""
    index, store = _build_index(tmp_path)
    try:
        index.ingest(NARRATIVE_TEXT, NARRATIVE_DOC)
        index.ingest(
            "香港 REVENUE 下降的高管 PR 评级讨论 SECRET_TOKEN。",
            DocumentMeta(doc_id="EXCO_MINUTES.pptx", entity="ACME_HK",
                         sensitivity="Restricted"),  # 大小写不敏感
        )
        adapter = NarrativeIndexRetriever(index)
        snippets = adapter.retrieve("香港REVENUE为什么下降")
        assert snippets
        assert all(s["doc_id"] != "EXCO_MINUTES.pptx" for s in snippets)
        assert all("SECRET_TOKEN" not in s["text"] for s in snippets)
    finally:
        store.close()


# ===========================================================================
# ProviderListwiseJudge：成功 / 乱回文退化 / 异常退化
# ===========================================================================

def test_judge_builds_prompt_and_parses_order():
    provider = ScriptedProvider(["2, 0, 1"])
    judge = ProviderListwiseJudge(provider)
    order = judge.judge("查询X", ["候选A", "候选B", "候选C"])
    assert order == [2, 0, 1]
    call = provider.calls[0]
    assert call["tools"] == []  # 精排不带工具
    prompt = call["messages"][0]["content"]
    assert "查询X" in prompt
    assert "[0] 候选A" in prompt and "[2] 候选C" in prompt


def test_judge_garbage_response_degrades_to_identity():
    judge = ProviderListwiseJudge(ScriptedProvider(["完全不是下标的回文"]))
    assert judge.judge("q", ["a", "b", "c"]) == [0, 1, 2]


def test_judge_exception_degrades_to_rrf_order(tmp_path):
    """provider 抛错 → listwise_rerank 的 B 线退化语义生效：返回 RRF 序，绝不抛死。"""
    index, store = _build_index(
        tmp_path, judge=ProviderListwiseJudge(RaisingProvider())
    )
    try:
        index.ingest(NARRATIVE_TEXT, NARRATIVE_DOC)
        snippets = NarrativeIndexRetriever(index).retrieve("香港REVENUE为什么下降")
        assert snippets  # 不抛、不空
        assert snippets[0]["doc_id"] == "HK_QBR_2025Q4.pptx"
    finally:
        store.close()


def test_judge_reorders_index_results(tmp_path):
    """judge 倒排两个候选 → retrieve 出口顺序随之颠倒（二审真实生效）。"""
    docs = [
        ("D1.pptx", "香港 REVENUE 下降 银保 渠道 调整 香港 REVENUE"),
        ("D2.pptx", "香港 REVENUE 下降 一笔带过"),
    ]
    # 第一次召回为 RRF 序；脚本让 judge 永远把第 2 个候选排第一
    provider = ScriptedProvider(["1, 0"])
    index, store = _build_index(tmp_path, judge=ProviderListwiseJudge(provider))
    try:
        for doc_id, text in docs:
            index.ingest(text, DocumentMeta(doc_id=doc_id, entity="ACME_HK"))
        # 同一 index 先用 rerank=False 取 RRF 基线序（不消耗 judge 脚本）
        rrf_order = [
            r.chunk.doc_id
            for r in index.retrieve("香港REVENUE为什么下降", rerank=False)
        ]
        reranked = NarrativeIndexRetriever(index).retrieve("香港REVENUE为什么下降")
        assert [s["doc_id"] for s in reranked] == list(reversed(rrf_order))
    finally:
        store.close()


# ===========================================================================
# 端到端：composite / narrative / Restricted 不出域
# ===========================================================================

def test_composite_end_to_end_number_with_lineage_and_narrative(tmp_path, fact_db):
    """数字（确定值+血缘）与叙事（原文+来源）在同一回答中端到端汇合。"""
    index, chunk_store = _build_index(tmp_path)
    fs = FactStore(fact_db)
    try:
        index.ingest(NARRATIVE_TEXT, NARRATIVE_DOC)
        result = answer_question(
            "香港去年REVENUE多少，为什么下降了", fs, MockProvider(reference_date=REF),
            reference_date=REF,
            narrative_retriever=NarrativeIndexRetriever(index),
        )
        assert result.route == "composite"
        # 数字子任务：确定值 + 血缘
        assert "1702" in result.answer
        assert "ACME_FY2025_Results.pptx" in result.answer
        assert result.tool_results[0]["status"] == "found"
        # 叙事子任务：检索原文 + 来源
        assert "MCV" in result.answer
        assert "HK_QBR_2025Q4.pptx" in result.answer
        docs = {s["doc"] for s in result.sources}
        assert {"ACME_FY2025_Results.pptx", "HK_QBR_2025Q4.pptx"} <= docs
    finally:
        chunk_store.close()
        fs.close()


def test_narrative_end_to_end_with_real_index(tmp_path, fact_db):
    index, chunk_store = _build_index(tmp_path)
    fs = FactStore(fact_db)
    try:
        index.ingest(
            "香港监管动态：MPFA 强积金新规要求披露管理费。",
            DocumentMeta(doc_id="REG_WATCH.pptx", entity="ACME_HK", topic="REG"),
        )
        result = answer_question(
            "香港最近有什么监管动态", fs, MockProvider(reference_date=REF),
            reference_date=REF,
            narrative_retriever=NarrativeIndexRetriever(index),
        )
        assert result.route == "narrative"
        assert "MPFA" in result.answer
        assert "REG_WATCH.pptx" in result.answer
        assert {"doc": "REG_WATCH.pptx", "locator": "REG_WATCH.pptx#para1"} in [
            {"doc": s["doc"], "locator": s["locator"]} for s in result.sources
        ]
    finally:
        chunk_store.close()
        fs.close()


def test_restricted_text_never_reaches_provider_nor_answer(tmp_path, fact_db):
    """集成链路下 Restricted 仍不出域：judge prompt、合成 prompt、回答、来源全无。"""
    recorder = RecordingProvider(MockProvider(reference_date=REF))
    index, chunk_store = _build_index(
        tmp_path, judge=ProviderListwiseJudge(recorder)
    )
    fs = FactStore(fact_db)
    try:
        index.ingest(
            "香港监管动态：MPFA 强积金新规要求披露管理费。",
            DocumentMeta(doc_id="REG_WATCH.pptx", entity="ACME_HK"),
        )
        index.ingest(
            "香港监管动态背后的高管 PR 评级 SECRET_TOKEN 讨论。",
            DocumentMeta(doc_id="EXCO_MINUTES.pptx", entity="ACME_HK",
                         sensitivity="RESTRICTED"),
        )
        result = answer_question(
            "香港最近有什么监管动态", fs, recorder,
            reference_date=REF,
            narrative_retriever=NarrativeIndexRetriever(index),
        )
        assert "SECRET_TOKEN" not in result.answer
        assert all(s["doc"] != "EXCO_MINUTES.pptx" for s in result.sources)
        assert all("SECRET_TOKEN" not in seen for seen in recorder.seen)
        assert "MPFA" in result.answer  # 非敏感内容照常回答
    finally:
        chunk_store.close()
        fs.close()


# ===========================================================================
# CLI 接线：--chunk-db
# ===========================================================================

def _seed_chunk_db(path) -> None:
    store = ChunkStore(path)
    store.init_schema()
    NarrativeIndex(store).ingest(NARRATIVE_TEXT, NARRATIVE_DOC)
    store.close()


def test_ask_cli_with_chunk_db_runs_composite(tmp_path, fact_db, capsys):
    chunk_db = tmp_path / "chunks.db"
    _seed_chunk_db(chunk_db)
    rc = ask_main([
        "--provider", "mock",
        "--db", str(fact_db),
        "--chunk-db", str(chunk_db),
        "--reference-date", "2026-06-12",
        "香港去年REVENUE多少，为什么下降了",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1702" in out
    assert "ACME_FY2025_Results.pptx" in out
    assert "MCV" in out
    assert "HK_QBR_2025Q4.pptx" in out


def test_ask_cli_without_chunk_db_keeps_degradation(fact_db, capsys):
    """不给 --chunk-db：narrative 路保持既有坦白降级行为。"""
    rc = ask_main([
        "--provider", "mock",
        "--db", str(fact_db),
        "--reference-date", "2026-06-12",
        "香港最近有什么监管动态",
    ])
    assert rc == 0
    assert "未接入" in capsys.readouterr().out


def test_build_narrative_retriever_factory(tmp_path):
    """工厂：开库+组装默认链（纯 BM25、glossary 改写、provider judge）。"""
    chunk_db = tmp_path / "chunks.db"
    _seed_chunk_db(chunk_db)
    retriever, store = build_narrative_retriever(
        chunk_db, provider=MockProvider(reference_date=REF)
    )
    try:
        snippets = retriever.retrieve("香港REVENUE为什么下降")
        assert snippets and snippets[0]["doc_id"] == "HK_QBR_2025Q4.pptx"
    finally:
        store.close()
