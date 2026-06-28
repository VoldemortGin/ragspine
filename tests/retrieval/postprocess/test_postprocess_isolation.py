"""W8 隔离 conformance：PostprocessingRetriever 的 RESTRICTED 隔离【继承】自被包裹的 base。

拍板（docs/invariants.md「RESTRICTED isolation 两出口」）：link 出口（NarrativeIndexRetriever）在出口
剔除 sensitivity==RESTRICTED 的块。postprocessor 链只对 base.retrieve(...) 的输出做子集/重排（MMR
去重 / lost-in-the-middle 重排 / 抽取式压缩 trim text），绝不自行造片段、绝不直接读块库——故链输出
恒为 base 输出的子集，RESTRICTED 永不出域。

本文件用【真 NarrativeIndex over ChunkStore】做集成证明：种入普通块 + RESTRICTED 块（其文本同样命中
查询），经 PostprocessingRetriever（推荐三段链）检索后断言输出无任何 RESTRICTED。reverse-proof：直接读
块库证明 RESTRICTED 块（含 SECRET_TOKEN）确实在库中——输出干净是因 base 剔除，而非数据缺失（能抓住
「postprocessor 绕过 base 直接读块库」的回归）。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever
from ragspine.retrieval.postprocess.chain import (
    RESTRICTED_SENSITIVITY,
    make_postprocessing_retriever,
)

# 普通块：与查询强重叠（保证召回非空、压缩后仍留句）。
NORMAL_TEXT = "香港 REVENUE 下降 MCV 客群 收缩 与 银保 渠道 调整。"
# RESTRICTED 块：同样含查询词（若不剔除会被 BM25 召回）。
SECRET_TEXT = "香港 REVENUE 下降 背后 的 高管 PR 评级 SECRET_TOKEN 讨论。"
QUERY = "香港 REVENUE 下降 MCV 客群 收缩"


def test_postprocess_inherits_restricted_isolation_from_base(tmp_path):
    """真索引集成：链输出无任何 RESTRICTED 块（隔离继承自 base 出口）。"""
    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    try:
        index = NarrativeIndex(store)
        index.ingest(NORMAL_TEXT, DocumentMeta(doc_id="HK_QBR.pptx", entity="ACME_HK"))
        index.ingest(
            SECRET_TEXT,
            DocumentMeta(doc_id="EXCO.pptx", entity="ACME_HK", sensitivity="RESTRICTED"),
        )
        base = NarrativeIndexRetriever(index)
        # 推荐三段链（MMR 去重 + 抽取式压缩 + lost-in-the-middle 重排），全确定性。
        wrapped = make_postprocessing_retriever(base, "recommended")
        out = wrapped.retrieve(QUERY)

        assert out, "普通块应被召回（输出非空）"
        assert all(
            str(s.get("sensitivity")).upper() != RESTRICTED_SENSITIVITY for s in out
        )
        assert all("SECRET_TOKEN" not in str(s.get("text", "")) for s in out)
        assert all(s.get("doc_id") != "EXCO.pptx" for s in out)

        # reverse-proof：RESTRICTED 块（含 SECRET_TOKEN）确实在块库中——输出干净是 base 剔除之功，
        # 而非数据本就不存在。若回归让 postprocessor 绕过 base 直接读块库，本断言抓住泄漏。
        stored = store.iter_chunks(doc_id="EXCO.pptx", include_inactive=True)
        assert stored, "RESTRICTED 块应已落库"
        assert any("SECRET_TOKEN" in c.text for c in stored)
        assert any(c.sensitivity.upper() == RESTRICTED_SENSITIVITY for c in stored)
    finally:
        store.close()
