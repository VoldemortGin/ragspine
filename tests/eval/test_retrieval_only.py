"""通用 retrieval-only 评测（eval/retrieval_only）：题集解析 + 页/内容判定 + recall@k / MRR。

全部离线：题集写在 tmp 里，检索器是按 ``NarrativeRetriever`` 协议返回固定 snippet 的桩。
口径（被测规格）：
- 有 page group 时按页判定（组内任一页命中即可；给了 doc 还要求 doc 匹配），忽略 expected；
- 没页码时回退 expected 与命中块文本比对（含数字则每个数字都须出现，规范化同 nl_gold）；
- rank = 各 group 首次命中位置的最大值；``page_recall`` 按不同页计名次；
- 与 ``nl_gold_ragspine.recall_at_k`` 对等（同一组数据两边结果完全一致）。
"""

import json
import os
from pathlib import Path

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.eval.nl_gold_ragspine import (
    ROUTE_FORCED_NARRATIVE,
    CaseRun,
    ClaimAnchor,
    GoldCase,
    Judgement,
    contains_normalized,
    load_nl_gold,
    normalize_answer,
    recall_at_k,
)
from ragspine.eval.retrieval_only import (
    BatchQuestion,
    QuestionSetError,
    content_hit,
    gold_rank,
    judge_hits,
    load_questions,
    parse_pages,
    recall_ks,
    retrieval_metrics,
    retrieve_hits,
)


def _hit(page: int | None, *, doc: str = "deck.md", text: str = "") -> dict[str, object]:
    locator = f"{doc}@page={page}#para1-2" if page is not None else f"{doc}@slide=3"
    return {"doc_id": doc, "source_locator": locator, "text": text}


# ---------------------------------------------------------------------------
# 页码解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (12, {12}),
        ("12", {12}),
        ([12, 13], {12, 13}),
        ("12, 14-15", {12, 14, 15}),
        ("12-14", {12, 13, 14}),
        ("", set()),
        (None, set()),
    ],
)
def test_parse_pages(raw: object, expected: set[int]) -> None:
    assert parse_pages(raw) == frozenset(expected)


@pytest.mark.parametrize("raw", [0, -1, "abc", "5-3", True, 1.5, "1-", [1, "x"]])
def test_parse_pages_rejects_illegal(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_pages(raw)


def test_batch_question_is_strict_and_frozen() -> None:
    q = BatchQuestion(id="a", question="q", page_groups=["3-4"], doc="deck.md")
    assert q.page_groups == (frozenset({3, 4}),)
    assert q.doc == ("deck.md",)
    with pytest.raises(ValueError):
        BatchQuestion(id="a", question="")
    with pytest.raises(ValueError):
        BatchQuestion(id="a", question="q", unknown="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 题集格式
# ---------------------------------------------------------------------------


def test_load_jsonl_maps_fields_and_keeps_extra(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    rows = [
        {"id": "a", "question": "Q1", "expected": "72%", "pages": [2, 3], "tag": "x"},
        {"question": "Q2", "page": 5, "doc": ["deck.md", "other.md"], "n": 3},
        {"id": "c", "question": "Q3"},
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n\n")
    qs = load_questions(path)
    assert [q.id for q in qs] == ["a", "q2", "c"]
    assert qs[0].expected == "72%" and qs[0].page_groups == (frozenset({2, 3}),)
    assert qs[0].extra == {"tag": "x"}
    assert qs[1].page_groups == (frozenset({5}),) and qs[1].doc == ("deck.md", "other.md")
    assert qs[1].extra == {"n": "3"}
    assert qs[2].page_groups == () and qs[2].expected is None


def test_load_json_list_and_object(tmp_path: Path) -> None:
    listed = tmp_path / "a.json"
    listed.write_text(json.dumps([{"id": "x", "question": "Q", "pages": "12, 14-15"}]))
    assert load_questions(listed)[0].page_groups == (frozenset({12, 14, 15}),)
    wrapped = tmp_path / "b.json"
    wrapped.write_text(json.dumps({"questions": [{"id": "y", "question": "Q"}]}))
    assert load_questions(wrapped)[0].id == "y"


def test_load_csv_with_page_range(tmp_path: Path) -> None:
    path = tmp_path / "q.csv"
    path.write_text("id,question,expected,pages,doc,note\na,Q1,17.5%,12-14,deck.md,n1\nb,Q2,,,,\n")
    qs = load_questions(path)
    assert qs[0].page_groups == (frozenset({12, 13, 14}),)
    assert qs[0].doc == ("deck.md",) and qs[0].extra == {"note": "n1"}
    assert qs[1].expected is None and qs[1].page_groups == () and qs[1].doc == ()


def test_load_txt_skips_comments_and_blanks(tmp_path: Path) -> None:
    path = tmp_path / "q.txt"
    path.write_text("# header\nQ1\n\n  Q2  \n#c\n", encoding="utf-8")
    qs = load_questions(path)
    assert [(q.id, q.question) for q in qs] == [("q1", "Q1"), ("q2", "Q2")]


def test_load_qa_golden_style_expected_object(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    row = {
        "id": "num-001",
        "question": "香港FY2025的REVENUE是多少",
        "expected": {"value": 1702.0, "unit": "USD_M", "source": {"doc": "ACME.pptx"}},
        "tags": {"topic": "FIN"},
    }
    refuse = {"id": "r", "question": "Q", "expected": {"value": None, "refuse": True}}
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n" + json.dumps(refuse) + "\n")
    qs = load_questions(path)
    assert qs[0].expected == "1702" and qs[0].doc == ("ACME.pptx",)
    assert json.loads(qs[0].extra["tags"]) == {"topic": "FIN"}
    assert qs[1].expected is None


def test_the_bundled_qa_golden_set_loads() -> None:
    qs = load_questions(Path(ROOT_DIR) / "data" / "golden" / "qa_golden_set.jsonl")
    assert qs and len({q.id for q in qs}) == len(qs)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("dup.jsonl", '{"id": "a", "question": "Q"}\n{"id": "a", "question": "Q2"}\n'),
        ("bad-page.jsonl", '{"id": "a", "question": "Q", "pages": "x"}\n'),
        ("no-question.jsonl", '{"id": "a"}\n'),
        ("broken.jsonl", "{not json\n"),
        ("q.yaml", "question: Q\n"),
        ("empty.txt", "# only comments\n"),
    ],
)
def test_load_rejects_bad_sets(tmp_path: Path, name: str, content: str) -> None:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    with pytest.raises(QuestionSetError):
        load_questions(path)


def _gold_file(tmp_path: Path) -> Path:
    def anchor(page_index: int, quote: str) -> dict[str, object]:
        return {"kind": "quote", "page_index": page_index, "quote": quote}

    def case(case_id: str, case_class: str, expected: dict[str, object], **extra: object):
        return {
            "case_id": case_id,
            "case_class": case_class,
            "question": {"en": f"{case_id} en", "zh": f"{case_id} zh"},
            "expected": expected,
            **extra,
        }

    payload = {
        "schema_version": "nl-answers-gold-v1",
        "cases": [
            case(
                "p-two",
                "positive",
                {
                    "status": "answered",
                    "required_claims": [
                        {"any_of": [anchor(2, "a"), anchor(3, "a")]},
                        anchor(6, "b"),
                    ],
                },
            ),
            case("abst", "abstain", {"status": "abstained"}),
            case("adv", "adversarial", {"status": "answered"}),
            case("off", "positive", {"status": "answered"}, offline_only=True),
            case(
                "gap",
                "positive",
                {"status": "answered", "known_gap": True, "required_claims": [anchor(0, "c")]},
            ),
        ],
    }
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_nl_gold_one_question_per_case_language(tmp_path: Path) -> None:
    qs = load_questions(_gold_file(tmp_path))
    by_id = {q.id: q for q in qs}
    # 跳过的 case（adversarial / offline_only）不出题；abstain 出题但无 page group（不参与判定）。
    assert set(by_id) == {"p-two:en", "p-two:zh", "abst:en", "abst:zh", "gap:en", "gap:zh"}
    assert by_id["p-two:en"].page_groups == (frozenset({3, 4}), frozenset({7}))
    assert by_id["p-two:zh"].question == "p-two zh"
    assert by_id["abst:en"].page_groups == () and by_id["abst:en"].expected is None
    assert by_id["gap:en"].page_groups == (frozenset({1}),)
    assert by_id["gap:en"].extra["case_class"] == "known-gap"


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def test_page_basis_ignores_expected() -> None:
    q = BatchQuestion(id="a", question="q", expected="999", page_groups=[7])
    judged = judge_hits(q, [_hit(5, text="999"), _hit(7, text="nothing")])
    assert judged.basis == "page"
    assert judged.rank == 2 and judged.hit_flags == (False, True)


def test_doc_filter_accepts_stem_and_casefold() -> None:
    q = BatchQuestion(id="a", question="q", page_groups=[3], doc="DECK")
    hits = [_hit(3, doc="other.md"), _hit(3, doc="deck.md")]
    judged = judge_hits(q, hits)
    assert judged.rank == 2 and judged.hit_flags == (False, True)
    # 不给 doc 时只看页码（与 nl_gold 一致）。
    loose = judge_hits(BatchQuestion(id="b", question="q", page_groups=[3]), hits)
    assert loose.rank == 1


def test_distinct_key_is_doc_page_when_doc_given() -> None:
    hits = [_hit(3, doc="x.md"), _hit(3, doc="y.md"), _hit(5, doc="deck.md")]
    with_doc = judge_hits(BatchQuestion(id="a", question="q", page_groups=[5], doc="deck"), hits)
    assert with_doc.page_rank == 3
    without_doc = judge_hits(BatchQuestion(id="b", question="q", page_groups=[5]), hits)
    assert without_doc.page_rank == 2


def test_unpaged_hits_do_not_take_page_ranks() -> None:
    q = BatchQuestion(id="a", question="q", page_groups=[7])
    judged = judge_hits(q, [_hit(None), _hit(7)])
    assert judged.rank == 1 and judged.hit_flags == (False, True)


def test_content_basis_fullwidth_and_thousands() -> None:
    q = BatchQuestion(id="a", question="q", expected="收入 1,320 与 １７.５％")
    judged = judge_hits(q, [_hit(None, text="只有 1320"), _hit(None, text="1320 和 17.5% 都在")])
    assert judged.basis == "content"
    assert judged.rank == 2 and judged.page_rank == 2
    assert judged.hit_flags == (False, True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Agency share of VONB was 72%.", "72%"),
        ("Agency share was 172%.", "72%"),
        ("ROE 38.00", "38"),
        ("The record ROE was high", "record roe"),
        ("unrelated", "record roe"),
        ("收入 1,320 亿", "1320"),
    ],
)
def test_content_hit_delegates_to_nl_gold_matching(text: str, expected: str) -> None:
    """单个数字 / 纯文本的判定完全委托 nl_gold 的公开函数（口径随 nl_gold 走，不写死归一化结果）。"""
    assert content_hit(text, expected) == contains_normalized(normalize_answer(text), expected)


def test_content_hit_requires_every_number() -> None:
    """含多个数字时逐个委托 contains_normalized，全部命中才算；文字措辞不必一致。"""
    text = "增长 12.5% 与 3 项"
    haystack = normalize_answer(text)
    assert contains_normalized(haystack, "12.5%") and contains_normalized(haystack, "3")
    assert content_hit(text, "约 12.5%，共 3 个")
    assert not contains_normalized(haystack, "7")
    assert not content_hit(text, "12.5% 与 7")


def test_content_basis_uses_prompt_text() -> None:
    q = BatchQuestion(id="a", question="q", expected="17.5%")
    hit = {"doc_id": "d", "source_locator": "d@page=2#p1", "text": "x", "prompt_text": "ROE 17.5%"}
    assert judge_hits(q, [hit]).rank == 1


def test_unjudgeable_question() -> None:
    judged = judge_hits(BatchQuestion(id="a", question="q"), [_hit(1)])
    assert judged.basis == "none" and not judged.judged and judged.rank is None


def test_multi_group_rank_is_max_of_firsts() -> None:
    hits = [(("d", p)) for p in (4, 1, 1, 9, 7, 3)]
    groups = (frozenset({3, 4}), frozenset({7}))
    assert gold_rank(hits, groups) == 5
    assert gold_rank(hits, groups, distinct=True) == 4
    assert gold_rank(hits, (frozenset({4}), frozenset({8}))) is None


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


def test_recall_ks() -> None:
    assert recall_ks(10) == (1, 3, 5, 10)
    assert recall_ks(4) == (1, 3, 4)
    assert recall_ks(20) == (1, 3, 5, 10, 20)
    assert recall_ks(1) == (1,)


def test_retrieval_metrics_edges() -> None:
    metrics = retrieval_metrics([(1, 1), (4, 2), (None, None)], (1, 3, 5))
    assert metrics["judged"] == 3
    assert metrics["recall"] == {"@1": 0.3333, "@3": 0.3333, "@5": 0.6667}
    assert metrics["page_recall"] == {"@1": 0.3333, "@3": 0.6667, "@5": 0.6667}
    assert metrics["mrr"] == round((1 + 0.25 + 0) / 3, 4)
    assert metrics["page_mrr"] == round((1 + 0.5 + 0) / 3, 4)
    empty = retrieval_metrics([], (1,))
    assert empty["judged"] == 0 and empty["recall"] == {"@1": 0.0} and empty["mrr"] == 0.0


# ---------------------------------------------------------------------------
# 与 nl_gold.recall_at_k 对等（口径单一来源在 nl_gold 解禁前靠这里钉住）
# ---------------------------------------------------------------------------


def _run(pages: list[int], case_id: str = "p") -> CaseRun:
    return CaseRun(
        route=ROUTE_FORCED_NARRATIVE,
        case_id=case_id,
        case_class="positive",
        language="en",
        question="q",
        answer="",
        answer_plain="",
        agent_route="narrative",
        route_label="narrative",
        sources=[],
        retrieved_locators=[],
        judgement=Judgement(passed=False, reason="", refusal=False),
        llm_calls=0,
        seconds=0.0,
        retrieved_pages=pages,
    )


def _ours(case: GoldCase, pages: list[int], ks: tuple[int, ...]) -> dict[str, object]:
    groups = tuple(frozenset(a.page_index + 1 for a in group) for group in case.required_claims)
    q = BatchQuestion(id=case.case_id, question="q", page_groups=groups)
    judged = judge_hits(q, [_hit(p) for p in pages])
    return retrieval_metrics([(judged.rank, judged.page_rank)], ks)


@pytest.mark.parametrize(
    ("claims", "pages"),
    [
        # test_nl_gold_ragspine 同一组数据：块名次 4、不同页名次 2。
        (((6,),), [5, 5, 5, 7]),
        # 多 claim group：{3,4} 与 {7} 都要进前 k。
        (((2, 3), (6,)), [4, 1, 1, 9, 7, 3]),
        # 一组始终不命中 → None。
        (((2, 3), (7,)), [4, 1, 1, 9, 7, 3]),
        # 同页重复块 + 多组交错。
        (((0,), (1,), (4,)), [2, 2, 1, 1, 5, 2, 5]),
    ],
)
def test_parity_with_nl_gold_recall_at_k(
    claims: tuple[tuple[int, ...], ...], pages: list[int]
) -> None:
    case = GoldCase(
        case_id="p",
        case_class="positive",
        questions=(("en", "q"),),
        required_claims=tuple(
            tuple(ClaimAnchor(kind="quote", page_index=i, quote="x") for i in group)
            for group in claims
        ),
    )
    ks = (1, 3, 5, 10)
    theirs = recall_at_k([_run(pages)], [case], ks=ks)
    ours = _ours(case, pages, ks)
    assert ours["recall"] == theirs["recall"]
    assert ours["page_recall"] == theirs["page_recall"]


def test_parity_through_gold_file(tmp_path: Path) -> None:
    """题集经 load_questions(gold) 得到的 page group 与 recall_at_k 的 eligible 口径一致。"""
    path = _gold_file(tmp_path)
    cases = load_nl_gold(path)
    pages_by_case = {"p-two": [4, 1, 7], "gap": [2, 1], "abst": [1]}
    runs = [
        _run(pages_by_case[c.case_id], c.case_id)
        for c in cases
        if not c.skip_reason and c.case_id in pages_by_case
    ]
    theirs = recall_at_k(runs, cases, ks=(1, 3))
    ranks = []
    for q in load_questions(path):
        if q.extra["language"] != "en":
            continue
        judged = judge_hits(q, [_hit(p) for p in pages_by_case[q.id.split(":")[0]]])
        if judged.judged:
            ranks.append((judged.rank, judged.page_rank))
    ours = retrieval_metrics(ranks, (1, 3))
    assert ours["judged"] == theirs["cases"] == 2
    assert ours["recall"] == theirs["recall"]
    assert ours["page_recall"] == theirs["page_recall"]


# ---------------------------------------------------------------------------
# 单题检索
# ---------------------------------------------------------------------------


class _FixedRetriever:
    def __init__(self, snippets: list[dict[str, object]]) -> None:
        self.snippets = snippets
        self.calls: list[tuple[str, dict[str, str] | None, int]] = []

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]:
        self.calls.append((query, filters, top_k))
        return list(self.snippets)


def test_retrieve_hits_passes_no_filters_and_truncates() -> None:
    retriever = _FixedRetriever([_hit(p) for p in range(1, 8)])
    hits = retrieve_hits(retriever, "q", top_k=3)
    assert len(hits) == 3
    assert retriever.calls == [("q", None, 3)]
