"""W6b 隔离 conformance：CorrectiveRetriever 的 RESTRICTED 隔离【继承】自被包裹的 base。

拍板（docs/invariants.md「RESTRICTED isolation 两出口」）：link 出口（NarrativeIndexRetriever）
在出口剔除 sensitivity==RESTRICTED 的块。CorrectiveRetriever 只对 base.retrieve(...) 的输出打分/取舍，
绝不自行造片段、绝不直接读块库——故其输出恒为 base 输出的子集，RESTRICTED 永不出域。

本文件用【真 NarrativeIndex over ChunkStore】做集成证明（最有意义的那种）：种入一条普通块 + 一条
RESTRICTED 块（其文本同样命中查询），经 CorrectiveRetriever 检索后断言输出无任何 RESTRICTED。
reverse-proof：直接读块库证明 RESTRICTED 块（含 SECRET_TOKEN）确实在库中——输出干净是因 base 剔除，
而非数据缺失（能抓住「corrective 绕过 base 直接读块库」的回归）。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.corrective import RESTRICTED_SENSITIVITY, CorrectiveRetriever
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever

# 普通块：与查询强重叠（保证 grade 达标、召回非空）。
NORMAL_TEXT = "香港 REVENUE 下降 MCV 客群 收缩 与 银保 渠道 调整。"
# RESTRICTED 块：同样含「香港 REVENUE 下降」等查询词（若不剔除则会被 BM25 召回）。
SECRET_TEXT = "香港 REVENUE 下降 背后 的 高管 PR 评级 SECRET_TOKEN 讨论。"
QUERY = "香港 REVENUE 下降 MCV 客群 收缩"


def test_corrective_inherits_restricted_isolation_from_base(tmp_path):
    """真索引集成：CorrectiveRetriever 输出无任何 RESTRICTED 块（隔离继承自 base 出口）。"""
    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    try:
        index = NarrativeIndex(store)
        index.ingest(NORMAL_TEXT, DocumentMeta(doc_id="HK_QBR.pptx", entity="ACME_HK"))
        index.ingest(
            SECRET_TEXT,
            DocumentMeta(doc_id="EXCO.pptx", entity="ACME_HK", sensitivity="RESTRICTED"),
        )
        cr = CorrectiveRetriever(NarrativeIndexRetriever(index))
        out = cr.retrieve(QUERY)

        assert out, "普通块应被召回（grade 达标，输出非空）"
        assert all(
            str(s.get("sensitivity")).upper() != RESTRICTED_SENSITIVITY for s in out
        )
        assert all("SECRET_TOKEN" not in str(s.get("text", "")) for s in out)
        assert all(s.get("doc_id") != "EXCO.pptx" for s in out)

        # reverse-proof：RESTRICTED 块（含 SECRET_TOKEN）确实在块库中——输出干净是 base 剔除之功，
        # 而非数据本就不存在。若某回归让 corrective 绕过 base 直接读块库，本断言保证泄漏会被抓住。
        stored = store.iter_chunks(doc_id="EXCO.pptx", include_inactive=True)
        assert stored, "RESTRICTED 块应已落库"
        assert any("SECRET_TOKEN" in c.text for c in stored)
        assert any(c.sensitivity.upper() == RESTRICTED_SENSITIVITY for c in stored)
    finally:
        store.close()


def test_base_itself_already_strips_restricted(tmp_path):
    """前提自检：被包裹的 base（NarrativeIndexRetriever）本身即已剔除 RESTRICTED。

    这是「隔离继承」断言的根基——corrective 只是不破坏它。
    """
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
        base_out = base.retrieve(QUERY)
        assert base_out
        assert all(s.get("doc_id") != "EXCO.pptx" for s in base_out)
        assert all("SECRET_TOKEN" not in str(s.get("text", "")) for s in base_out)
    finally:
        store.close()
