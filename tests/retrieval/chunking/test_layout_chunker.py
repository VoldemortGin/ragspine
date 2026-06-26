"""布局感知 + 父子切块（W4b）测试：LayoutAwareChunker（TDD 红色阶段）。

只验证对外行为：
    - is_heading 启发式：markdown # / 编号 / 章节关键字 / 短无标点行 = 标题；长句非标题。
    - 在标题边界切：预算够也不跨标题合并；每块带 heading（小节标题）。
    - 父子：同一小节的多个子块共享 parent_id；不同小节不同 parent_id；group_children_by_parent
      归组（small-to-big 父句柄）。
    - provenance：块 text 仍是原文段落连接（子串契约），locator/para 用【全局】段号。
    - 经 make_chunker('layout'/'parent_child') 选用；默认 DefaultChunker 行为不受影响。

红色预期：ragspine.retrieval.chunking.layout_chunker 尚不存在，import 即 collection ERROR。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunker import make_chunker
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.chunking.layout_chunker import (
    LayoutAwareChunker,
    group_children_by_parent,
    is_heading,
)


def _meta(**overrides) -> DocumentMeta:
    kwargs = dict(
        doc_id="doc1",
        title="2025 财务表现",
        topic="FIN",
        entity="ACME_HK",
        geography="HK",
        period="2025H1",
        language="zh",
        sensitivity="INTERNAL",
    )
    kwargs.update(overrides)
    return DocumentMeta(**kwargs)


# ===========================================================================
# is_heading 启发式
# ===========================================================================

def test_is_heading_structural_signals():
    assert is_heading("# 一级标题")
    assert is_heading("## 1.2 收入分析")
    assert is_heading("第三章 风险因素")
    assert is_heading("1. 概述")
    assert is_heading("一、背景")


def test_is_heading_short_no_punctuation():
    assert is_heading("收入概览")  # 短、无标点 -> 标题型


def test_is_heading_rejects_sentences_and_blank():
    assert not is_heading(
        "本季度香港地区营收同比增长，主要由银保渠道驱动。"
    )  # 长 + 含标点
    assert not is_heading("营收同比增长，渠道贡献上升。")  # 含逗号/句号
    assert not is_heading("")
    assert not is_heading("   ")


# ===========================================================================
# 标题边界切分 + heading 记录
# ===========================================================================

def test_chunks_on_heading_boundary_not_merging_across():
    """预算 480 足以把四段合一；布局感知须按标题切成 2 节，各不跨标题。"""
    text = "\n".join(
        ["# 收入", "香港收入同比增长强劲。", "# 成本", "运营成本保持稳定。"]
    )
    chunks = LayoutAwareChunker().chunk(text, _meta(), max_chars=480, overlap_chars=0)
    assert [c.text for c in chunks] == [
        "# 收入\n香港收入同比增长强劲。",
        "# 成本\n运营成本保持稳定。",
    ]
    assert [c.heading for c in chunks] == ["# 收入", "# 成本"]
    assert chunks[0].parent_id != chunks[1].parent_id


def test_preamble_before_first_heading_is_own_section():
    """首个标题前的前导段自成一节（heading 为空）。"""
    text = "\n".join(["前言段落无标题。", "# 正文", "正文内容一段。"])
    chunks = LayoutAwareChunker().chunk(text, _meta(), max_chars=480, overlap_chars=0)
    assert chunks[0].heading == ""
    assert chunks[0].text == "前言段落无标题。"
    assert chunks[1].heading == "# 正文"


# ===========================================================================
# 父子：parent_id 归组
# ===========================================================================

def test_section_children_share_parent_id():
    """一节内被预算切成多块，子块共享同一 parent_id；group 归组拿回兄弟全集。"""
    text = "\n".join(["# 财务概览", "甲" * 100, "乙" * 100, "丙" * 100, "丁" * 100])
    chunks = LayoutAwareChunker().chunk(text, _meta(), max_chars=250, overlap_chars=0)
    assert len(chunks) > 1
    assert len({c.parent_id for c in chunks}) == 1
    assert all(c.heading == "# 财务概览" for c in chunks)
    groups = group_children_by_parent(chunks)
    assert len(groups) == 1
    assert len(next(iter(groups.values()))) == len(chunks)


def test_distinct_sections_distinct_parents():
    text = "\n".join(["# A", "甲" * 50, "# B", "乙" * 50])
    chunks = LayoutAwareChunker().chunk(text, _meta(), max_chars=480, overlap_chars=0)
    assert len(group_children_by_parent(chunks)) == 2


# ===========================================================================
# provenance：全局段号、子串契约
# ===========================================================================

def test_locators_use_global_paragraph_numbers():
    """跨小节 locator 用【全局】段号，不是小节内重置（citation 诚实）。"""
    text = "\n".join(["# A", "甲" * 50, "# B", "乙" * 50])
    chunks = LayoutAwareChunker().chunk(text, _meta(), max_chars=480, overlap_chars=0)
    assert chunks[0].source_locator == "doc1#para1-2"
    assert chunks[1].source_locator == "doc1#para3-4"
    assert (chunks[0].para_start, chunks[0].para_end) == (1, 2)
    assert (chunks[1].para_start, chunks[1].para_end) == (3, 4)


def test_chunk_text_is_paragraph_join_substring():
    text = "\n".join(["# 标题", "第一段内容。", "第二段内容。"])
    chunks = LayoutAwareChunker().chunk(text, _meta(), max_chars=480, overlap_chars=0)
    assert chunks[0].text == "# 标题\n第一段内容。\n第二段内容。"


def test_no_heading_single_section_degenerate():
    """无任何标题 -> 单节（heading 空、parent_id=doc#s0），预算贪心如常。"""
    chunks = LayoutAwareChunker().chunk(
        "\n".join(["甲" * 100, "乙" * 100]), _meta(), max_chars=250, overlap_chars=0
    )
    assert all(c.parent_id == "doc1#s0" for c in chunks)
    assert all(c.heading == "" for c in chunks)


def test_empty_text_returns_empty():
    assert LayoutAwareChunker().chunk("", _meta()) == []
    assert LayoutAwareChunker().chunk("   \n  \n", _meta()) == []


def test_param_validation_inherited():
    with pytest.raises(ValueError):
        LayoutAwareChunker().chunk("文本", _meta(), max_chars=0)


# ===========================================================================
# 经 make_chunker 选用
# ===========================================================================

def test_make_chunker_resolves_layout():
    assert isinstance(make_chunker("layout"), LayoutAwareChunker)
    assert isinstance(make_chunker("parent_child"), LayoutAwareChunker)
    assert isinstance(make_chunker("parent-child"), LayoutAwareChunker)
