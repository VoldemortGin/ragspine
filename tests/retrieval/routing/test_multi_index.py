"""多库/多路检索路由（批次 2.2 ②）单测：跨库融合 + 路由缝 + 库来源维度 + RESTRICTED 隔离继承。"""

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
    NarrativeRetriever,
)
from ragspine.retrieval.routing.router import (
    LIBRARY_ROUTER_ENV,
    KeywordLibraryRouter,
    LibraryRouter,
    make_library_router,
)


class _FakeRetriever:
    """确定性假库检索器：返回预置 snippet 列表（顺序即 RRF rank）。"""

    def __init__(self, snippets: list[dict[str, Any]]):
        self._snippets = snippets

    def retrieve(self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50):
        return [dict(s) for s in self._snippets[:top_k]]


def _snip(chunk_id: str, text: str = "t", sensitivity: str = "INTERNAL") -> dict[str, Any]:
    return {
        "text": text, "doc_id": chunk_id.split("#")[0], "chunk_id": chunk_id,
        "source_locator": f"{chunk_id}#para1", "sensitivity": sensitivity,
    }


# ---------------- 融合 + 库来源维度 ----------------
def test_is_narrative_retriever_protocol():
    m = MultiIndexRetriever([LibraryIndex("a", "desc", _FakeRetriever([]))])
    assert isinstance(m, NarrativeRetriever)


def test_fanout_fuses_and_tags_library_id():
    lib_a = LibraryIndex("law", "法律库", _FakeRetriever([_snip("a#0"), _snip("a#1")]))
    lib_b = LibraryIndex("fin", "财务库", _FakeRetriever([_snip("b#0")]))
    m = MultiIndexRetriever([lib_a, lib_b])
    out = m.retrieve("q")
    ids = {s["chunk_id"] for s in out}
    assert ids == {"a#0", "a#1", "b#0"}  # 扇出全部库并集
    # 每条都带库来源维度。
    assert all(LIBRARY_ID_KEY in s for s in out)
    assert {s["chunk_id"]: s[LIBRARY_ID_KEY] for s in out} == {
        "a#0": "law", "a#1": "law", "b#0": "fin"}


def test_top_of_each_library_ranked_high():
    """各库 rank-1（a#0 / b#0）RRF 分应高于 a#1（rank-2）。"""
    lib_a = LibraryIndex("law", "d", _FakeRetriever([_snip("a#0"), _snip("a#1")]))
    lib_b = LibraryIndex("fin", "d", _FakeRetriever([_snip("b#0")]))
    out = MultiIndexRetriever([lib_a, lib_b]).retrieve("q")
    order = [s["chunk_id"] for s in out]
    assert order.index("a#1") == 2  # a#1 rank 最低（在两个 rank-1 之后）


def test_deterministic():
    lib_a = LibraryIndex("law", "d", _FakeRetriever([_snip("a#0"), _snip("a#1")]))
    lib_b = LibraryIndex("fin", "d", _FakeRetriever([_snip("b#0")]))
    m = MultiIndexRetriever([lib_a, lib_b])
    assert [s["chunk_id"] for s in m.retrieve("q")] == [s["chunk_id"] for s in m.retrieve("q")]


def test_duplicate_library_id_raises():
    with pytest.raises(ValueError):
        MultiIndexRetriever([
            LibraryIndex("x", "d", _FakeRetriever([])),
            LibraryIndex("x", "d", _FakeRetriever([])),
        ])


def test_top_k_truncates():
    lib = LibraryIndex("law", "d", _FakeRetriever([_snip(f"a#{i}") for i in range(10)]))
    m = MultiIndexRetriever([lib], top_k=3)
    assert len(m.retrieve("q")) == 3


# ---------------- 路由缝 ----------------
def test_make_library_router_default_none():
    assert make_library_router(None) is None
    assert make_library_router("none") is None


def test_make_library_router_env_and_unknown(monkeypatch):
    monkeypatch.setenv(LIBRARY_ROUTER_ENV, "keyword")
    assert isinstance(make_library_router(None), KeywordLibraryRouter)
    monkeypatch.delenv(LIBRARY_ROUTER_ENV, raising=False)
    with pytest.raises(ValueError):
        make_library_router("neural")


def test_keyword_router_is_protocol():
    assert isinstance(KeywordLibraryRouter(), LibraryRouter)


def test_keyword_router_selects_by_description_overlap():
    libs = [
        LibraryIndex("law", "劳动合同 法律 条款", _FakeRetriever([])),
        LibraryIndex("fin", "营收 财务 报表", _FakeRetriever([])),
    ]
    chosen = KeywordLibraryRouter().route("劳动合同怎么解除", libs)
    assert chosen == ["law"]  # 只命中法律库描述


def test_router_zero_overlap_returns_empty_and_fans_out():
    libs = [LibraryIndex("a", "xxx", _FakeRetriever([_snip("a#0")])),
            LibraryIndex("b", "yyy", _FakeRetriever([_snip("b#0")]))]
    router = KeywordLibraryRouter()
    assert router.route("完全无关 zzz", libs) == []  # 零重叠
    # MultiIndexRetriever 回落扇出全部库（不饿死召回）。
    out = MultiIndexRetriever(libs, router=router).retrieve("完全无关 zzz")
    assert {s["chunk_id"] for s in out} == {"a#0", "b#0"}


def test_routed_mode_restricts_to_selected_library():
    libs = [
        LibraryIndex("law", "法律 条款", _FakeRetriever([_snip("law#0")])),
        LibraryIndex("fin", "财务 营收", _FakeRetriever([_snip("fin#0")])),
    ]
    out = MultiIndexRetriever(libs, router=KeywordLibraryRouter()).retrieve("营收 多少")
    assert {s["chunk_id"] for s in out} == {"fin#0"}
    assert all(s[LIBRARY_ID_KEY] == "fin" for s in out)


# ---------------- RESTRICTED 隔离继承（真实 base）----------------
def test_restricted_never_surfaces_across_libraries(tmp_path):
    """每库 base（NarrativeIndexRetriever）出口剔 RESTRICTED；跨库融合恒为其子集，RESTRICTED 绝不出域。"""
    def _make_lib(name: str, docs: list[tuple[str, str, str]]) -> LibraryIndex:
        store = ChunkStore(tmp_path / f"{name}.db")
        store.init_schema()
        for doc_id, text, sens in docs:
            store.replace_doc_chunks(
                doc_id, chunk_document(text, DocumentMeta(doc_id=doc_id, topic="FIN", sensitivity=sens)))
        idx = NarrativeIndex(store)
        return LibraryIndex(name, f"{name} 营收 库", NarrativeIndexRetriever(idx))

    lib_a = _make_lib("liba", [("pub_a.pdf", "营收公开A。", "INTERNAL"),
                               ("sec_a.pdf", "营收机密A。", "RESTRICTED")])
    lib_b = _make_lib("libb", [("sec_b.pdf", "营收机密B。", "RESTRICTED")])
    out = MultiIndexRetriever([lib_a, lib_b]).retrieve("营收")
    assert all(str(s["sensitivity"]).upper() != "RESTRICTED" for s in out)
    assert all(s["doc_id"] not in ("sec_a.pdf", "sec_b.pdf") for s in out)
    # 命中项带库来源维度 + 完整 provenance。
    for s in out:
        assert s[LIBRARY_ID_KEY] in ("liba", "libb")
        assert s["doc_id"] and s["source_locator"]
