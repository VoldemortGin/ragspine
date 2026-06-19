"""Chunker 缝测试：Protocol + DefaultChunker 默认实现 + make_chunker 工厂（TDD 红色阶段）。

只验证缝的对外行为：
    - DefaultChunker.chunk 与 chunk_document【逐位等价】（零行为变化是本增量的头条）。
    - DefaultChunker 结构匹配 @runtime_checkable Chunker Protocol。
    - make_chunker 按 spec/env 解析（'default'/'recursive' -> DefaultChunker；None/'none' -> None；
      未知名 -> ValueError 列出可选名字），范式同 make_vector_store。

红色预期：ragspine.retrieval.chunking.chunker 尚不存在，import 即 collection ERROR。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunker import (
    CHUNKER_ENV,
    Chunker,
    DefaultChunker,
    make_chunker,
)
from ragspine.retrieval.chunking.chunking import DocumentMeta, chunk_document

_CORPUS = "\n".join(["甲" * 100, "乙" * 100, "丙" * 100, "丁" * 100])


def _meta() -> DocumentMeta:
    return DocumentMeta(
        doc_id="doc1",
        title="t",
        topic="FIN",
        entity="ACME_HK",
        geography="HK",
        period="2025H1",
        language="zh",
        sensitivity="INTERNAL",
    )


def test_default_chunker_is_runtime_checkable():
    """DefaultChunker 结构匹配 @runtime_checkable Chunker Protocol。"""
    assert isinstance(DefaultChunker(), Chunker)


def test_default_chunker_byte_identical_to_chunk_document():
    """零行为变化：DefaultChunker.chunk 与 chunk_document 对同一输入产出【逐位一致】。"""
    by_class = DefaultChunker().chunk(_CORPUS, _meta(), max_chars=250, overlap_chars=120)
    by_func = chunk_document(_CORPUS, _meta(), max_chars=250, overlap_chars=120)
    assert by_class == by_func


def test_default_chunker_default_params_match():
    """缺省参数（不传 max_chars / overlap_chars）也与 chunk_document 默认行为逐位一致。"""
    text = "\n".join("段落内容" * 30 for _ in range(20))
    assert DefaultChunker().chunk(text, _meta()) == chunk_document(text, _meta())


def test_make_chunker_resolves_default():
    """make_chunker('default') / 'recursive' -> DefaultChunker 实例。"""
    assert isinstance(make_chunker("default"), DefaultChunker)
    assert isinstance(make_chunker("recursive"), DefaultChunker)


def test_make_chunker_none_returns_none():
    """None / 'none'（大小写不敏感）-> None（调用方回退到内置 chunk_document 默认）。"""
    assert make_chunker(None) is None
    assert make_chunker("none") is None
    assert make_chunker("  NONE ") is None


def test_make_chunker_unknown_raises_valueerror():
    """未知 spec -> ValueError，报错列出该 spec。"""
    with pytest.raises(ValueError) as exc:
        make_chunker("definitely-not-a-chunker")
    assert "definitely-not-a-chunker" in str(exc.value)


def test_make_chunker_reads_env(monkeypatch):
    """缺省 spec 时读环境变量 RAGSPINE_CHUNKER（范式同 make_vector_store）。"""
    monkeypatch.setenv(CHUNKER_ENV, "default")
    assert isinstance(make_chunker(), DefaultChunker)
