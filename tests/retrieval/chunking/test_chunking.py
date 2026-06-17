"""切块器测试（叙事通路检索侧，TDD 红色阶段）。

只验证外部行为：段落聚合到字符预算、相邻块重叠、超长段句切/硬切、source_locator
回指格式、元数据继承、中英混排、参数校验。零网络、零三方 tokenizer。

红色预期：chunk_document 因 stub raise NotImplementedError 而全部 FAIL。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunking import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    Chunk,
    DocumentMeta,
    chunk_document,
)


def _meta(**overrides) -> DocumentMeta:
    """构造一份带全量元数据的 DocumentMeta（可逐字段覆盖）。"""
    kwargs = dict(
        doc_id="doc1",
        title="2025 上半年财务表现",
        topic="FIN",
        entity="ACME_HK",
        geography="HK",
        period="2025H1",
        language="zh",
        sensitivity="INTERNAL",
        source_locator_prefix="",
    )
    kwargs.update(overrides)
    return DocumentMeta(**kwargs)


# 4 段各 100 字符的标准语料（重叠/预算测试用，长度可精确手算）。
_P1 = "A" * 100
_P2 = "B" * 100
_P3 = "C" * 100
_P4 = "D" * 100
_FOUR_PARAS = "\n".join([_P1, _P2, _P3, _P4])


# ===========================================================================
# 基本形态：空文本、短文本、元数据继承
# ===========================================================================

def test_empty_text_returns_empty():
    """空文本 / 纯空白文本 -> []。"""
    assert chunk_document("", _meta()) == []
    assert chunk_document("   \n  \n\t ", _meta()) == []


def test_short_text_single_chunk():
    """短于预算的文本 -> 恰好 1 个 Chunk，文本与段落定位齐全。"""
    chunks = chunk_document("香港 REVENUE 上半年增长强劲。", _meta())
    assert len(chunks) == 1
    c = chunks[0]
    assert isinstance(c, Chunk)
    assert c.text == "香港 REVENUE 上半年增长强劲。"
    assert c.seq == 0
    assert c.doc_id == "doc1"
    assert c.chunk_id.startswith("doc1")
    assert c.para_start == 1 and c.para_end == 1


def test_metadata_inherited_on_all_chunks():
    """每个块完整继承文档元数据。"""
    meta = _meta()
    chunks = chunk_document(_FOUR_PARAS, meta, max_chars=250, overlap_chars=0)
    assert len(chunks) > 1
    for c in chunks:
        assert c.title == meta.title
        assert c.topic == meta.topic
        assert c.entity == meta.entity
        assert c.geography == meta.geography
        assert c.period == meta.period
        assert c.language == meta.language
        assert c.sensitivity == meta.sensitivity


# ===========================================================================
# 段落聚合 + 预算 + 重叠
# ===========================================================================

def test_paragraph_aggregation_respects_budget():
    """段落贪心聚合，所有块长度 <= max_chars。"""
    chunks = chunk_document(_FOUR_PARAS, _meta(), max_chars=250, overlap_chars=0)
    assert len(chunks) > 1
    assert all(len(c.text) <= 250 for c in chunks)


def test_adjacent_chunks_overlap():
    """重叠回带：4 段 x100 字、预算 250、重叠 120 -> 3 块，相邻块共享一个段落。"""
    chunks = chunk_document(_FOUR_PARAS, _meta(), max_chars=250, overlap_chars=120)
    assert [c.text for c in chunks] == [
        f"{_P1}\n{_P2}",
        f"{_P2}\n{_P3}",
        f"{_P3}\n{_P4}",
    ]
    assert [(c.para_start, c.para_end) for c in chunks] == [(1, 2), (2, 3), (3, 4)]


def test_zero_overlap_param():
    """overlap_chars=0 -> 块间无共享段落。"""
    chunks = chunk_document(_FOUR_PARAS, _meta(), max_chars=250, overlap_chars=0)
    assert [c.text for c in chunks] == [f"{_P1}\n{_P2}", f"{_P3}\n{_P4}"]
    assert [(c.para_start, c.para_end) for c in chunks] == [(1, 2), (3, 4)]


# ===========================================================================
# source_locator 回指
# ===========================================================================

def test_source_locator_format():
    """locator 格式：单段 'doc1#para1'，跨段 'doc1#para1-2'。"""
    single = chunk_document("只有一段。", _meta())
    assert single[0].source_locator == "doc1#para1"

    multi = chunk_document(_FOUR_PARAS, _meta(), max_chars=250, overlap_chars=0)
    assert multi[0].source_locator == "doc1#para1-2"
    assert multi[1].source_locator == "doc1#para3-4"


def test_source_locator_prefix_override():
    """显式 source_locator_prefix 优先于 doc_id。"""
    meta = _meta(source_locator_prefix="report.pptx!slide3")
    chunks = chunk_document("一段文本。", meta)
    assert chunks[0].source_locator == "report.pptx!slide3#para1"


# ===========================================================================
# 超长单段：句切 / 硬切
# ===========================================================================

def test_oversized_paragraph_split_by_sentence():
    """超长单段按句末标点切分，子块均 <= 预算、locator 都指向该段、拼接还原原文。"""
    para = ("X" * 59 + "。") * 20  # 1200 字符、20 句
    chunks = chunk_document(para, _meta(), max_chars=480, overlap_chars=0)
    assert len(chunks) == 3
    assert all(len(c.text) <= 480 for c in chunks)
    assert all(c.source_locator == "doc1#para1" for c in chunks)
    assert "".join(c.text for c in chunks) == para


def test_hard_cut_when_no_sentence_boundary():
    """无句末标点的超长连续串按 max_chars 硬切，拼接还原原文。"""
    para = "Z" * 1000
    chunks = chunk_document(para, _meta(), max_chars=400, overlap_chars=0)
    assert [len(c.text) for c in chunks] == [400, 400, 200]
    assert "".join(c.text for c in chunks) == para


# ===========================================================================
# 中英混排 / 编号 / 默认参数 / 参数校验
# ===========================================================================

def test_mixed_cjk_english_content_preserved():
    """中英混排正常切块，每个段落文本都至少出现在一个块里。"""
    paras = [
        "Nexora 事件对银保渠道的影响仍在评估中。",
        "MPFA released new disclosure requirements for MPF fees.",
        "香港 REVENUE grew strongly，营收持续增长。",
        "CPL attribution analysis 显示银保渠道贡献上升。",
    ]
    chunks = chunk_document("\n".join(paras), _meta(), max_chars=80, overlap_chars=0)
    joined = "\n".join(c.text for c in chunks)
    assert all(p in joined for p in paras)


def test_chunk_ids_unique_and_sequential():
    """chunk_id 唯一、seq 从 0 连续递增。"""
    chunks = chunk_document(_FOUR_PARAS, _meta(), max_chars=120, overlap_chars=0)
    assert [c.seq for c in chunks] == list(range(len(chunks)))
    ids = [c.chunk_id for c in chunks]
    assert len(set(ids)) == len(ids)
    assert all(i.startswith("doc1") for i in ids)


def test_default_budget_applied():
    """默认参数：预算在拍板的 400-600 区间内，长文本被切成多块且都不超预算。"""
    assert 400 <= DEFAULT_CHUNK_CHARS <= 600
    assert 0 < DEFAULT_OVERLAP_CHARS < DEFAULT_CHUNK_CHARS
    text = "\n".join("段落内容" * 30 for _ in range(20))  # 每段 120 字 x20 段
    chunks = chunk_document(text, _meta())
    assert len(chunks) > 1
    assert all(len(c.text) <= DEFAULT_CHUNK_CHARS for c in chunks)


def test_invalid_params_raise():
    """非法参数：max_chars<=0 / overlap<0 / overlap>=max_chars -> ValueError。"""
    with pytest.raises(ValueError):
        chunk_document("文本", _meta(), max_chars=0)
    with pytest.raises(ValueError):
        chunk_document("文本", _meta(), max_chars=100, overlap_chars=-1)
    with pytest.raises(ValueError):
        chunk_document("文本", _meta(), max_chars=100, overlap_chars=100)
