"""W10 切块策略单测：sentence-window + semantic + RAPTOR（Chunker 缝）。

全部确定性、可离线（语义/RAPTOR 默认零依赖确定性后端 + 抽取式摘要）。重点验证 provenance（doc_id +
source_locator）、is_synthesis 标注、parent_id 树连接、边界切分、确定性。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunker import make_chunker
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.chunking.raptor import (
    ExtractiveSummarizer,
    LLMSummarizer,
    RaptorChunker,
    _connected_components,
)
from ragspine.retrieval.chunking.semantic_chunker import SemanticChunker
from ragspine.retrieval.chunking.sentence_window import SentenceWindowChunker


def _meta() -> DocumentMeta:
    return DocumentMeta(doc_id="report.pptx", title="T", entity="ACME_HK", sensitivity="INTERNAL")


class FakeBackend:
    """按 text -> 固定向量映射的确定性 embedding 后端替身（控制相似度）。"""

    def __init__(self, mapping, default=None):
        self.mapping = mapping
        self.default = default or [0.0, 0.0, 1.0]

    def embed_texts(self, texts):
        return [self.mapping.get(t, self.default) for t in texts]


# ===========================================================================
# sentence-window
# ===========================================================================

def test_sentence_window_centers_each_sentence_with_context():
    text = "第一句。第二句。第三句。第四句。"
    chunks = SentenceWindowChunker(window_size=1).chunk(text, _meta())
    assert len(chunks) == 4  # 每句一个中心
    # 中心为第二句的块应含第一、二、三句（±1 窗口）。
    assert "第一句" in chunks[1].text and "第二句" in chunks[1].text and "第三句" in chunks[1].text
    # 首句块只含 第一、二句（左侧无邻句）。
    assert "第三句" not in chunks[0].text
    for c in chunks:
        assert c.doc_id == "report.pptx"
        assert c.source_locator


def test_sentence_window_zero_window_is_single_sentences():
    text = "甲。乙。丙。"
    chunks = SentenceWindowChunker(window_size=0).chunk(text, _meta())
    assert [c.text for c in chunks] == ["甲。", "乙。", "丙。"]


def test_sentence_window_deterministic_and_empty():
    assert SentenceWindowChunker().chunk("", _meta()) == []
    text = "甲。乙。丙。丁。戊。"
    a = [c.text for c in SentenceWindowChunker().chunk(text, _meta())]
    b = [c.text for c in SentenceWindowChunker().chunk(text, _meta())]
    assert a == b


def test_sentence_window_invalid_param():
    with pytest.raises(ValueError):
        SentenceWindowChunker(window_size=-1)


# ===========================================================================
# semantic
# ===========================================================================

def test_semantic_splits_on_similarity_boundary():
    """相邻句相似度跌破阈值处切块：A1~A2 相似（同簇），A2→B 突变（切），B1~B2 相似。"""
    text = "甲句一。甲句二。乙句一。乙句二。"
    # 甲句 向量相近、乙句 向量相近、甲↔乙 正交。
    backend = FakeBackend({
        "甲句一。": [1.0, 0.0, 0.0],
        "甲句二。": [0.96, 0.1, 0.0],
        "乙句一。": [0.0, 1.0, 0.0],
        "乙句二。": [0.0, 0.97, 0.1],
    })
    chunks = SemanticChunker(backend, similarity_threshold=0.5).chunk(text, _meta())
    assert len(chunks) == 2, "应在 甲->乙 边界切成两块"
    assert "甲句一" in chunks[0].text and "甲句二" in chunks[0].text
    assert "乙句一" in chunks[1].text and "乙句二" in chunks[1].text
    for c in chunks:
        assert c.doc_id and c.source_locator


def test_semantic_default_backend_offline():
    """默认零依赖确定性后端：不注入 backend 也能切、且确定。"""
    text = "香港收入下降。新加坡利润增长。代理人渠道扩张。"
    c1 = [c.text for c in SemanticChunker().chunk(text, _meta())]
    c2 = [c.text for c in SemanticChunker().chunk(text, _meta())]
    assert c1 == c2 and c1


def test_semantic_max_chars_forces_split():
    """即便语义同簇，超 max_chars 也强制开新块（防超窗）。"""
    text = "甲。乙。丙。"
    backend = FakeBackend({}, default=[1.0, 0.0, 0.0])  # 全相同向量 -> 永不语义切
    chunks = SemanticChunker(backend, similarity_threshold=0.5).chunk(
        text, _meta(), max_chars=2
    )
    assert len(chunks) == 3  # 每句超 2 字预算 -> 各自成块


def test_semantic_invalid_threshold():
    with pytest.raises(ValueError):
        SemanticChunker(similarity_threshold=2.0)


# ===========================================================================
# RAPTOR
# ===========================================================================

def test_connected_components_deterministic():
    vecs = [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]]
    comps = _connected_components(vecs, 0.5)
    assert comps == [[0, 1], [2]]


def test_extractive_summarizer_picks_central_sentences():
    s = ExtractiveSummarizer(max_sentences=1)
    out = s.summarize(["香港收入下降。香港收入下降明显。无关内容。"])
    assert "香港收入下降" in out


def test_extractive_summarizer_short_keeps_all():
    s = ExtractiveSummarizer(max_sentences=3)
    assert s.summarize(["只有一句"]) == "只有一句"


def test_raptor_builds_synthesis_nodes_with_provenance_and_parent_links():
    """两叶聚成一簇 -> 生成 is_synthesis 摘要节点，叶 parent_id 指向它，摘要带血缘。"""
    text = "香港收入下降。香港收入下降明显。"  # 两句 -> 两叶（max_chars 小）
    # 让两叶向量相同 -> 必聚一簇。
    backend = FakeBackend({}, default=[1.0, 0.0, 0.0])
    chunker = RaptorChunker(
        backend, ExtractiveSummarizer(), cluster_similarity=0.5,
        max_levels=2, min_cluster_size=2,
    )
    chunks = chunker.chunk(text, _meta(), max_chars=8, overlap_chars=0)

    leaves = [c for c in chunks if not c.is_synthesis]
    synth = [c for c in chunks if c.is_synthesis]
    assert len(leaves) >= 2
    assert len(synth) >= 1, "应生成至少一个摘要节点"
    # 摘要节点血缘齐备且标 synthesis。
    for s in synth:
        assert s.doc_id == "report.pptx"
        assert s.source_locator
        assert s.is_synthesis is True
        # seq 不与叶冲突（store 唯一索引前提）。
    seqs = [c.seq for c in chunks]
    assert len(seqs) == len(set(seqs)), "seq 全局唯一"
    # 叶的 parent_id 指向某摘要节点。
    synth_ids = {s.chunk_id for s in synth}
    assert any(leaf.parent_id in synth_ids for leaf in leaves)


def test_raptor_no_clustering_returns_leaves_only():
    """各叶互不相似（正交）-> 无簇 -> 只返回叶，无摘要节点。"""
    text = "甲甲甲。乙乙乙。"
    backend = FakeBackend({
        # 叶文本是段落聚合，这里让任何文本都拿到正交向量（按调用序）。
    }, default=[1.0, 0.0, 0.0])

    # 用一个每次返回不同正交向量的后端，确保叶不相似。
    class OrthoBackend:
        def embed_texts(self, texts):
            return [[1.0 if i == j else 0.0 for j in range(len(texts))] for i in range(len(texts))]

    chunks = RaptorChunker(OrthoBackend(), min_cluster_size=2).chunk(
        text, _meta(), max_chars=4, overlap_chars=0
    )
    assert all(not c.is_synthesis for c in chunks)


def test_raptor_deterministic():
    text = "香港收入下降。香港收入下降明显。新加坡利润增长。"
    backend = FakeBackend({}, default=[1.0, 0.0, 0.0])
    mk = lambda: RaptorChunker(backend, cluster_similarity=0.5, min_cluster_size=2)
    a = [(c.chunk_id, c.is_synthesis, c.parent_id) for c in mk().chunk(text, _meta(), max_chars=10, overlap_chars=0)]
    b = [(c.chunk_id, c.is_synthesis, c.parent_id) for c in mk().chunk(text, _meta(), max_chars=10, overlap_chars=0)]
    assert a == b


def test_raptor_empty_text():
    assert RaptorChunker().chunk("", _meta()) == []


def test_raptor_invalid_params():
    with pytest.raises(ValueError):
        RaptorChunker(max_levels=0)
    with pytest.raises(ValueError):
        RaptorChunker(min_cluster_size=1)


class _BoomProvider:
    def chat(self, messages, *, tools=None):
        from corespine import ProviderError

        raise ProviderError("boom")


def test_llm_summarizer_degrades_to_extractive_on_error():
    s = LLMSummarizer(_BoomProvider())
    out = s.summarize(["香港收入下降。香港收入下降明显。无关。"])
    assert "香港收入下降" in out  # 抽取式兜底产出


# ===========================================================================
# make_chunker 选型
# ===========================================================================

def test_make_chunker_resolves_w10_strategies():
    assert isinstance(make_chunker("sentence_window"), SentenceWindowChunker)
    assert isinstance(make_chunker("sentence-window"), SentenceWindowChunker)
    assert isinstance(make_chunker("semantic"), SemanticChunker)
    assert isinstance(make_chunker("raptor"), RaptorChunker)


def test_make_chunker_none_still_byte_identical():
    assert make_chunker("none") is None
    assert make_chunker(None) is None
