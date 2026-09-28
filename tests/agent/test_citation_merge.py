"""叙事「（资料来源：…）」后缀按文档合并（ADR 0029）。

- 同一文档的多个片段合成一条：``doc page=6, 10, 14, 18``（去重、升序、连续页合成 ``a-b``），
  pptx 用 ``slide=``；解析不出页码的 locator 去重后原样附在后面。
- 每个文档只有一个 locator 时与旧的 ``"；".join(...)`` 逐字节相同；
  ``RAGSPINE_CITATION_MERGE=off`` 时即使有重复也逐字节等于旧输出。
- ``sources`` 不受影响（逐片段血缘不丢）；后缀仍在 number guard 之后追加，
  且合并后的串经 guard 的豁免逻辑不会被误报。
"""

import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion, Choice, ResponseMessage

import ragspine.agent.agent as agent_mod
from ragspine.agent.agent import NARRATIVE_FALLBACK_ENV, answer_question
from ragspine.agent.citations import (
    CITATION_MERGE_ENV,
    merge_citation,
    resolve_citation_merge,
)
from ragspine.agent.number_guard import (
    NARRATIVE_NUMBER_GUARD_ENV,
    NUMBER_GUARD_NOTICE,
    guard_narrative_answer,
    ungrounded_numbers,
)
from ragspine.eval.nl_gold_ragspine import ForcedNarrativeIntentParser
from ragspine.storage.fact_store import SqliteFactStore

REF = date(2026, 9, 28)


def _src(doc: str, locator: str) -> dict[str, object]:
    return {"doc": doc, "locator": locator}


def _old_cite(missing) -> str:
    """改动前 agent.py 的拼法（逐字节对照基准）。"""
    return "；".join(f"{s['doc']} {s['locator']}".strip() for s in missing)


# ---------------------------------------------------------------------------
# 纯函数：merge_citation
# ---------------------------------------------------------------------------


def test_same_doc_pages_dedup_sorted():
    d = "d.md"
    missing = [
        _src(d, f"{d}@page=18#para1-19"),
        _src(d, f"{d}@page=14#para1-7"),
        _src(d, f"{d}@page=6#para2"),
        _src(d, f"{d}@page=14#para1"),
        _src(d, f"{d}@page=10#para3-4"),
    ]
    assert merge_citation(missing) == "d.md page=6, 10, 14, 18"


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ([5, 6, 7, 9], "page=5-7, 9"),
        ([5, 6], "page=5-6"),
        ([9, 7, 5, 6, 7], "page=5-7, 9"),
        ([3, 5, 6, 7], "page=3, 5-7"),
        ([1, 3, 5], "page=1, 3, 5"),
        ([2, 3, 4, 10, 11], "page=2-4, 10-11"),
    ],
)
def test_consecutive_pages_collapse_to_ascii_range(pages, expected):
    missing = [_src("d.md", f"d.md@page={p}#para{i}") for i, p in enumerate(pages)]
    assert merge_citation(missing) == f"d.md {expected}"


def test_docs_keep_first_seen_order():
    missing = [
        _src("a.md", "a.md@page=3#para1"),
        _src("b.md", "b.md@page=1#para1"),
        _src("a.md", "a.md@page=2#para1"),
    ]
    assert merge_citation(missing) == "a.md page=2-3；b.md b.md@page=1#para1"


def test_pptx_slide_locators_parse_both_forms():
    missing = [
        _src("X.pptx", "X.pptx@slide=3,frame=2#para1"),
        _src("X.pptx", "slide=12"),
        _src("X.pptx", "X.pptx@slide=4#para2"),
    ]
    assert merge_citation(missing) == "X.pptx slide=3-4, 12"


def test_unparseable_locators_dedup_verbatim():
    missing = [
        _src("review.txt", "review.txt#para1-2"),
        _src("review.txt", "#para4"),
        _src("review.txt", "review.txt#para1-2"),
    ]
    assert merge_citation(missing) == "review.txt review.txt#para1-2, #para4"


def test_mixed_groups_order_is_page_then_slide_then_verbatim():
    d = "mix.pdf"
    missing = [
        _src(d, "faq#what-is"),
        _src(d, "slide=4"),
        _src(d, f"{d}@page=7#para1"),
        _src(d, "appendix"),
        _src(d, f"{d}@page=5#para1"),
        _src(d, "slide=2"),
        _src(d, "faq#what-is"),
        _src(d, f"{d}@page=6#para3"),
    ]
    assert merge_citation(missing) == "mix.pdf page=5-7, slide=2, 4, faq#what-is, appendix"


def test_single_distinct_locator_repeated_prints_once():
    loc = "d.md@page=14#para1-7"
    assert merge_citation([_src("d.md", loc), _src("d.md", loc)]) == f"d.md {loc}"


def test_page_not_at_start_or_after_at_is_not_parsed():
    # 只认 ^ 或 @ 之后的 page= / slide=，别处出现的 page= 当不透明串原样保留。
    missing = [_src("d.md", "d.md#subpage=3"), _src("d.md", "d.md@page=3x")]
    assert merge_citation(missing) == "d.md d.md#subpage=3, d.md@page=3x"


@pytest.mark.parametrize(
    "missing",
    [
        [],
        [_src("d.md", "d.md@page=18#para1-19")],
        [_src("HK_QBR_2025Q4.pptx", "slide=12")],
        [_src("deck.md", "")],
        [_src("review.txt", "review.txt#para1-2"), _src("faq/x.md", "faq/x.md#what-is")],
        [
            _src("a.md", "a.md@page=3#para1"),
            _src("RESULTS_2024.md", "RESULTS_2024.md@page=2#para1-3"),
            _src("X.pptx", "X.pptx@slide=3,frame=2#para1"),
        ],
    ],
)
def test_one_locator_per_doc_is_byte_identical_to_old(missing):
    assert merge_citation(missing) == _old_cite(missing)


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for name in (CITATION_MERGE_ENV, NARRATIVE_NUMBER_GUARD_ENV, NARRATIVE_FALLBACK_ENV):
        monkeypatch.delenv(name, raising=False)


def test_resolve_default_on():
    assert resolve_citation_merge() is True


@pytest.mark.parametrize(("raw", "expected"), [(" OFF ", False), ("on", True), ("", True)])
def test_resolve_reads_env(monkeypatch, raw, expected):
    monkeypatch.setenv(CITATION_MERGE_ENV, raw)
    assert resolve_citation_merge() is expected


def test_resolve_rejects_unknown(monkeypatch):
    monkeypatch.setenv(CITATION_MERGE_ENV, "maybe")
    with pytest.raises(ValueError, match=CITATION_MERGE_ENV):
        resolve_citation_merge()


# ---------------------------------------------------------------------------
# 编排层端到端
# ---------------------------------------------------------------------------

DOC = "deck.md"
PROSE = "增长主要来自代理渠道。"
SNIPPETS = [
    {"text": "Agency 72%", "doc_id": DOC, "locator": f"{DOC}@page=18#para1-19"},
    {"text": "VONB up 15%", "doc_id": DOC, "locator": f"{DOC}@page=14#para1-7"},
    {"text": "Other", "doc_id": "RESULTS_2024.md", "locator": "RESULTS_2024.md#para3"},
    {"text": "Pathway", "doc_id": DOC, "locator": f"{DOC}@page=6#para1-23"},
    {"text": "VONB detail", "doc_id": DOC, "locator": f"{DOC}@page=14#para1"},
    {"text": "Mix", "doc_id": DOC, "locator": f"{DOC}@page=5#para2"},
    {"text": "Deck", "doc_id": "X.pptx", "locator": "X.pptx@slide=4,frame=1#para1"},
    {"text": "Deck2", "doc_id": "X.pptx", "locator": "slide=2"},
]
MERGED = "deck.md page=5-6, 14, 18；RESULTS_2024.md RESULTS_2024.md#para3；X.pptx slide=2, 4"


@pytest.fixture
def store(tmp_db_path):
    fs = SqliteFactStore(tmp_db_path)
    fs.init_schema()
    yield fs
    fs.close()


def _text(content: str) -> ChatCompletion:
    msg = ResponseMessage(role="assistant", content=content)
    return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="stop"),))


class ScriptedProvider:
    def __init__(self, answer: str):
        self._answer = answer

    def chat(self, messages, *, tools=None):
        return _text(self._answer)


class FakeRetriever:
    def __init__(self, snippets):
        self.snippets = snippets

    def retrieve(self, query, *, filters=None, top_k=50):
        return [dict(s) for s in self.snippets]


def _ask(store, answer=PROSE, snippets=SNIPPETS, *, guard=None):
    return answer_question(
        "代理渠道表现如何？",
        store,
        ScriptedProvider(answer),
        reference_date=REF,
        narrative_retriever=FakeRetriever(snippets),
        intent_parser=ForcedNarrativeIntentParser(),
        narrative_number_guard=guard,
    )


def _refs(sources) -> list[str]:
    """与 agent._run_narrative 给 guard 的 refs 同一口径。"""
    return [str(r) for src in sources for r in (src["doc"], src["locator"]) if r]


def test_default_on_merges_suffix(store):
    result = _ask(store)
    assert result.answer_plain == PROSE
    assert result.answer == f"{PROSE}\n（资料来源：{MERGED}）"


def test_off_is_byte_identical_to_old_even_with_duplicates(store, monkeypatch):
    monkeypatch.setenv(CITATION_MERGE_ENV, "off")
    result = _ask(store)
    assert result.answer == f"{PROSE}\n（资料来源：{_old_cite(result.sources)}）"


def test_sources_and_answer_plain_identical_on_and_off(store, monkeypatch):
    on = _ask(store)
    monkeypatch.setenv(CITATION_MERGE_ENV, "off")
    off = _ask(store)
    assert on.sources == off.sources
    assert on.sources == [{"doc": s["doc_id"], "locator": s["locator"]} for s in SNIPPETS]
    assert on.answer_plain == off.answer_plain == PROSE
    assert on.answer != off.answer


def test_guard_runs_before_suffix_is_appended(store, monkeypatch):
    seen: list[str] = []
    real_guard = agent_mod.guard_narrative_answer

    def spy(answer, question, evidence, refs):
        seen.append(answer)
        return real_guard(answer, question, evidence, refs)

    monkeypatch.setattr(agent_mod, "guard_narrative_answer", spy)
    result = _ask(store, guard=True)
    # guard 看到的是模型原文（不含后缀）；后缀在 guard 之后原样追加。
    assert seen == [PROSE]
    assert result.answer_plain == PROSE
    assert result.answer == f"{PROSE}\n（资料来源：{MERGED}）"


# ---------------------------------------------------------------------------
# number guard 兼容：合并后的串经过 guard 的豁免逻辑不被误报
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix",
    [
        "d.md page=3, 5-7",
        "d.md page=5-7, 9",
        "X.pptx slide=2, 4",
        "RESULTS_2024.md page=12-13, RESULTS_2024.md#para1-2",
        "mix.pdf page=5-7, slide=2, 4, faq#what-is",
        MERGED,
    ],
)
def test_merged_suffix_passes_ungrounded_numbers(suffix):
    refs = [
        "d.md",
        "X.pptx",
        "RESULTS_2024.md",
        "RESULTS_2024.md#para1-2",
        "mix.pdf",
        "faq#what-is",
        *_refs([{"doc": s["doc_id"], "locator": s["locator"]} for s in SNIPPETS]),
    ]
    text = f"结论见资料。\n（资料来源：{suffix}）"
    assert ungrounded_numbers(text, question="", evidence=[], source_refs=refs) == []


@pytest.mark.parametrize(
    ("suffix", "refs"),
    [
        ("X.pptx slide=2, 4", ["X.pptx", "slide=2", "slide=4"]),
        ("d.md page=12-13", ["d.md", "page=1", "page=12"]),
        ("d.md page=1, 3", ["d.md", "page=1#para2", "d.md@page=3#para1"]),
    ],
)
def test_bare_page_locator_ref_does_not_split_merged_list(suffix, refs):
    # 裸 page= / slide= locator 作为 refs 若先整串替换，会把 "slide=2, 4" 切成孤立的 ", 4"；
    # 这类 ref 本就能被页码引用模式整段豁免，guard 不再拿它做整串替换。
    text = f"结论见资料。\n（资料来源：{suffix}）"
    assert ungrounded_numbers(text, question="", evidence=[], source_refs=refs) == []


def test_source_numbers_do_not_excuse_body_numbers():
    text = "共 5 项，增长 7%。\n（资料来源：d.md page=3, 5-7）"
    refs = ["d.md", "d.md@page=3#para1", "d.md@page=5#para1"]
    assert ungrounded_numbers(text, question="", evidence=[], source_refs=refs) == ["5", "7%"]


def test_orchestrator_output_survives_real_guard(store):
    # 真实编排输出（含合并后缀）整段送回 guard_narrative_answer，refs 与 agent 同口径：零改写。
    result = _ask(store, guard=True)
    assert "page=5-6, 14, 18" in result.answer and "slide=2, 4" in result.answer
    evidence = [s["text"] for s in SNIPPETS]
    assert guard_narrative_answer(result.answer, "", evidence, _refs(result.sources)) == (
        result.answer,
        0,
    )


def test_model_echoing_merged_citation_is_not_rewritten(store):
    # 模型把合并格式的来源串写进正文（如照抄上一轮答案）：这段文本真的经过 _run_narrative 里的 guard。
    echoed = f"{PROSE}\n（资料来源：{MERGED}）"
    result = _ask(store, answer=echoed, guard=True)
    assert result.answer_plain == echoed
    assert result.answer == echoed  # 各 doc 名都已出现在答案里，不再追加后缀


def test_model_echo_with_ungrounded_body_number_is_still_rewritten(store):
    snippets = [
        {"text": "Agency 72%", "doc_id": "d.md", "locator": "d.md@page=3#para1"},
        {"text": "VONB up 15%", "doc_id": "d.md", "locator": "d.md@page=5#para1"},
    ]
    answer = "共 5 项，增长 7%。\n（资料来源：d.md page=3, 5-7）"
    result = _ask(store, answer=answer, snippets=snippets, guard=True)
    assert result.answer_plain.startswith(NUMBER_GUARD_NOTICE)
    assert "7%" not in result.answer_plain
    assert result.answer == f"{result.answer_plain}\n（资料来源：d.md page=3, 5）"
