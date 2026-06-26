"""Contextual retrieval（W4a）测试：确定性 context 头 + 索引文本缝（TDD 红色阶段）。

只验证对外行为：
    - build_context_header 从 chunk 既有受控元数据拼【确定性】头，跳过空字段、全空 -> ''；
      值全部来自元数据（零编造）。
    - contextual_index_text 把头拼到【索引文本】前，chunk.text（citation 原文）原样不动。
    - HybridRetriever/NarrativeIndex 注入 index_text_fn 后，context 头进入 BM25 索引（能被
      只命中头的 query 召回）；默认 index_text_fn=None 时逐位等价旧行为（头不进索引）。
    - make_index_text_fn 工厂按 spec/env 选用（范式同 make_chunker）。

红色预期：ragspine.retrieval.contextual 尚不存在 / HybridRetriever 无 index_text_fn 参数。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import Chunk, DocumentMeta
from ragspine.retrieval.contextual import (
    CONTEXTUAL_ENV,
    build_context_header,
    contextual_index_text,
    make_index_text_fn,
)
from ragspine.retrieval.lexical.retrieval import HybridRetriever, NarrativeIndex


def _chunk(seq: int, text: str, **md) -> Chunk:
    base = dict(
        chunk_id=f"d#c{seq}",
        doc_id="d",
        seq=seq,
        text=text,
        source_locator=f"d#para{seq + 1}",
        para_start=seq + 1,
        para_end=seq + 1,
        title="",
        entity="",
        period="",
    )
    base.update(md)
    return Chunk(**base)


# ===========================================================================
# build_context_header：确定性、跳过空字段、零编造
# ===========================================================================

def test_build_context_header_from_metadata():
    """头由 title/entity/period 拼成，标签固定、顺序确定、只含元数据值。"""
    c = _chunk(0, "正文", title="2025上半年财务", entity="ACME_HK", period="2025H1")
    assert build_context_header(c) == "[文档:2025上半年财务 · 实体:ACME_HK · 期间:2025H1]"


def test_build_context_header_skips_empty_fields():
    """空字段不进头（entity 空时不出现 '实体:' 段）。"""
    c = _chunk(0, "正文", title="T", entity="", period="2025H1")
    assert build_context_header(c) == "[文档:T · 期间:2025H1]"


def test_build_context_header_includes_heading_when_present():
    """有 heading（章节）时纳入头尾。"""
    c = _chunk(0, "正文", title="T", heading="收入分析")
    assert build_context_header(c) == "[文档:T · 章节:收入分析]"


def test_build_context_header_empty_when_no_metadata():
    """所有受控字段皆空 -> 空头（调用方据此回退原文，不拼空括号）。"""
    c = _chunk(0, "正文")
    assert build_context_header(c) == ""


# ===========================================================================
# contextual_index_text：头进索引文本，原文不被污染
# ===========================================================================

def test_contextual_index_text_prepends_header_keeps_text_pure():
    """索引文本 = 头 + 换行 + 原文；chunk.text 原样不动（citation 不污染）。"""
    c = _chunk(0, "香港营收增长。", title="T", entity="ACME_HK", period="2025H1")
    idx = contextual_index_text(c)
    assert idx.startswith("[文档:T")
    assert idx.endswith("\n香港营收增长。")
    assert c.text == "香港营收增长。"  # 原文未被改动


def test_contextual_index_text_no_header_returns_text():
    """无可用元数据 -> 索引文本就是原文（不加任何前缀）。"""
    c = _chunk(0, "纯文本")
    assert contextual_index_text(c) == "纯文本"


# ===========================================================================
# HybridRetriever 注入：context 头进 BM25 索引（opt-in），默认不进
# ===========================================================================

def _two_chunks() -> list[Chunk]:
    # chunk 正文都【不含】实体代码；代码只活在元数据里。
    return [
        _chunk(0, "本季度区域表现稳健。", title="财报", entity="ACME_HK", period="2025H1"),
        _chunk(1, "其他无关内容段落。", title="财报", entity="OTHER", period="2024H1"),
    ]


def test_context_header_enters_index_when_opt_in():
    """注入 contextual_index_text：只命中头里实体代码的 query 能召回该块。"""
    chunks = _two_chunks()
    on = HybridRetriever(chunks, index_text_fn=contextual_index_text).search("ACME_HK")
    assert "d#c0" in [r.chunk.chunk_id for r in on]
    assert "d#c1" not in [r.chunk.chunk_id for r in on]


def test_default_retriever_does_not_index_context():
    """默认 index_text_fn=None：实体代码不进索引，纯正文 query 召回为空（逐位等价旧行为）。"""
    chunks = _two_chunks()
    off = HybridRetriever(chunks).search("ACME_HK")
    assert off == []
    # 显式传 None 与不传一致。
    assert HybridRetriever(chunks, index_text_fn=None).search("ACME_HK") == []


def test_narrative_index_contextual_through_store(tmp_path):
    """端到端：入库的受控元数据经 store 回来仍能拼头（title/entity/period 已落列），
    contextual 开则 'ACME_HK' 这类只在头里的 query 可召回；关则召回空。"""
    store = ChunkStore(tmp_path / "c.db")
    try:
        store.init_schema()
        meta = DocumentMeta(doc_id="d", title="财报", entity="ACME_HK", period="2025H1")
        on_index = NarrativeIndex(store, index_text_fn=contextual_index_text)
        on_index.ingest("本季度区域表现稳健。", meta)
        res = on_index.retrieve("ACME_HK", rerank=False)
        assert res, "contextual 开：只在头里的实体代码应可召回"

        off_index = NarrativeIndex(store)  # 同库，默认不索引头
        assert off_index.retrieve("ACME_HK", rerank=False) == []
    finally:
        store.close()


# ===========================================================================
# make_index_text_fn 工厂（spec/env 选用）
# ===========================================================================

def test_make_index_text_fn_none_returns_none():
    assert make_index_text_fn(None) is None
    assert make_index_text_fn("none") is None
    assert make_index_text_fn("  NONE ") is None


def test_make_index_text_fn_on_returns_contextual():
    assert make_index_text_fn("default") is contextual_index_text
    assert make_index_text_fn("deterministic") is contextual_index_text
    assert make_index_text_fn("on") is contextual_index_text


def test_make_index_text_fn_unknown_raises():
    with pytest.raises(ValueError) as exc:
        make_index_text_fn("definitely-not-a-strategy")
    assert "definitely-not-a-strategy" in str(exc.value)


def test_make_index_text_fn_reads_env(monkeypatch):
    monkeypatch.setenv(CONTEXTUAL_ENV, "on")
    assert make_index_text_fn() is contextual_index_text
    monkeypatch.delenv(CONTEXTUAL_ENV, raising=False)
    assert make_index_text_fn() is None
