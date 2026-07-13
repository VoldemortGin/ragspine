"""多库/多路检索路由（批次 2.2 ②）不变量绑定（conformance）。

对【每个路由模式】（none＝扇出 / keyword＝按描述路由）参数化断言核心不变量：
    - 隔离继承：以真实 NarrativeIndexRetriever 为库 base（各自出口已剔 RESTRICTED），跨库融合结果
      绝不含 RESTRICTED——RESTRICTED 绝不出域。
    - provenance 库来源维度：每条融合结果都带非空 library_id + doc_id + source_locator。
    - 确定性：同 query 两次融合逐位一致。

非空泛证明：一个「泄漏 RESTRICTED」的反证 base（不剔除）喂进同一隔离断言核【必须 FAIL】——证明
断言核确实能抓到泄漏（隔离必须来自 base 出口，融合层忠实透传、绝不自行放大）。
"""

import os
from typing import Any

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta, chunk_document
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever
from ragspine.retrieval.routing.multi_index import (
    LIBRARY_ID_KEY,
    LibraryIndex,
    MultiIndexRetriever,
)
from ragspine.retrieval.routing.router import make_library_router

ROUTER_MODES = ("none", "keyword")


def _real_library(tmp_path, name: str, docs: list[tuple[str, str, str]]) -> LibraryIndex:
    store = ChunkStore(tmp_path / f"{name}.db")
    store.init_schema()
    for doc_id, text, sens in docs:
        store.replace_doc_chunks(
            doc_id, chunk_document(text, DocumentMeta(doc_id=doc_id, topic="FIN", sensitivity=sens)))
    return LibraryIndex(name, f"{name} 营收 财务 库", NarrativeIndexRetriever(NarrativeIndex(store)))


def _assert_isolation_and_provenance(retriever: MultiIndexRetriever) -> None:
    """隔离 + provenance 断言核：融合结果无 RESTRICTED、每条带 library_id + doc_id + source_locator。"""
    out = retriever.retrieve("营收")
    assert out, "应至少检索到公开块"
    for s in out:
        assert str(s.get("sensitivity", "")).upper() != "RESTRICTED", f"RESTRICTED 出域：{s!r}"
        assert s.get(LIBRARY_ID_KEY), f"缺库来源维度 library_id：{s!r}"
        assert s.get("doc_id") and s.get("source_locator"), f"缺 provenance：{s!r}"
    # 确定性。
    assert [s["chunk_id"] for s in retriever.retrieve("营收")] == [s["chunk_id"] for s in out]


@pytest.mark.parametrize("router_mode", ROUTER_MODES, ids=ROUTER_MODES)
def test_routing_mode_isolation_and_provenance(tmp_path, router_mode):
    libs = [
        _real_library(tmp_path, "liba", [("pub_a.pdf", "营收公开A。", "INTERNAL"),
                                          ("sec_a.pdf", "营收机密A。", "RESTRICTED")]),
        _real_library(tmp_path, "libb", [("pub_b.pdf", "营收公开B。", "INTERNAL"),
                                          ("sec_b.pdf", "营收机密B。", "RESTRICTED")]),
    ]
    retriever = MultiIndexRetriever(libs, router=make_library_router(router_mode))
    _assert_isolation_and_provenance(retriever)


class _LeakyLibraryRetriever:
    """反证 base：直接吐出一个 RESTRICTED snippet（【故意】不剔除）——模拟丢了出口隔离的 base。"""

    def retrieve(self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50) -> list[dict[str, Any]]:
        return [{"text": "机密", "doc_id": "leak.pdf", "chunk_id": "leak#0",
                 "source_locator": "leak.pdf#para1", "sensitivity": "RESTRICTED"}]


def test_leaky_base_fails_isolation_core():
    """泄漏 RESTRICTED 的反证 base 喂进同一隔离断言核必须 FAIL——证明隔离断言非空泛。"""
    retriever = MultiIndexRetriever([LibraryIndex("leak", "营收 库", _LeakyLibraryRetriever())])
    with pytest.raises(AssertionError):
        _assert_isolation_and_provenance(retriever)
