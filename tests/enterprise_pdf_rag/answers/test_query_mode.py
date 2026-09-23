"""Query classification picks a retrieval channel from the question alone."""

import pytest

from enterprise_pdf_rag.answers.query_mode import (
    MAX_BM25_ONLY_CONTENT_WORDS,
    MAX_BM25_ONLY_SHORT_CONTENT_WORDS,
    MAX_BM25_ONLY_TOKENS,
    classify_query,
    content_words,
    is_label_query,
    is_numeric,
    tokenize_query,
)
from ragspine.extraction.evidence.metadata.periods import find_periods
from ragspine.retrieval.lexical.retrieval import tokenize


@pytest.mark.parametrize(
    "question",
    [
        "VONB 1H26",
        "VONB margin 1H26",
        "What was VONB in 1H26?",
        "EV equity 2026",
        "1H26 Distribution Mix",
        "Thailand 1H26 VONB",
    ],
)
def test_short_questions_take_the_lexical_channel_alone(question: str) -> None:
    assert classify_query(question, lexical_hits=7) == "bm25_only"


@pytest.mark.parametrize(
    "question",
    [
        "What was the Group VONB margin in 1H26 and how did it move?",
        "Which distribution channels grew new business value this half?",
        "How did the agency channel compare with the bancassurance channel in 1H26?",
        "Summarise the drivers behind the change in operating profit after tax",
    ],
)
def test_long_or_narrative_questions_keep_reciprocal_rank_fusion(question: str) -> None:
    assert classify_query(question, lexical_hits=7) == "rrf"


@pytest.mark.parametrize(
    ("question", "mode"),
    [
        # Natural phrases the token budget alone mistook for label-and-period probes: three
        # content words, so the lexical channel has to score a phrase, not a label.
        ("Agency share of VONB 1H26", "rrf"),
        ("Agency share of VONB", "rrf"),
        (
            "Distribution Mix 1H26: what is the Agency share of VONB, "
            "and what is the Partnerships share?",
            "rrf",
        ),
        (
            "In the 1H26 Distribution Mix chart, what percentage of VONB came from Agency?",
            "rrf",
        ),
        # The probe's own shape — a label plus a period — still takes BM25 alone.
        ("1H26 Distribution Mix", "bm25_only"),
        ("Thailand 1H26 VONB", "bm25_only"),
    ],
)
def test_the_gold_questions_route_to_the_channel_that_retrieves_them(
    question: str, mode: str
) -> None:
    """Frozen natural-language gold questions, pinned to the channel that finds their evidence.

    ``Agency share of VONB 1H26`` is five tokens, so the token budget alone routed it to BM25,
    where the donut chart it needs falls to rank 12; fusion keeps it at rank 7, inside the ten
    prompt seats.
    """
    assert classify_query(question, lexical_hits=7) == mode


def test_the_short_clause_needs_a_content_word_budget_as_well_as_a_token_budget() -> None:
    assert MAX_BM25_ONLY_SHORT_CONTENT_WORDS == 2
    question = "Agency share of VONB 1H26"
    # Within the token budget, over the content-word budget: not a label-and-period probe.
    assert len(tokenize_query(question)) <= MAX_BM25_ONLY_TOKENS
    assert len(content_words(tokenize_query(question))) > MAX_BM25_ONLY_SHORT_CONTENT_WORDS
    assert classify_query(question, lexical_hits=1) == "rrf"
    # One content word fewer, and it is a label plus a period again.
    assert classify_query("share of VONB 1H26", lexical_hits=1) == "bm25_only"
    # A figure is not required: a bare two-word label is still a label.
    assert classify_query("expense ratio", lexical_hits=1) == "bm25_only"
    # Four content words in four tokens: over budget, whether or not it reads like a label.
    assert classify_query("operating profit after tax", lexical_hits=1) == "rrf"


def test_a_question_the_lexical_channel_cannot_score_falls_back_to_the_vector_channel() -> None:
    # A Chinese question over an English corpus: every CJK token misses the index.
    assert classify_query("中国内地的新业务价值是多少", lexical_hits=0) == "vector_only"
    assert classify_query("VONB 1H26", lexical_hits=0) == "vector_only"


def test_the_token_budget_is_counted_on_ragspine_tokens() -> None:
    # Two content words, so only the token count decides; the function words pad it to five.
    question = "alpha beta of the in"
    assert len(tokenize_query(question)) == MAX_BM25_ONLY_TOKENS
    assert content_words(tokenize_query(question)) == ("alpha", "beta")
    assert classify_query(question, lexical_hits=1) == "bm25_only"
    assert classify_query(question + " of", lexical_hits=1) == "rrf"


def test_the_numeric_clause_needs_both_a_figure_and_few_content_words() -> None:
    assert MAX_BM25_ONLY_CONTENT_WORDS == 1
    # Six tokens, so only the numeric clause can route it; one content word is within budget.
    assert len(tokenize_query("What was the VONB in 1H26?")) > MAX_BM25_ONLY_TOKENS
    assert classify_query("What was the VONB in 1H26?", lexical_hits=1) == "bm25_only"
    # One content word more.
    assert classify_query("What was the group VONB in 1H26?", lexical_hits=1) == "rrf"
    # No figure at all.
    assert classify_query("What was the VONB in this half?", lexical_hits=1) == "rrf"


def test_content_words_drop_stopwords_and_figures() -> None:
    assert content_words(tokenize_query("What was the VONB in 1H26?")) == ("vonb",)
    assert content_words(tokenize_query("VONB margin")) == ("vonb", "margin")


@pytest.mark.parametrize("token", ["1h26", "2026", "fy2024", "12", "3m"])
def test_is_numeric_accepts_any_token_carrying_a_figure(token: str) -> None:
    assert is_numeric(token)


@pytest.mark.parametrize("token", ["vonb", "margin", "half"])
def test_is_numeric_rejects_plain_words(token: str) -> None:
    assert not is_numeric(token)


@pytest.mark.parametrize("label", ["1H26", "1H 2026", "FY24", "Q1 2025", "2026", "2026年上半年"])
def test_every_period_label_the_repo_normalises_carries_a_figure(label: str) -> None:
    """The figure test subsumes a period test, so the rule needs only the former."""
    assert find_periods(label)
    assert any(is_numeric(token) for token in tokenize_query(label))


def test_a_blank_question_is_rejected() -> None:
    with pytest.raises(ValueError):
        classify_query("   ", lexical_hits=1)


def test_a_negative_lexical_hit_count_is_rejected() -> None:
    with pytest.raises(ValueError):
        classify_query("VONB 1H26", lexical_hits=-1)


@pytest.mark.parametrize(
    "text",
    [
        "What was the VONB in 1H26?",
        "VONB margin 1H26",
        "中国内地的新业务价值是多少",
        "AIA 2026年上半年 VONB",
        "",
        "   ",
        "what's the OPAT, year-on-year?",
        "Q1 2025 / FY24 — 12.5% up",
    ],
)
def test_the_classifier_counts_the_tokens_the_lexical_channel_scores(text: str) -> None:
    """``answers/`` may import no SDK, so the tokenizer is restated; it must not drift."""
    assert tokenize_query(text) == tokenize(text)


def test_is_label_query_names_the_shape_that_needs_no_map_of_the_document() -> None:
    """ADR 0019 spends no tree-routing call on a label: BM25 already matches it verbatim."""
    assert is_label_query("1H26 Distribution Mix")
    assert is_label_query("VONB 1H26")
    assert is_label_query("What was the VONB in 1H26?")
    assert not is_label_query("Which distribution channels grew new business value this half?")
    assert not is_label_query("Agency share of VONB 1H26")
    # Nothing the lexical channel can tokenize is no label either.
    assert not is_label_query("")
    assert not is_label_query("   ")
    assert not is_label_query("— / —")


@pytest.mark.parametrize(
    "question",
    [
        "VONB 1H26",
        "1H26 Distribution Mix",
        "What was the VONB in 1H26?",
        "Agency share of VONB 1H26",
        "Which distribution channels grew new business value this half?",
        "operating profit after tax",
    ],
)
def test_the_channel_rule_is_the_label_predicate_and_nothing_else(question: str) -> None:
    assert (classify_query(question, lexical_hits=7) == "bm25_only") == is_label_query(question)
