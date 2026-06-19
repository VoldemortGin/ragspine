"""Chunker 切块处 provenance 不变量绑定（conformance）。

落地 docs/prd-breadth-via-adapters.md「Invariant-binding conformance kit · Provenance
(… Chunker)」：把 provenance 绑死在 Chunker 缝上，对【每个注册 chunker】参数化断言——任何切块
策略（含未来 semantic / contextual / parent-child）只要登记进 conftest.CHUNKER_IMPLS 就必须证明
每个 Chunk 都带非空 source_doc_id（= doc_id，血缘根）与 locator（source_locator，citation 回指）。
丢血缘的实现直接 CI 红，而非生产事故。

非空泛证明（同 SourceConnector / VectorStore 的「诚实反证」手法）：一个故意丢 doc_id /
source_locator 的 stub chunker 喂进同一断言核【必须 FAIL】，证明该断言不是空泛通过。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunker import Chunker
from ragspine.retrieval.chunking.chunking import Chunk, DocumentMeta

# 多段语料：足以被切成 >1 块，覆盖跨段 locator（para 起-止）。
_CORPUS = "\n".join(["甲" * 100, "乙" * 100, "丙" * 100, "丁" * 100])


def _meta() -> DocumentMeta:
    return DocumentMeta(
        doc_id="report.pptx",
        title="2025 上半年财务表现",
        topic="FIN",
        entity="ACME_HK",
        geography="HK",
        period="2025H1",
        language="zh",
        sensitivity="INTERNAL",
    )


def _assert_chunk_provenance_complete(chunker) -> int:
    """对一个 Chunker 断言：产出非空，且每个 Chunk 都带非空 doc_id（血缘根）+ source_locator。

    返回产出块数。这是 provenance pack 的【单一判定核】——参数化用例与「反证 stub」共用它，
    确保两者验的是同一条不变量。
    """
    chunks = chunker.chunk(_CORPUS, _meta(), max_chars=250, overlap_chars=0)
    assert chunks, "chunker 未产出任何 Chunk（语料非空，应至少切出一块）"
    for c in chunks:
        assert c.doc_id, f"Chunk 缺 doc_id（血缘根）：{c!r}"
        assert c.source_locator, f"Chunk 缺 source_locator（citation 回指）：{c!r}"
    return len(chunks)


# ===========================================================================
# P · Provenance：每个注册 Chunker 的产出都带齐血缘
# ===========================================================================

def test_every_chunk_carries_provenance(chunker):
    """每个注册 Chunker：产出的每个 Chunk 都带非空 doc_id + source_locator。"""
    n = _assert_chunk_provenance_complete(chunker)
    assert n > 1  # 250 字预算下 4x100 字语料应切成多块


def test_chunker_is_runtime_checkable(chunker):
    """每个注册 Chunker 都结构匹配 @runtime_checkable Chunker Protocol。"""
    assert isinstance(chunker, Chunker)


# ===========================================================================
# 非空泛证明：丢血缘的 stub 必须 FAIL（证明 provenance pack 非空泛）
# ===========================================================================

class _LineageDroppingChunker:
    """反证 stub：产出 doc_id / source_locator 皆空的 Chunk——【故意】违反 provenance。"""

    def chunk(self, text, meta, *, max_chars=480, overlap_chars=80):
        return [
            Chunk(
                chunk_id="x#c0",
                doc_id="",
                seq=0,
                text=text,
                source_locator="",
                para_start=1,
                para_end=1,
            )
        ]


def test_lineage_dropping_stub_fails_provenance():
    """喂丢血缘 stub 进同一断言核必须 AssertionError——证明 provenance pack 非空泛。"""
    with pytest.raises(AssertionError):
        _assert_chunk_provenance_complete(_LineageDroppingChunker())
