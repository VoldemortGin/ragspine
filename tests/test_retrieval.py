"""混合检索测试（叙事通路检索侧，TDD 红色阶段）。

只验证外部行为：中英混排分词、BM25 手算例与强信号实体词（Nexora/CPL/MPFA）命中、
余弦相似度、RRF 手算融合、元数据预过滤先于打分（fake backend 录调用钉死）、
top-50 默认召回、multi-query 改写合并、glossary 规则改写器、NarrativeIndex 端到端
（建库->检索->二审，Restricted 不出域）。

依赖全部 fake 注入（确定性向量 / 确定性 judge），零网络、零真实模型、零新依赖。
红色预期：行为入口因 stub raise NotImplementedError 而全部 FAIL。
"""

import os
import zlib

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import Chunk, DocumentMeta
from ragspine.retrieval.lexical.retrieval import (
    DEFAULT_TOP_K,
    GlossaryQueryRewriter,
    HybridRetriever,
    NarrativeIndex,
    RetrievalResult,
    bm25_scores,
    cosine_similarity,
    rrf_fuse,
    tokenize,
)


# ---------------------------------------------------------------------------
# 测试专用 fake（定义在测试文件内，不放 src）
# ---------------------------------------------------------------------------

class FakeEmbeddingBackend:
    """实现 EmbeddingBackend 协议的确定性替身。

    基于字符 crc32 的词袋向量（与 PYTHONHASHSEED 无关，跨平台确定），并记录所有
    被要求 embedding 的文本，供「预过滤先于打分」断言使用。
    """

    DIM = 64

    def __init__(self):
        self.embedded: list[str] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [self._embed(t) for t in texts]

    @classmethod
    def _embed(cls, text: str) -> list[float]:
        vec = [0.0] * cls.DIM
        for ch in text.lower():
            if not ch.isspace():
                vec[zlib.crc32(ch.encode("utf-8")) % cls.DIM] += 1.0
        return vec


class FakeRewriter:
    """实现 QueryRewriter 协议的确定性替身：固定追加预设变体。"""

    def __init__(self, extra: list[str]):
        self.extra = extra

    def rewrite(self, query: str) -> list[str]:
        return [query, *self.extra]


class SpyJudge:
    """实现 ListwiseJudge 协议的替身：录下调用并按倒序返回。"""

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        self.calls.append((query, list(candidates)))
        return list(range(len(candidates)))[::-1]


def _chunk(chunk_id: str, text: str, **overrides) -> Chunk:
    """构造检索语料块。"""
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
    """跨 topic/entity 的小语料，含强信号实体词 Nexora / MPFA / CPL。"""
    return [
        _chunk("c1#0", "Nexora 事件对银保渠道的影响评估", topic="REG", entity="ACME_CN", geography="CN"),
        _chunk("c2#0", "MPFA 强积金新规要求披露管理费", topic="REG", entity="ACME_HK"),
        _chunk("c3#0", "CPL 渠道归因分析显示银保增长", topic="FIN", entity="ACME_CN", geography="CN"),
        _chunk("c4#0", "香港 REVENUE 营收持续增长", topic="FIN", entity="ACME_HK"),
        _chunk("c5#0", "weekend cricket match report 板球比赛", topic="OTHER", entity="NONE"),
    ]


# ===========================================================================
# 分词：中英混排
# ===========================================================================

def test_tokenize_ascii_words_case_folded():
    """ASCII 连续串按词、大小写归一。"""
    tokens = tokenize("Nexora CPL2024 MPFA")
    assert "nexora" in tokens
    assert "cpl2024" in tokens
    assert "mpfa" in tokens
    assert all(t == t.lower() for t in tokens)


def test_tokenize_cjk_bigram_and_unigram():
    """CJK 按字符 bigram，一元也保留。"""
    tokens = tokenize("影响")
    assert {"影", "响", "影响"} <= set(tokens)


def test_tokenize_mixed():
    """中英混排：英文词 + 中文一元/二元共存。"""
    tokens = tokenize("MPFA新规")
    assert {"mpfa", "新", "规", "新规"} <= set(tokens)


def test_tokenize_empty_and_punct():
    """空串 / 纯标点 -> []。"""
    assert tokenize("") == []
    assert tokenize(" !!! ，。 ") == []


# ===========================================================================
# BM25：手算例 + 强信号词
# ===========================================================================

def test_bm25_hand_example():
    """手算例：docs=[[x,x,y],[y,z]]、query=[x]，标准 Okapi k1=1.5/b=0.75。

    idf(x)=ln(1+(2-1+0.5)/(1+0.5))=ln2；avgdl=2.5；
    doc0: tf=2, dl=3 -> ln2 * 2*2.5 / (2+1.5*(0.25+0.75*3/2.5)) = 0.930399。
    """
    scores = bm25_scores(["x"], [["x", "x", "y"], ["y", "z"]])
    assert scores[0] == pytest.approx(0.930399, rel=1e-4)
    assert scores[1] == 0.0


def test_bm25_absent_term_zero():
    """query 词不在语料 -> 全 0。"""
    scores = bm25_scores(["nope"], [["x", "y"], ["z"]])
    assert scores == [0.0, 0.0]


def test_bm25_entity_strong_signal(corpus):
    """强信号实体词：query 'Nexora 影响' 在 BM25 下最高分是含 Nexora 的块。"""
    docs_tokens = [tokenize(c.text) for c in corpus]
    scores = bm25_scores(tokenize("Nexora 影响"), docs_tokens)
    assert scores.index(max(scores)) == 0  # c1 含 Nexora + 影响


# ===========================================================================
# 余弦 / RRF 手算
# ===========================================================================

def test_cosine_basics():
    """同向=1、正交=0、零向量=0。"""
    assert cosine_similarity([1.0, 2.0], [2.0, 4.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_rrf_hand_calc():
    """手算例：[[A,B],[A,C,B]]、k=60：A=2/61，B=1/62+1/63，C=1/62（rank 从 1 起）。"""
    fused = rrf_fuse([["A", "B"], ["A", "C", "B"]], k=60)
    assert fused["A"] == pytest.approx(2 / 61)
    assert fused["B"] == pytest.approx(1 / 62 + 1 / 63)
    assert fused["C"] == pytest.approx(1 / 62)
    assert fused["A"] > fused["B"] > fused["C"]


def test_rrf_k_param():
    """k 可参数化：k=1 时 [[A],[A]] -> A=1/2+1/2=1.0。"""
    fused = rrf_fuse([["A"], ["A"]], k=1)
    assert fused["A"] == pytest.approx(1.0)


# ===========================================================================
# HybridRetriever：通道 / 预过滤 / top-k / multi-query
# ===========================================================================

def test_bm25_only_search_hits_entity(corpus):
    """纯 BM25 模式（无 backend）：'Nexora 事件' top1 = c1，通道得分可解释。"""
    retriever = HybridRetriever(corpus)
    results = retriever.search("Nexora 事件")
    assert results
    top = results[0]
    assert isinstance(top, RetrievalResult)
    assert top.chunk.chunk_id == "c1#0"
    assert top.bm25_score > 0
    assert top.vector_score == 0.0
    assert top.fused_score > 0


def test_bm25_only_excludes_no_hit_chunks(corpus):
    """纯 BM25 模式下全 query 零命中的块不出现在结果里。"""
    retriever = HybridRetriever(corpus)
    ids = {r.chunk.chunk_id for r in retriever.search("Nexora")}
    assert "c5#0" not in ids


def test_vector_channel_scores(corpus):
    """向量通道：query 与块文本完全相同 -> 该块 vector_score≈1 且居首。"""
    retriever = HybridRetriever(corpus, embedding_backend=FakeEmbeddingBackend())
    results = retriever.search("香港 REVENUE 营收持续增长")
    assert results[0].chunk.chunk_id == "c4#0"
    assert results[0].vector_score == pytest.approx(1.0, abs=1e-9)


def test_prefilter_before_scoring(corpus):
    """预过滤先于打分：被过滤块即便强命中 query 也不进结果、其文本不被 embedding。"""
    backend = FakeEmbeddingBackend()
    retriever = HybridRetriever(corpus, embedding_backend=backend)
    results = retriever.search("Nexora 事件", topic="FIN")
    assert results
    assert all(r.chunk.topic == "FIN" for r in results)
    assert "c1#0" not in {r.chunk.chunk_id for r in results}
    # c1（topic=REG）的文本绝不进入向量打分。
    assert corpus[0].text not in backend.embedded


def test_prefilter_combination(corpus):
    """组合过滤 AND：topic=REG + entity=ACME_HK -> 只剩 c2。"""
    retriever = HybridRetriever(corpus)
    results = retriever.search("MPFA 新规", topic="REG", entity="ACME_HK")
    assert {r.chunk.chunk_id for r in results} == {"c2#0"}


def test_prefilter_no_match_empty(corpus):
    """过滤后无候选 -> []。"""
    retriever = HybridRetriever(corpus)
    assert retriever.search("Nexora", topic="NOPE") == []


def test_default_recall_top_50():
    """拍板默认召回 top-50：60 个命中块只回 50 个。"""
    chunks = [_chunk(f"d{i}#0", f"alpha 共同主题块 {i}") for i in range(60)]
    retriever = HybridRetriever(chunks)
    assert DEFAULT_TOP_K == 50
    assert len(retriever.search("alpha")) == 50


def test_top_k_parameterized():
    """top_k 可参数化（构造器与单次查询均可覆盖）。"""
    chunks = [_chunk(f"d{i}#0", f"alpha 共同主题块 {i}") for i in range(20)]
    assert len(HybridRetriever(chunks, top_k=7).search("alpha")) == 7
    assert len(HybridRetriever(chunks).search("alpha", top_k=3)) == 3


def test_results_sorted_by_fused_score(corpus):
    """结果按融合得分非增排序。"""
    retriever = HybridRetriever(corpus, embedding_backend=FakeEmbeddingBackend())
    results = retriever.search("银保 渠道 增长")
    scores = [r.fused_score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_multiquery_rewrite_merged_by_rrf(corpus):
    """multi-query 合并：原 query 召不回的块经改写变体召回（RRF 合并）。"""
    extra_chunk = _chunk("c6#0", "REVENUE grew strongly in 2025")
    chunks = [*corpus, extra_chunk]
    plain = HybridRetriever(chunks)
    assert "c6#0" not in {r.chunk.chunk_id for r in plain.search("营收的表现")}

    rewriting = HybridRetriever(
        chunks, query_rewriter=FakeRewriter(["REVENUE 表现"])
    )
    ids = {r.chunk.chunk_id for r in rewriting.search("营收的表现")}
    assert "c6#0" in ids
    assert "c4#0" in ids  # 原 query 命中的块仍在


def test_multiquery_duplicate_rewrites_tolerated(corpus):
    """改写器返回重复 query 不致重复计分/崩溃。"""
    retriever = HybridRetriever(
        corpus, query_rewriter=FakeRewriter(["Nexora 事件", "Nexora 事件"])
    )
    results = retriever.search("Nexora 事件")
    ids = [r.chunk.chunk_id for r in results]
    assert len(ids) == len(set(ids))
    assert ids[0] == "c1#0"


# ===========================================================================
# GlossaryQueryRewriter：确定性规则改写
# ===========================================================================

def test_rewriter_original_first():
    """原 query 恒在首位。"""
    queries = GlossaryQueryRewriter().rewrite("REVENUE 在香港的表现")
    assert queries[0] == "REVENUE 在香港的表现"
    assert len(queries) >= 2


def test_rewriter_canonicalizes_synonym():
    """中文同义词被改写出含受控代码的变体（营收 -> REVENUE）。"""
    queries = GlossaryQueryRewriter().rewrite("营收在香港的表现")
    assert any("REVENUE" in q for q in queries[1:])


def test_rewriter_deterministic():
    """同一 query 两次改写结果完全一致。"""
    rewriter = GlossaryQueryRewriter()
    q = "营收在香港的表现"
    assert rewriter.rewrite(q) == rewriter.rewrite(q)


def test_rewriter_unknown_terms_passthrough():
    """不含任何词典词条的 query 原样返回（只有原 query）。"""
    q = "completely unrelated text 板球"
    assert GlossaryQueryRewriter().rewrite(q) == [q]


def test_rewriter_max_queries_cap():
    """max_queries 截断，原 query 仍在首位。"""
    queries = GlossaryQueryRewriter(max_queries=3).rewrite("营收 香港 集团")
    assert len(queries) <= 3
    assert queries[0] == "营收 香港 集团"


def test_rewriter_ascii_word_boundary():
    """ASCII 词条要求词边界：'concern' 不触发 'cn' 词条。"""
    q = "no concern here"
    assert GlossaryQueryRewriter().rewrite(q) == [q]


# ===========================================================================
# NarrativeIndex：建库 -> 检索 -> 二审 端到端
# ===========================================================================

def _meta(doc_id: str, **overrides) -> DocumentMeta:
    kwargs = dict(
        doc_id=doc_id,
        title=doc_id,
        topic="FIN",
        entity="ACME_HK",
        geography="HK",
        period="2025H1",
        language="zh",
        sensitivity="INTERNAL",
    )
    kwargs.update(overrides)
    return DocumentMeta(**kwargs)


@pytest.fixture
def index_store(tmp_db_path):
    s = ChunkStore(tmp_db_path)
    s.init_schema()
    yield s
    s.close()


def test_index_ingest_and_retrieve(index_store):
    """端到端：入库两份文档，带 topic 过滤检索，命中正确文档且 citation 在。"""
    index = NarrativeIndex(index_store)
    n1 = index.ingest("香港 REVENUE 营收持续增长。", _meta("doc_fin"))
    n2 = index.ingest(
        "Nexora 事件的监管影响评估仍在进行。", _meta("doc_reg", topic="REG", entity="ACME_CN")
    )
    assert n1 >= 1 and n2 >= 1

    results = index.retrieve("Nexora 影响", topic="REG", rerank=False)
    assert results
    assert all(r.chunk.topic == "REG" for r in results)
    top = results[0]
    assert top.chunk.doc_id == "doc_reg"
    assert top.chunk.source_locator.startswith("doc_reg")


def test_index_reingest_idempotent(index_store):
    """同 doc 重复 ingest 不产生重复活跃块。"""
    index = NarrativeIndex(index_store)
    index.ingest("香港 REVENUE 增长。", _meta("doc_fin"))
    index.ingest("香港 REVENUE 增长。", _meta("doc_fin"))
    results = index.retrieve("REVENUE", rerank=False)
    ids = [r.chunk.chunk_id for r in results]
    assert len(ids) == len(set(ids))
    assert index_store.count() == 1


def test_index_rerank_with_judge(index_store):
    """二审：judge 倒序 -> 输出为 RRF 序的倒序（截 top_n）。"""
    judge = SpyJudge()
    index = NarrativeIndex(index_store, judge=judge)
    for i in range(3):
        index.ingest(f"REVENUE 表现文档 {i} 各不相同的内容{i}", _meta(f"doc{i}"))

    baseline = index.retrieve("REVENUE", rerank=False)
    assert len(baseline) == 3
    reranked = index.retrieve("REVENUE")
    assert [r.chunk.chunk_id for r in reranked] == [
        r.chunk.chunk_id for r in reversed(baseline)
    ]
    assert len(judge.calls) == 1
    query, candidates = judge.calls[0]
    assert query == "REVENUE"
    assert len(candidates) == 3


def test_index_restricted_never_reaches_judge(index_store):
    """Restricted 不出域：敏感块文本绝不进入 judge 候选，但仍可出现在结果里。"""
    judge = SpyJudge()
    index = NarrativeIndex(index_store, judge=judge)
    index.ingest("REVENUE 普通文档内容。", _meta("doc_pub"))
    index.ingest(
        "REVENUE SECRET-PR-9 高管绩效评级明细。",
        _meta("doc_sec", sensitivity="RESTRICTED"),
    )

    results = index.retrieve("REVENUE")
    assert judge.calls
    for _, candidates in judge.calls:
        assert all("SECRET-PR-9" not in c for c in candidates)
    assert any(r.chunk.sensitivity == "RESTRICTED" for r in results)


def test_index_rerank_false_skips_judge(index_store):
    """rerank=False -> judge 不被调用。"""
    judge = SpyJudge()
    index = NarrativeIndex(index_store, judge=judge)
    index.ingest("REVENUE 文档。", _meta("doc1"))
    index.retrieve("REVENUE", rerank=False)
    assert judge.calls == []
