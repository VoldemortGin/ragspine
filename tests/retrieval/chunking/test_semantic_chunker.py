"""semantic 切块策略（W10，Chunker 缝 opt-in 实现）单测（TDD 红 → 绿）。

钉死合约：
- 按相邻段 embedding 距离峰值切分（拓扑连贯），而非纯定长——话题变化处起新块。
- 相似/相同相邻段聚在一起（不在低距离处切）。
- 段内预算/超长/provenance 全复用 chunk_document（块 text=原文子串，locator 全局段号）。
- 确定性（给定确定性 embedder，同输入两次逐位一致）；默认 embedder 为零依赖确定性词法散列后端，
  故离线可跑；真语义 ONNX 后端经注入 opt-in。
- 经 make_chunker('semantic') opt-in；默认仍 DefaultChunker（字节不变，另测）。

红：semantic_chunker 落地前 import 即 ModuleNotFoundError。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunker import Chunker, make_chunker
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.chunking.semantic_chunker import SemanticChunker


def _meta() -> DocumentMeta:
    return DocumentMeta(
        doc_id="doc.pdf",
        title="t",
        topic="FIN",
        entity="ACME_HK",
        geography="HK",
        period="2025H1",
        language="zh",
        sensitivity="INTERNAL",
    )


# 两个话题各两段：营收话题（P1/P2 共享词）→ 人事话题（P3/P4 共享词）。
# 距离谱：d(P1,P2) 低、d(P2,P3) 高（话题切换峰值）、d(P3,P4) 低 → 在 P2|P3 处切一刀。
_TWO_TOPICS = "\n".join([
    "本季度收入增长强劲表现良好",
    "本季度收入增长保持稳定良好",
    "员工培训计划全面展开推进",
    "员工福利政策持续优化改进",
])


def test_is_runtime_checkable():
    assert isinstance(SemanticChunker(), Chunker)


def test_splits_at_topic_boundary():
    """相邻段 embedding 距离峰值处切分：两话题 → 两块，切点在话题切换处。"""
    chunks = SemanticChunker().chunk(_TWO_TOPICS, _meta(), max_chars=480, overlap_chars=0)
    assert len(chunks) == 2
    assert "收入" in chunks[0].text and "员工" not in chunks[0].text
    assert "员工" in chunks[1].text and "收入" not in chunks[1].text


def test_identical_paragraphs_stay_together():
    """相同相邻段距离为 0（不在低距离处切）→ 聚成一块（预算内）。"""
    text = "\n".join(["完全一样的内容", "完全一样的内容", "完全一样的内容"])
    chunks = SemanticChunker().chunk(text, _meta(), max_chars=480, overlap_chars=0)
    assert len(chunks) == 1


def test_chunk_text_is_original_substring_join():
    """块 text 是段落以换行连接（原文子串契约不破）。"""
    chunks = SemanticChunker().chunk(_TWO_TOPICS, _meta(), max_chars=480, overlap_chars=0)
    for c in chunks:
        # 每块由相邻段以 '\n' 连接，且是原文的连续片段。
        assert c.text in _TWO_TOPICS


def test_provenance_global_para_numbers():
    """locator/para 用全局段号（与 chunk_document 同口径）；块2 从 para3 起。"""
    chunks = SemanticChunker().chunk(_TWO_TOPICS, _meta(), max_chars=480, overlap_chars=0)
    for c in chunks:
        assert c.doc_id == "doc.pdf"
        assert c.source_locator
    assert chunks[0].para_start == 1
    assert chunks[1].para_start == 3


def test_empty_text_returns_empty():
    assert SemanticChunker().chunk("  \n ", _meta()) == []


def test_invalid_params_raise():
    with pytest.raises(ValueError):
        SemanticChunker().chunk(_TWO_TOPICS, _meta(), max_chars=0)
    with pytest.raises(ValueError):
        SemanticChunker().chunk(_TWO_TOPICS, _meta(), max_chars=100, overlap_chars=100)


def test_deterministic():
    a = SemanticChunker().chunk(_TWO_TOPICS, _meta(), max_chars=480, overlap_chars=0)
    b = SemanticChunker().chunk(_TWO_TOPICS, _meta(), max_chars=480, overlap_chars=0)
    assert a == b


def test_injected_embedder_is_used():
    """注入的 embedder 被真正调用（真语义 ONNX 后端由此接入 opt-in）。"""

    class SpyEmbedder:
        def __init__(self) -> None:
            self.calls = 0

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            # 交替向量，制造相邻段高距离（强制每段切开）。
            return [[1.0, 0.0] if i % 2 == 0 else [0.0, 1.0] for i, _ in enumerate(texts)]

    spy = SpyEmbedder()
    chunks = SemanticChunker(embedder=spy).chunk(_TWO_TOPICS, _meta(), max_chars=480, overlap_chars=0)
    assert spy.calls >= 1
    assert len(chunks) == 4  # 四段两两正交 → 每段自成一块


def test_make_chunker_selects_semantic():
    assert isinstance(make_chunker("semantic"), SemanticChunker)
