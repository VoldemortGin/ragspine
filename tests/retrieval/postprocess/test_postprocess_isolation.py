"""W8 隔离 conformance：postprocessor 的 RESTRICTED 隔离【继承】自 link 出口（同 W6b/W2 范式）。

拍板（docs/invariants.md「RESTRICTED isolation 两出口」）：link 出口（NarrativeIndexRetriever）在
出口剔除 sensitivity==RESTRICTED 的块。postprocessor 只对该【已剥离子集】做重排/去冗余/压缩，绝不
自行造片段、绝不直接读块库——故 RESTRICTED 永不进入 postprocessor、更永不出域。

reverse-proof：把 RESTRICTED 片段【直接】喂给 postprocessor，它会原样透传/压缩（不自带剔除）——
证明保护住在上游出口而非 postprocessor 本身（能抓住「postprocessor 绕过出口直接吞块」的回归）。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import (
    NarrativeIndexRetriever,
    build_narrative_retriever,
)
from ragspine.retrieval.postprocess import (
    PROMPT_TEXT_KEY,
    CompressionPostprocessor,
    LostInTheMiddlePostprocessor,
    MMRPostprocessor,
    make_postprocessor,
)
from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY

NORMAL_TEXT = "香港 REVENUE 下降 MCV 客群 收缩 与 银保 渠道 调整。"
SECRET_TEXT = "香港 REVENUE 下降 背后 高管 PR 评级 SECRET_TOKEN 讨论。"
QUERY = "香港 REVENUE 下降 MCV 客群 收缩"


def _seed(store: ChunkStore) -> NarrativeIndex:
    index = NarrativeIndex(store)
    index.ingest(NORMAL_TEXT, DocumentMeta(doc_id="HK_QBR.pptx", entity="ACME_HK"))
    index.ingest(
        SECRET_TEXT,
        DocumentMeta(doc_id="EXCO.pptx", entity="ACME_HK", sensitivity="RESTRICTED"),
    )
    return index


def test_postprocessor_chain_never_surfaces_restricted(tmp_path):
    """真索引集成：挂 MMR+LITM+压缩链后，输出仍无任何 RESTRICTED（隔离继承自出口）。"""
    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    try:
        index = _seed(store)
        chain = make_postprocessor("mmr,lost_in_middle,compress")
        retriever = NarrativeIndexRetriever(index, postprocessor=chain)
        out = retriever.retrieve(QUERY)

        assert out, "普通块应被召回，输出非空"
        assert all(str(s.get("sensitivity")).upper() != RESTRICTED_SENSITIVITY for s in out)
        assert all("SECRET_TOKEN" not in str(s.get("text", "")) for s in out)
        assert all("SECRET_TOKEN" not in str(s.get(PROMPT_TEXT_KEY, "")) for s in out)
        assert all(s.get("doc_id") != "EXCO.pptx" for s in out)

        # reverse-proof：RESTRICTED 块确在库中——输出干净是出口剔除之功，非数据缺失。
        stored = store.iter_chunks(doc_id="EXCO.pptx", include_inactive=True)
        assert stored and any("SECRET_TOKEN" in c.text for c in stored)
    finally:
        store.close()


def test_build_narrative_retriever_postprocessor_seam_inherits_isolation(tmp_path):
    """经 build_narrative_retriever 的 postprocessor= 缝挂链，RESTRICTED 同样不出域。"""
    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    try:
        _seed(store)
        store.close()
        retriever, store2 = build_narrative_retriever(
            tmp_path / "chunks.db", postprocessor=make_postprocessor("mmr")
        )
        try:
            out = retriever.retrieve(QUERY)
            assert out
            assert all(s.get("doc_id") != "EXCO.pptx" for s in out)
            assert all("SECRET_TOKEN" not in str(s.get("text", "")) for s in out)
        finally:
            store2.close()
    finally:
        pass


def test_reverse_proof_postprocessors_do_not_self_strip_restricted():
    """reverse-proof：直接喂 RESTRICTED 片段，postprocessor 不自带剔除（保护在上游出口）。"""
    restricted = {
        "text": SECRET_TEXT,
        "chunk_id": "x",
        "doc_id": "EXCO.pptx",
        "sensitivity": RESTRICTED_SENSITIVITY,
    }
    for pp in (MMRPostprocessor(), LostInTheMiddlePostprocessor()):
        out = pp.postprocess(QUERY, [restricted])
        assert any("SECRET_TOKEN" in str(s.get("text", "")) for s in out), (
            "postprocessor 应原样透传（不自带隔离）——证明断言有牙"
        )
    comp = CompressionPostprocessor(threshold=0.0).postprocess(QUERY, [restricted])
    assert "SECRET_TOKEN" in str(comp[0].get(PROMPT_TEXT_KEY, "")), (
        "压缩对直接喂入的 RESTRICTED 会写进 prompt_text——证明保护在上游出口而非压缩器"
    )
