"""W9 隔离 conformance：QueryTransformRetriever 的 RESTRICTED 隔离【继承】自被包裹的 base。

变换只对 base.retrieve(...) 的多查询输出做 RRF 融合取舍，绝不自行造片段、绝不直读块库——base
（NarrativeIndexRetriever）已在出口剔除 RESTRICTED，故融合输出恒为 base 输出的子集，RESTRICTED
永不出域。即使变换产出的每个子查询都强命中 RESTRICTED 块，也照样剔除。

reverse-proof：直接读块库证明 RESTRICTED 块（含 SECRET_TOKEN）确实在库中——输出干净是 base 剔除之功。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.query_transform import QueryTransformRetriever
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import RESTRICTED_SENSITIVITY, NarrativeIndexRetriever

NORMAL_TEXT = "香港 REVENUE 下降 MCV 客群 收缩 与 银保 渠道 调整。"
SECRET_TEXT = "香港 REVENUE 下降 背后 的 高管 PR 评级 SECRET_TOKEN 讨论。"


class MultiQueryFanout:
    """duck-typed QueryTransform：产出两个都命中 RESTRICTED 文本的子查询（最严苛的隔离压力）。"""

    def transform(self, query, *, reference_date=None):
        return ["香港 REVENUE 下降", "高管 PR 评级 讨论"]


def test_query_transform_inherits_restricted_isolation_from_base(tmp_path):
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
        wrapped = QueryTransformRetriever(base, MultiQueryFanout())
        out = wrapped.retrieve("香港 REVENUE 下降")

        assert out, "普通块应被召回（融合输出非空）"
        assert all(
            str(s.get("sensitivity")).upper() != RESTRICTED_SENSITIVITY for s in out
        )
        assert all("SECRET_TOKEN" not in str(s.get("text", "")) for s in out)
        assert all(s.get("doc_id") != "EXCO.pptx" for s in out)

        # reverse-proof：RESTRICTED 块确实在库中。
        stored = store.iter_chunks(doc_id="EXCO.pptx", include_inactive=True)
        assert stored and any("SECRET_TOKEN" in c.text for c in stored)
    finally:
        store.close()
