"""领域切块预设（Item ⑩）测试：LawsChunker / BookChunker / QaChunker（结构式断言）。

只验证对外行为（镜像 test_layout_chunker.py 风格，断 .text/.heading/.parent_id/.source_locator）：
    - laws：每个「第N条」自成小节（独立 parent_id、条款行为 heading），条内短实质行不误切；
      与基座 LayoutAwareChunker 对照证明预设调优了谓词。
    - book：markdown / 「第N章」/ 编号标题起小节；散文短行不被当作章节标题（对照基座会误切）。
    - qa：每个问句起小节，答案段落随之同块共享 parent_id；问答对成对；问：前缀亦识别；长答案预算
      切分后仍共享同一 parent_id。
    - 工厂：make_chunker 解析三预设及别名，均为 Chunker。
    - 确定性 + 基座重构后逐位不变。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunker import Chunker, make_chunker
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.chunking.domain_presets import (
    BookChunker,
    LawsChunker,
    QaChunker,
)
from ragspine.retrieval.chunking.layout_chunker import LayoutAwareChunker


def _meta(**overrides) -> DocumentMeta:
    kwargs = dict(
        doc_id="doc1",
        title="示例文档",
        topic="LEGAL",
        entity="ACME_HK",
        geography="HK",
        period="2025",
        language="zh",
        sensitivity="INTERNAL",
    )
    kwargs.update(overrides)
    return DocumentMeta(**kwargs)


# ===========================================================================
# LawsChunker：条款层级
# ===========================================================================

_LAWS_TEXT = "\n".join(
    [
        "第一章 总则",
        "第一条 为了保护个人信息权益，制定本法。",
        "本条明确立法目的",  # 短、无标点的实质性行（非标题）
        "第二条 本法所称个人信息是指相关信息。",
        "处理者应履行义务。",
    ]
)


def test_laws_groups_by_clause():
    """每个第N条自成小节：heading = 条款行，各条独立 parent_id。"""
    chunks = LawsChunker().chunk(_LAWS_TEXT, _meta(), max_chars=480, overlap_chars=0)
    assert [c.heading for c in chunks] == [
        "第一章 总则",
        "第一条 为了保护个人信息权益，制定本法。",
        "第二条 本法所称个人信息是指相关信息。",
    ]
    # 三节三 parent_id，各不相同。
    assert len({c.parent_id for c in chunks}) == 3


def test_laws_clause_text_is_paragraph_join_substring():
    """条内正文随条款同块：text 为原文段落连接（子串契约）。"""
    chunks = LawsChunker().chunk(_LAWS_TEXT, _meta(), max_chars=480, overlap_chars=0)
    clause1 = next(c for c in chunks if c.heading.startswith("第一条"))
    assert (
        clause1.text
        == "第一条 为了保护个人信息权益，制定本法。\n本条明确立法目的"
    )


def test_laws_short_substantive_line_not_split_out():
    """条内的短实质性行【不】被切成独立小节（laws 关掉了通用短行启发式）。"""
    chunks = LawsChunker().chunk(_LAWS_TEXT, _meta(), max_chars=480, overlap_chars=0)
    assert all(c.heading != "本条明确立法目的" for c in chunks)


def test_laws_provenance_complete():
    """每块带非空 doc_id + source_locator。"""
    chunks = LawsChunker().chunk(_LAWS_TEXT, _meta(), max_chars=480, overlap_chars=0)
    assert chunks
    for c in chunks:
        assert c.doc_id == "doc1"
        assert c.source_locator


def test_laws_recognizes_clause_where_base_does_not():
    """对照：laws 认第N条为边界，基座 LayoutAwareChunker 不认（第N条既非章节亦非短无标点行）。"""
    laws = LawsChunker().chunk(_LAWS_TEXT, _meta(), max_chars=480, overlap_chars=0)
    base = LayoutAwareChunker().chunk(_LAWS_TEXT, _meta(), max_chars=480, overlap_chars=0)
    assert any(c.heading.startswith("第二条") for c in laws)
    assert not any(c.heading.startswith("第二条") for c in base)


# ===========================================================================
# BookChunker：章节层级
# ===========================================================================

_BOOK_TEXT = "\n".join(
    [
        "# 前言",
        "这是前言的正文段落，交代背景。",
        "第一章 启程",
        "少年抬头望天",  # 短散文行，无标点（非章节标题）
        "1. 引子",
        "第二章 归来",
        "故事到此结束。",
    ]
)


def test_book_chapter_hierarchy():
    """markdown / 第N章 / 编号标题起小节；散文行随其后。"""
    chunks = BookChunker().chunk(_BOOK_TEXT, _meta(), max_chars=480, overlap_chars=0)
    assert [c.heading for c in chunks] == [
        "# 前言",
        "第一章 启程",
        "1. 引子",
        "第二章 归来",
    ]


def test_book_short_prose_line_not_a_heading():
    """散文短行【不】被 BookChunker 当作章节标题（对照基座 LayoutAwareChunker 会误切）。"""
    book = BookChunker().chunk(_BOOK_TEXT, _meta(), max_chars=480, overlap_chars=0)
    base = LayoutAwareChunker().chunk(_BOOK_TEXT, _meta(), max_chars=480, overlap_chars=0)
    assert all(c.heading != "少年抬头望天" for c in book)
    # 基座启用短无标点行启发式，会把该散文行误切成独立小节。
    assert any(c.heading == "少年抬头望天" for c in base)


def test_book_prose_stays_within_chapter():
    """散文行留在其章节块内（子串契约）。"""
    chunks = BookChunker().chunk(_BOOK_TEXT, _meta(), max_chars=480, overlap_chars=0)
    ch1 = next(c for c in chunks if c.heading == "第一章 启程")
    assert ch1.text == "第一章 启程\n少年抬头望天"


# ===========================================================================
# QaChunker：问答对成对
# ===========================================================================

_QA_TEXT = "\n".join(
    [
        "Q: 什么是个人信息？",
        "个人信息是与个人相关的信息。",
        "它包括姓名和身份证号。",
        "Q: 谁负责保护？",
        "处理者负责保护。",
    ]
)


def test_qa_pairs_stay_together():
    """两个问答对 -> 恰两个 parent_id；问句为 heading；答案段落共享问句的 parent_id。"""
    chunks = QaChunker().chunk(_QA_TEXT, _meta(), max_chars=480, overlap_chars=0)
    assert len({c.parent_id for c in chunks}) == 2
    assert [c.heading for c in chunks] == [
        "Q: 什么是个人信息？",
        "Q: 谁负责保护？",
    ]
    # 第一对：问句 + 两段答案落在同一块（同 parent_id）。
    pair1 = next(c for c in chunks if c.heading == "Q: 什么是个人信息？")
    assert "个人信息是与个人相关的信息。" in pair1.text
    assert "它包括姓名和身份证号。" in pair1.text


def test_qa_detects_chinese_question_prefix():
    """问：前缀形式亦识别为问句（小节 heading）。"""
    text = "\n".join(["问：如何申请？", "填写表格并提交。"])
    chunks = QaChunker().chunk(text, _meta(), max_chars=480, overlap_chars=0)
    assert len(chunks) == 1
    assert chunks[0].heading == "问：如何申请？"
    assert "填写表格并提交。" in chunks[0].text


def test_qa_long_answer_keeps_pair_parent_id():
    """长答案超预算被 chunk_document 切成多块，但仍共享同一 parent_id（问答对不拆散）。"""
    text = "\n".join(["Q: 详述流程？", "甲" * 100, "乙" * 100, "丙" * 100])
    chunks = QaChunker().chunk(text, _meta(), max_chars=250, overlap_chars=0)
    assert len(chunks) > 1
    assert len({c.parent_id for c in chunks}) == 1
    assert all(c.heading == "Q: 详述流程？" for c in chunks)


# ===========================================================================
# 工厂解析 + 别名
# ===========================================================================

def test_make_chunker_resolves_presets():
    assert isinstance(make_chunker("laws"), LawsChunker)
    assert isinstance(make_chunker("qa"), QaChunker)
    assert isinstance(make_chunker("book"), BookChunker)


def test_preset_aliases_resolve():
    assert isinstance(make_chunker("law"), LawsChunker)
    assert isinstance(make_chunker("legal"), LawsChunker)
    assert isinstance(make_chunker("faq"), QaChunker)
    assert isinstance(make_chunker("chapter"), BookChunker)


def test_presets_are_chunkers():
    assert isinstance(make_chunker("laws"), Chunker)
    assert isinstance(make_chunker("qa"), Chunker)
    assert isinstance(make_chunker("book"), Chunker)


# ===========================================================================
# 确定性
# ===========================================================================

def test_presets_deterministic():
    cases = [
        (LawsChunker, _LAWS_TEXT),
        (BookChunker, _BOOK_TEXT),
        (QaChunker, _QA_TEXT),
    ]
    for cls, text in cases:
        first = cls().chunk(text, _meta(), max_chars=480, overlap_chars=0)
        second = cls().chunk(text, _meta(), max_chars=480, overlap_chars=0)
        assert [
            (c.text, c.heading, c.parent_id, c.source_locator) for c in first
        ] == [(c.text, c.heading, c.parent_id, c.source_locator) for c in second]


# ===========================================================================
# 基座 LayoutAwareChunker 重构后逐位不变（refactor 不改默认行为）
# ===========================================================================

def test_layout_chunker_byte_identical_after_refactor():
    """复用 test_layout_chunker.py 的标题边界用例：基座输出结构逐位不变。"""
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
