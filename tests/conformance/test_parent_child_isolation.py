"""父子（small-to-big）存储级展开的端到端不变量绑定（conformance，批次 2.2 follow-up）。

钉死（ADR 0018）：
    - 检索粒度=细 child、生成上下文=父小节窗口，二者【解耦】：命中 child 后 window_text 经独立的
      prompt_text 键作生成上下文，text / source_locator / chunk_id 仍是命中的细 child。
    - provenance 诚实：source_locator 指向 child 真实段落，parent_locator 附指父小节真实跨度，
      窗口扩展绝不伪装成命中证据、绝不产生新检索命中。
    - RESTRICTED 隔离（反向证明）：带 window_text 的 RESTRICTED 块经 A 线出口【整段被拒】——
      其父窗口绝不经 child 泄漏到生成上下文（最安全语义：window 随 child 受同一出口门控）。
    - 默认切块器（不填 window_text/parent_locator）→ snippet 无 prompt_text/parent_locator 键，
      逐位等价旧行为（字节不变）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore, StoredChunk
from ragspine.retrieval.chunking.chunker import make_chunker
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.lexical.retrieval import NarrativeIndex, RetrievalResult
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever

# 两小节；营收节多段（供 small-to-big 窗口展开验证），风险节含一段。
_TEXT = "# 营收\n营收增长。\n毛利改善。\n# 风险\n汇率波动。"


def _meta(doc_id: str = "report.pdf", sensitivity: str = "INTERNAL") -> DocumentMeta:
    return DocumentMeta(doc_id=doc_id, title="t", topic="FIN", sensitivity=sensitivity)


def _windowed_chunk(sensitivity: str, seq: int = 0) -> StoredChunk:
    """一个带非空 window_text/parent_locator 的 StoredChunk（模拟父子预设的存储产物）。"""
    return StoredChunk(
        chunk_id=f"d#c{seq}",
        doc_id="d",
        seq=seq,
        text="营收增长。",
        source_locator="d#para2",
        para_start=2,
        para_end=2,
        topic="FIN",
        sensitivity=sensitivity,
        parent_id="d#s0",
        heading="# 营收",
        window_text="# 营收\n营收增长。\n毛利改善。",
        parent_locator="d#para1-3",
    )


class _StubIndex:
    """返回预置 RetrievalResult 的最小 RetrievableIndex 替身（隔离出口的确定性反向证明）。"""

    def __init__(self, results: list[RetrievalResult]):
        self._results = results

    def retrieve(self, query, **kwargs):  # noqa: ANN001, ANN003
        return list(self._results)


# ===========================================================================
# 端到端：ingest（父子预设）→ store 持久化 → retrieve → snippet 展开
# ===========================================================================

def _ingest_and_retrieve(tmp_path, chunker_spec, *, sensitivity="INTERNAL"):
    store = ChunkStore(tmp_path / "chunk.db")
    store.init_schema()
    index = NarrativeIndex(store, max_chars=6, overlap_chars=0, chunker=make_chunker(chunker_spec))
    index.ingest(_TEXT, _meta(sensitivity=sensitivity))
    retriever = NarrativeIndexRetriever(index)
    snippets = retriever.retrieve("营收增长", top_k=50)
    store.close()
    return snippets


@pytest.mark.parametrize("spec", ["parent_child", "small_to_big"])
def test_child_hit_expands_to_parent_window(tmp_path, spec):
    """命中细 child → prompt_text 展开到父小节窗口；text/locator 仍是 child（检索/生成解耦）。"""
    snippets = _ingest_and_retrieve(tmp_path, spec)
    assert snippets, "应召回至少一个 child"
    # 存在一个命中 child：其自身 text 不含'毛利改善'，但父窗口 prompt_text 含之（small-to-big 展开）。
    expanded = [s for s in snippets if "毛利改善" not in s["text"] and "毛利改善" in s.get("prompt_text", "")]
    assert expanded, "至少一个 child 的父窗口应展开出 child 自身不含的兄弟段落"
    s = expanded[0]
    # provenance：source_locator 指向 child 真实段落，parent_locator 附指父小节真实跨度，二者不同。
    assert s["source_locator"].startswith("report.pdf#para")
    assert s["parent_locator"].startswith("report.pdf#para")
    assert s["source_locator"] != s["parent_locator"]
    # child text ⊆ 父窗口（窗口是诚实的上下文超集，非捏造）。
    assert s["text"] in s["prompt_text"]


def test_window_does_not_create_new_hits(tmp_path):
    """窗口只影响生成上下文，绝不产生新检索命中：命中数=细 child 命中数（与默认切块的召回同源）。"""
    snippets = _ingest_and_retrieve(tmp_path, "parent_child")
    # 每条 snippet 都是一个真实 child（有独立 chunk_id 与 source_locator），无一条以父窗口冒充命中。
    assert len({s["chunk_id"] for s in snippets}) == len(snippets)
    for s in snippets:
        assert s["chunk_id"] and s["source_locator"]


def test_default_chunker_snippet_byte_identical(tmp_path):
    """默认切块器：window_text/parent_locator 空 → snippet 无 prompt_text/parent_locator 键（字节不变）。"""
    snippets = _ingest_and_retrieve(tmp_path, "default")
    assert snippets
    for s in snippets:
        assert "prompt_text" not in s
        assert "parent_locator" not in s


# ===========================================================================
# RESTRICTED 隔离反向证明：带窗口的 RESTRICTED 块整段被拒，父窗口绝不泄漏
# ===========================================================================

def test_restricted_windowed_chunk_rejected_whole(tmp_path):
    """带 window_text 的 RESTRICTED 块经出口整段被拒——父窗口绝不经 child 泄漏到 prompt_text。"""
    internal = _windowed_chunk("INTERNAL", seq=0)
    restricted = _windowed_chunk("RESTRICTED", seq=1)
    restricted.window_text = "机密：内幕消息。"  # 若泄漏，会出现在某条 prompt_text 里
    index = _StubIndex([
        RetrievalResult(chunk=internal, bm25_score=1.0, vector_score=0.0, fused_score=1.0),
        RetrievalResult(chunk=restricted, bm25_score=1.0, vector_score=0.0, fused_score=0.9),
    ])
    snippets = NarrativeIndexRetriever(index).retrieve("营收", top_k=50)
    # RESTRICTED 块整段不出域：无其 chunk_id、无其正文、无其父窗口。
    assert all(s["chunk_id"] != "d#c1" for s in snippets)
    assert all(str(s.get("sensitivity", "")).upper() != "RESTRICTED" for s in snippets)
    joined = "".join(s["text"] + s.get("prompt_text", "") for s in snippets)
    assert "机密" not in joined and "内幕消息" not in joined
    # INTERNAL 块正常展开父窗口。
    assert any(s["chunk_id"] == "d#c0" and "毛利改善" in s.get("prompt_text", "") for s in snippets)


def test_restricted_only_yields_no_snippet(tmp_path):
    """仅有一个带窗口的 RESTRICTED 块时：零 snippet（整段拒绝，无任何窗口泄漏）。"""
    restricted = _windowed_chunk("RESTRICTED")
    index = _StubIndex([
        RetrievalResult(chunk=restricted, bm25_score=1.0, vector_score=0.0, fused_score=1.0),
    ])
    snippets = NarrativeIndexRetriever(index).retrieve("营收", top_k=50)
    assert snippets == []
