"""标题进索引（RAGSPINE_CONTEXTUAL_INDEX=off|heading|full）：标题路径只进 BM25 / 向量的【索引文本】。

- heading：索引文本 = "[章节:标题路径]" + 换行 + 正文；full：再加 title / entity / period（W4a 情境头）。
- 只出现在标题里的词（"Distribution Mix"）能被 BM25 命中；
- 交给 LLM 的 text / prompt_text 不变，不出现情境头，也不重复标题；
- page+child 的整页单元只带一次页内标题（去重后的段），不按块重复；
- RESTRICTED 块的标题不进任何整页单元、不出现在任何 snippet 里。
"""

import json
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.contextual import (
    CONTEXTUAL_INDEX_MODES,
    contextual_index_text,
    heading_index_text,
    make_contextual_index_mode,
    make_index_text_fn,
)
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever

from .conftest import DOC, FIGURE_HEADING, SECRET_HEADING, heading_corpus

HEADER = "[章节:"


def _chunk(seq: int):
    return heading_corpus()[DOC][seq]


# ---------------------------------------------------------------------------
# 工厂与索引文本
# ---------------------------------------------------------------------------


def test_modes_and_factory():
    assert CONTEXTUAL_INDEX_MODES == ("off", "heading", "full")
    assert make_contextual_index_mode(None) == "off"
    assert make_contextual_index_mode(" HEADING ") == "heading"
    assert make_contextual_index_mode("none") == "off"
    assert make_contextual_index_mode("on") == "full"
    with pytest.raises(ValueError, match="off"):
        make_contextual_index_mode("titles")
    assert make_index_text_fn("off") is None
    assert make_index_text_fn("heading") is heading_index_text
    assert make_index_text_fn("full") is contextual_index_text
    # 旧 spec 仍然可用。
    assert make_index_text_fn("none") is None
    assert make_index_text_fn("on") is contextual_index_text


def test_heading_index_text_prefixes_only_the_heading_path():
    figure = _chunk(1)
    assert heading_index_text(figure) == f"{HEADER}{FIGURE_HEADING}]\n{figure.text}"
    assert figure.text == "Agency\n72%\nPartnerships\n28%\n2,928"  # 原文不被改动
    legacy = heading_corpus()["legacy.pdf"][0]
    assert heading_index_text(legacy) == legacy.text  # 没有标题 -> 原样
    assert contextual_index_text(figure).startswith("[文档:deck · 章节:")


# ---------------------------------------------------------------------------
# BM25：标题词命中
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page_parent", ["off", "page+child"])
def test_heading_only_term_hits_bm25(heading_store, page_parent):
    def top_ids(mode: str) -> list[str]:
        index = NarrativeIndex(
            heading_store, index_text_fn=make_index_text_fn(mode), page_parent=page_parent
        )
        return [r.chunk.chunk_id for r in index.retrieve("distribution mix", rerank=False)]

    assert f"{DOC}#c1" not in top_ids("off"), "off：标题词不在索引里"
    assert top_ids("heading")[0] == f"{DOC}#c1"
    assert top_ids("full")[0] == f"{DOC}#c1"


# ---------------------------------------------------------------------------
# prompt 文本不变、不重复标题
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page_parent", ["off", "dedup", "page+child"])
def test_prompt_text_has_no_header_and_no_repeated_heading(heading_store, page_parent):
    retriever = NarrativeIndexRetriever(
        NarrativeIndex(
            heading_store, index_text_fn=make_index_text_fn("heading"), page_parent=page_parent
        )
    )
    snippets = retriever.retrieve("distribution mix")
    assert snippets
    corpus = {c.chunk_id: c for cs in heading_corpus().values() for c in cs}
    for snippet in snippets:
        text = str(snippet["text"])
        prompt = str(snippet.get("prompt_text", text))
        assert HEADER not in text and HEADER not in prompt
        assert "Distribution Mix" not in prompt, "标题不能被拼进交给 LLM 的文本"
        assert text == corpus[str(snippet["chunk_id"])].text


def test_page_unit_carries_page_headings_once(heading_store):
    seen: dict[str, str] = {}

    def spy(chunk):
        out = heading_index_text(chunk)
        seen[chunk.chunk_id] = out
        return out

    NarrativeIndex(heading_store, index_text_fn=spy, page_parent="page+child").retrieve(
        "new business profile", rerank=False
    )
    unit = seen[f"{DOC}@page=1"]
    header, _, body = unit.partition("\n")
    assert unit.count(HEADER) == 1
    for segment in ("New Business Profile", "Distribution Mix", "Commentary"):
        assert header.count(segment) == 1, segment
    # 正文部分就是整页窗口（不含情境头）。
    assert HEADER not in body and "Agency\n72%" in body


def test_page_unit_heading_is_empty_without_headings():
    from ragspine.retrieval.page_parent.pages import page_heading

    assert page_heading(heading_corpus()["legacy.pdf"]) == ""
    assert page_heading(heading_corpus()[DOC][:3]) == (
        "New Business Profile > Distribution Mix > Commentary"
    )


# ---------------------------------------------------------------------------
# RESTRICTED：标题不泄漏
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["heading", "full"])
@pytest.mark.parametrize("page_parent", ["off", "dedup", "page+child"])
def test_restricted_heading_never_leaks(heading_store, mode, page_parent):
    seen: dict[str, str] = {}
    fn = make_index_text_fn(mode)

    def spy(chunk):
        out = fn(chunk)
        seen[chunk.chunk_id] = out
        return out

    retriever = NarrativeIndexRetriever(
        NarrativeIndex(
            heading_store,
            index_text_fn=spy,
            page_parent=page_parent,
            judge=_ReverseJudge(),
        )
    )
    snippets = retriever.retrieve("Falcon acquisition")
    dumped = json.dumps(snippets, ensure_ascii=False, default=str)
    assert "Falcon" not in dumped
    assert f"{DOC}#c4" not in dumped
    for chunk_id, text in seen.items():
        if chunk_id.endswith("@page=2"):
            assert SECRET_HEADING not in text, "整页单元不能带 RESTRICTED 块的标题"


class _ReverseJudge:
    def judge(self, query: str, candidates: list[str]) -> list[int]:
        assert all("Falcon" not in c for c in candidates), "RESTRICTED 标题进了精排候选"
        return list(reversed(range(len(candidates))))
