"""W8 默认字节不变 + 压缩 prompt_text 由 agent 消费（默认仍字节不变）。

- postprocessor=None（默认）→ NarrativeIndexRetriever.retrieve 输出与不挂链逐字一致。
- agent._snippet_text 优先读 prompt_text（压缩产出送 prompt），缺省回落 text/content（默认不变）。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import _snippet_text
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever
from ragspine.retrieval.postprocess import PROMPT_TEXT_KEY

QUERY = "香港 REVENUE 下降 MCV 客群 收缩"


def test_default_none_postprocessor_is_byte_identical(tmp_path):
    """默认（postprocessor=None）与显式不挂链：retrieve 输出逐字一致。"""
    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    try:
        index = NarrativeIndex(store)
        index.ingest("香港 REVENUE 下降 MCV 客群 收缩 银保 渠道 调整。",
                     DocumentMeta(doc_id="A.pptx", entity="ACME_HK"))
        index.ingest("新单 价值 NBV 提升 与 产品 结构 优化。",
                     DocumentMeta(doc_id="B.pptx", entity="ACME_HK"))
        base = NarrativeIndexRetriever(index)
        with_none = NarrativeIndexRetriever(index, postprocessor=None)
        assert base.retrieve(QUERY) == with_none.retrieve(QUERY)
    finally:
        store.close()


def test_agent_snippet_text_prefers_prompt_text():
    """压缩产出 prompt_text 时 agent 用它送 prompt。"""
    s = {"text": "原文 很 长 的 一 大 段", PROMPT_TEXT_KEY: "压缩后 简短"}
    assert _snippet_text(s) == "压缩后 简短"


def test_agent_snippet_text_falls_back_to_text_default_unchanged():
    """无 prompt_text（默认）→ 回落 text/content，字节不变。"""
    assert _snippet_text({"text": "原文"}) == "原文"
    assert _snippet_text({"content": "内容"}) == "内容"
    assert _snippet_text({}) == ""
