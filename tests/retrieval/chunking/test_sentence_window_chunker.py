"""sentence-window 切块策略（W10，Chunker 缝 opt-in 实现）单测（TDD 红 → 绿）。

钉死合约：
- 索引粒度=单句：每个句子成一块（检索粒度细），块 text 仍是原文子串（citation 诚实）。
- 合成粒度=句子窗口：每块带 window_text（±N 句上下文），供合成时展开（检索与生成上下文解耦）。
- provenance：每块带非空 doc_id + source_locator（locator 回指句子所属段落，1-based）。
- 参数校验 / 空文本 与 chunk_document 一致；确定性（同输入两次逐位一致）。
- 经 make_chunker('sentence_window') opt-in；默认仍 DefaultChunker（字节不变，另测）。

红：sentence_window_chunker 落地前 import 即 ModuleNotFoundError。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunker import Chunker, make_chunker
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.chunking.sentence_window_chunker import (
    DEFAULT_WINDOW_SIZE,
    SentenceWindowChunker,
)

# 段1：3 句；段2：2 句。共 5 句 → 5 块。
_TEXT = "甲句一。甲句二。甲句三。\n乙句一。乙句二。"


def _meta() -> DocumentMeta:
    return DocumentMeta(
        doc_id="report.pdf",
        title="t",
        topic="FIN",
        entity="ACME_HK",
        geography="HK",
        period="2025H1",
        language="zh",
        sensitivity="INTERNAL",
    )


def test_is_runtime_checkable():
    assert isinstance(SentenceWindowChunker(), Chunker)


def test_one_chunk_per_sentence():
    chunks = SentenceWindowChunker().chunk(_TEXT, _meta())
    assert [c.text for c in chunks] == [
        "甲句一。",
        "甲句二。",
        "甲句三。",
        "乙句一。",
        "乙句二。",
    ]


def test_chunk_text_is_original_substring():
    """每块 text 都是原文连续子串（citation 诚实，同既有切块契约）。"""
    for c in SentenceWindowChunker().chunk(_TEXT, _meta()):
        assert c.text in _TEXT


def test_window_text_expands_to_neighbors():
    """window_text = ±window 句窗口：含本句 + 邻句（合成时的富上下文）。"""
    chunks = SentenceWindowChunker(window_size=1).chunk(_TEXT, _meta())
    # 中间句（甲句二）的窗口应含前一句 + 本句 + 后一句。
    mid = chunks[1]
    assert mid.text == "甲句二。"
    assert "甲句一。" in mid.window_text
    assert "甲句二。" in mid.window_text
    assert "甲句三。" in mid.window_text
    # 窗口是本句的超集（解耦：检索粒度 < 生成上下文）。
    assert len(mid.window_text) > len(mid.text)


def test_window_size_zero_is_just_the_sentence():
    chunks = SentenceWindowChunker(window_size=0).chunk(_TEXT, _meta())
    for c in chunks:
        assert c.window_text == c.text


def test_provenance_locator_points_to_paragraph():
    """locator 回指句子所属段落（1-based）：段1 的句 → para1，段2 的句 → para2。"""
    chunks = SentenceWindowChunker().chunk(_TEXT, _meta())
    for c in chunks:
        assert c.doc_id == "report.pdf"
        assert c.source_locator
    assert chunks[0].para_start == chunks[0].para_end == 1
    assert chunks[3].para_start == chunks[3].para_end == 2
    assert chunks[0].source_locator == "report.pdf#para1"
    assert chunks[3].source_locator == "report.pdf#para2"


def test_empty_text_returns_empty():
    assert SentenceWindowChunker().chunk("   \n  ", _meta()) == []


def test_invalid_params_raise():
    with pytest.raises(ValueError):
        SentenceWindowChunker().chunk(_TEXT, _meta(), max_chars=0)
    with pytest.raises(ValueError):
        SentenceWindowChunker().chunk(_TEXT, _meta(), max_chars=100, overlap_chars=100)


def test_deterministic():
    a = SentenceWindowChunker().chunk(_TEXT, _meta())
    b = SentenceWindowChunker().chunk(_TEXT, _meta())
    assert a == b


def test_make_chunker_selects_sentence_window():
    chunker = make_chunker("sentence_window")
    assert isinstance(chunker, SentenceWindowChunker)
    # 别名与 kwargs 透传。
    assert isinstance(make_chunker("sentence-window", window_size=2), SentenceWindowChunker)
    assert make_chunker("sentence_window", window_size=2).window_size == 2
    assert DEFAULT_WINDOW_SIZE >= 1
