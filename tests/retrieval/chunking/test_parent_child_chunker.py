"""父子（small-to-big）分段预设（批次 2.2，Chunker 缝 opt-in 实现）单测（TDD）。

钉死合约：
- 索引粒度=细 child：child text 仍是原文子串（citation 诚实），且带 parent_id/heading。
- 展开粒度=parent 小节：每个 child 带 window_text=父小节全文（合成时展开，检索/生成上下文解耦）。
- provenance：每 child 带非空 doc_id + source_locator；parent_locator 指向父小节【真实】段落跨度
  （'{prefix}#para{起}-{止}'），绝不臆造。
- 同一父小节下的 child 共享 parent_id，window_text/parent_locator 一致。
- 参数校验 / 空文本 与 chunk_document 一致；确定性（同输入两次逐位一致）。
- 经 make_chunker('parent_child'/'small_to_big') opt-in；默认仍 DefaultChunker（字节不变，另测）。
- 对齐 laws/qa/book 预设的接口形状（Chunker Protocol、LayoutAwareChunker 子类）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunker import Chunker, make_chunker
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.chunking.domain_presets import ParentChildChunker
from ragspine.retrieval.chunking.layout_chunker import (
    LayoutAwareChunker,
    group_children_by_parent,
)

# 两个 markdown 标题小节；每节多段（供 small-to-big 展开验证）。
_TEXT = "# 甲节\n甲段一。\n甲段二。\n# 乙节\n乙段一。\n乙段二。"


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


def test_is_runtime_checkable_and_is_layout_subclass():
    """对齐接口形状：满足 Chunker Protocol，且是 LayoutAwareChunker 子类（预设家族成员）。"""
    chunker = ParentChildChunker()
    assert isinstance(chunker, Chunker)
    assert isinstance(chunker, LayoutAwareChunker)


def test_child_text_is_original_substring():
    """每 child text 都是原文连续子串（citation 诚实，同既有切块契约）。"""
    for c in ParentChildChunker().chunk(_TEXT, _meta()):
        assert c.text in _TEXT


def test_children_carry_parent_window_and_real_parent_locator():
    """每 child 带 window_text=父小节全文 + parent_locator=父小节真实段落跨度。"""
    chunks = ParentChildChunker().chunk(_TEXT, _meta())
    # 甲节含全局段 1-3（# 甲节 / 甲段一 / 甲段二）；乙节含 4-6。
    for c in chunks:
        assert c.window_text  # 非空父上下文
        assert c.text in c.window_text  # child ⊆ parent 窗口（small-to-big）
        assert c.parent_locator  # 非空真实 parent locator
        assert c.parent_locator.startswith("report.pdf#para")
    # 甲节的 child：window_text 覆盖整节、parent_locator 指向 para1-3。
    jia = [c for c in chunks if c.heading == "# 甲节"]
    assert jia, "应有归属甲节的 child"
    for c in jia:
        assert c.parent_locator == "report.pdf#para1-3"
        assert "甲段一。" in c.window_text and "甲段二。" in c.window_text


def test_siblings_share_parent_id_and_context():
    """同父小节下的 child 共享 parent_id / window_text / parent_locator。"""
    chunks = ParentChildChunker().chunk(_TEXT, _meta())
    groups = group_children_by_parent(chunks)
    for _pid, siblings in groups.items():
        windows = {c.window_text for c in siblings}
        locators = {c.parent_locator for c in siblings}
        assert len(windows) == 1, "同父 child 的 window_text 必须一致"
        assert len(locators) == 1, "同父 child 的 parent_locator 必须一致"


def test_provenance_locator_present_and_honest():
    """每 child 带非空 doc_id + source_locator（citation 回指自身段落）。"""
    for c in ParentChildChunker().chunk(_TEXT, _meta()):
        assert c.doc_id == "report.pdf"
        assert c.source_locator.startswith("report.pdf#para")


def test_fine_child_granularity_splits_within_section():
    """小 max_chars → 小节内切成多个共享同一 parent 的细 child（small-to-big 精度）。"""
    text = "# 节\n" + "\n".join(f"段{i}。" for i in range(1, 6))
    chunks = ParentChildChunker().chunk(text, _meta(), max_chars=6, overlap_chars=0)
    assert len(chunks) > 1
    # 全部同一 parent（同小节），parent_locator 覆盖整节。
    assert len({c.parent_id for c in chunks}) == 1
    assert len({c.parent_locator for c in chunks}) == 1


def test_empty_text_returns_empty():
    assert ParentChildChunker().chunk("   \n  ", _meta()) == []


def test_invalid_params_raise():
    with pytest.raises(ValueError):
        ParentChildChunker().chunk(_TEXT, _meta(), max_chars=0)
    with pytest.raises(ValueError):
        ParentChildChunker().chunk(_TEXT, _meta(), max_chars=100, overlap_chars=100)


def test_deterministic():
    a = ParentChildChunker().chunk(_TEXT, _meta())
    b = ParentChildChunker().chunk(_TEXT, _meta())
    assert a == b


def test_make_chunker_selects_parent_child():
    """经 make_chunker 的 parent_child / small_to_big 别名选用（含连字符大小写不敏感）。"""
    for spec in ("parent_child", "parent-child", "small_to_big", "small-to-big", "PARENT_CHILD"):
        assert isinstance(make_chunker(spec), ParentChildChunker)


def test_default_layout_unaffected_no_window_or_parent_locator():
    """基类 LayoutAwareChunker 不填 window_text/parent_locator（默认字节不变，隔离本预设影响）。"""
    for c in LayoutAwareChunker().chunk(_TEXT, _meta()):
        assert c.window_text == ""
        assert c.parent_locator == ""
