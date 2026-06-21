"""HybridRetriever 接入 VectorStore 缝（见 src/ragspine/retrieval/docs/vector-store.md 的「wiring」步）。

把向量打分从内联 cosine 暴力扫委托给可注入的 VectorStore（默认 InProcessVectorStore）。
核心断言：委托后结果与原内联实现【逐位等价】——store.query 按 (-score, id) 排序、cosine
口径一致，注入与否结果完全相同；同时 store 被真实填充与查询（缝是活的，不是死脚手架）。

byte-identity 锁：test_byte_identity_golden 钉死一组从【改造前内联实现】抓取的精确三元组
（DeterministicEmbeddingBackend + GlossaryQueryRewriter），任何打分/排序漂移立刻红。
前置约定：chunk_id 唯一（与既有 by_id={c.chunk_id:c} 假设一致；重复 id 不在支持范围）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.lexical.retrieval import GlossaryQueryRewriter, HybridRetriever
from ragspine.retrieval.vector.embedding_backends import DeterministicEmbeddingBackend
from ragspine.retrieval.vector.store import InProcessVectorStore, VectorStore


class FakeEmbeddingBackend:
    """确定性 fake：字符 crc32 词袋向量（与 PYTHONHASHSEED 无关），口径同 test_retrieval。"""

    DIM = 64

    def __init__(self):
        self.embedded: list[str] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import zlib

        self.embedded.extend(texts)
        out = []
        for t in texts:
            vec = [0.0] * self.DIM
            for ch in t.lower():
                if not ch.isspace():
                    vec[zlib.crc32(ch.encode("utf-8")) % self.DIM] += 1.0
            out.append(vec)
        return out


def _chunk(chunk_id: str, text: str, **overrides) -> Chunk:
    kwargs = dict(
        chunk_id=chunk_id,
        doc_id=chunk_id.split("#")[0],
        seq=0,
        text=text,
        source_locator=f"{chunk_id}!para1",
        para_start=1,
        para_end=1,
        topic="FIN",
        entity="ACME_HK",
        geography="HK",
        period="2025H1",
        language="zh",
        sensitivity="INTERNAL",
    )
    kwargs.update(overrides)
    return Chunk(**kwargs)


@pytest.fixture
def corpus() -> list[Chunk]:
    return [
        _chunk("c1#0", "Nexora 事件对银保渠道的影响评估", topic="REG", entity="ACME_CN", geography="CN"),
        _chunk("c2#0", "MPFA 强积金新规要求披露管理费", topic="REG", entity="ACME_HK"),
        _chunk("c3#0", "CPL 渠道归因分析显示银保增长", topic="FIN", entity="ACME_CN", geography="CN"),
        _chunk("c4#0", "香港 REVENUE 营收持续增长", topic="FIN", entity="ACME_HK"),
        _chunk("c5#0", "weekend cricket match report 板球比赛", topic="OTHER", entity="NONE"),
    ]


def _triples(results):
    return [(r.chunk.chunk_id, r.vector_score, r.fused_score) for r in results]


# ---------------------------------------------------------------------------
# 缝是活的：注入的 store 被真实填充并查询
# ---------------------------------------------------------------------------

def test_injected_store_is_populated_and_queried(corpus):
    """注入一个 InProcessVectorStore：search 后它装入了候选向量且参与了打分。"""
    store = InProcessVectorStore()
    retriever = HybridRetriever(corpus, embedding_backend=FakeEmbeddingBackend(), vector_store=store)
    results = retriever.search("香港 REVENUE 营收持续增长")
    assert store.count() == len(corpus)
    assert results[0].chunk.chunk_id == "c4#0"
    assert results[0].vector_score == pytest.approx(1.0, abs=1e-9)


def test_default_store_is_in_process_when_embedding_present(corpus):
    """未注入 store 但有 embedding 后端：检索器自建零依赖 InProcessVectorStore 默认。"""
    retriever = HybridRetriever(corpus, embedding_backend=FakeEmbeddingBackend())
    assert isinstance(retriever.vector_store, InProcessVectorStore)
    assert isinstance(retriever.vector_store, VectorStore)


def test_no_store_when_pure_bm25(corpus):
    """纯 BM25（无 embedding 后端）：vector_store 保持 None（无向量通道）。"""
    retriever = HybridRetriever(corpus)
    assert retriever.vector_store is None


# ---------------------------------------------------------------------------
# 逐位等价
# ---------------------------------------------------------------------------

def test_injected_vs_default_store_identical(corpus):
    """注入显式 store 与用内置默认，检索结果（id/vector/fused）逐位一致。"""
    default = HybridRetriever(corpus, embedding_backend=FakeEmbeddingBackend())
    injected = HybridRetriever(
        corpus, embedding_backend=FakeEmbeddingBackend(), vector_store=InProcessVectorStore()
    )
    q = "银保 渠道 增长"
    assert _triples(default.search(q)) == _triples(injected.search(q))


def test_two_searches_deterministic(corpus):
    """同一检索器连查两次：结果逐位一致（确定性，store 复用不串味）。"""
    retriever = HybridRetriever(corpus, embedding_backend=FakeEmbeddingBackend())
    q = "香港 REVENUE 营收"
    assert _triples(retriever.search(q)) == _triples(retriever.search(q))


def test_byte_identity_golden(corpus):
    """改造前内联实现抓取的精确三元组（DeterministicEmbeddingBackend dim=32 + glossary 改写）。

    委托给 VectorStore 后必须逐位复现——任何 cosine 口径 / 排序 / RRF 漂移立刻红。
    """
    # 形如改造前 capture：(chunk_id, bm25_score, vector_score, fused_score)
    golden = [
        ("c4#0", 12.236496213582912, 0.7745966692414833, 0.1631411951348493),
        ("c3#0", 4.770858364820141, 0.5619514869490166, 0.16182753729025545),
        ("c1#0", 2.3854291824100704, 0.5677749739576691, 0.15898617511520735),
        ("c2#0", 0.0, 0.319504825211347, 0.078125),
        ("c5#0", 0.0, 0.16724840200141816, 0.07692307692307693),
    ]
    retriever = HybridRetriever(
        corpus,
        embedding_backend=DeterministicEmbeddingBackend(dim=32),
        query_rewriter=GlossaryQueryRewriter(),
    )
    got = [
        (r.chunk.chunk_id, r.bm25_score, r.vector_score, r.fused_score)
        for r in retriever.search("香港 REVENUE 银保 增长")
    ]
    assert [g[0] for g in got] == [g[0] for g in golden]  # 顺序
    for (gid, gb, gv, gf), (rid, rb, rv, rf) in zip(golden, got, strict=True):
        assert rid == gid
        assert rb == pytest.approx(gb, abs=1e-12)
        assert rv == pytest.approx(gv, abs=1e-12)
        assert rf == pytest.approx(gf, abs=1e-12)


def test_prefilter_still_blocks_embedding_under_delegation(corpus):
    """委托后仍守「预过滤先于打分」：被过滤块文本绝不进 embed，也不入 store。"""
    backend = FakeEmbeddingBackend()
    store = InProcessVectorStore()
    retriever = HybridRetriever(corpus, embedding_backend=backend, vector_store=store)
    results = retriever.search("Nexora 事件", topic="FIN")
    assert all(r.chunk.topic == "FIN" for r in results)
    assert "c1#0" not in {r.chunk.chunk_id for r in results}
    assert corpus[0].text not in backend.embedded
    # 只有 FIN 候选（c3/c4）入向量库。
    assert store.count() == 2
    assert {h.id for h in store.query([0.0] * FakeEmbeddingBackend.DIM, k=99)} == {"c3#0", "c4#0"}


def test_empty_string_dim_filter_parity():
    """where 与 python 预过滤口径一致：空串维度在 dim=None / dim='' 下行为各自正确。

    内联预过滤用 `if val is not None`，故 '' 是真实过滤值（不跳过）；store 路径须复刻。
    golden（改造前内联）：topic=None -> [e2#0, e1#0]；topic='' -> [e1#0]。
    """
    corpus = [_chunk("e1#0", "营收 增长 数据", topic=""), _chunk("e2#0", "营收 下滑", topic="FIN")]
    backend = DeterministicEmbeddingBackend(dim=32)
    none_ids = [r.chunk.chunk_id for r in HybridRetriever(corpus, embedding_backend=backend).search("营收", topic=None)]
    empty_ids = [r.chunk.chunk_id for r in HybridRetriever(corpus, embedding_backend=backend).search("营收", topic="")]
    assert none_ids == ["e2#0", "e1#0"]
    assert empty_ids == ["e1#0"]


def test_k_covers_all_candidates_above_default_query_k():
    """向量查询 k=len(candidates)（非 store 默认 50）：60 个候选全部拿到向量分（>50 不截断）。

    若向量查询误用默认 k=50，第 51–60 个候选会从向量命中里掉队 -> vector_score=0；
    全部含 'alpha' 与 query 同桶，cosine 必 >0，故 60 条 vector_score 全 >0 即证 k=60。
    """
    chunks = [_chunk(f"d{i}#0", f"alpha 共同主题块 {i}") for i in range(60)]
    retriever = HybridRetriever(chunks, embedding_backend=DeterministicEmbeddingBackend(dim=32))
    results = retriever.search("alpha", top_k=60)
    assert len(results) == 60
    assert all(r.vector_score > 0.0 for r in results)
