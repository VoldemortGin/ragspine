"""Query classification picks a retrieval channel from the question alone."""

import pytest

from enterprise_pdf_rag.answers.query_mode import (
    MAX_BM25_ONLY_CONTENT_WORDS,
    MAX_BM25_ONLY_TOKENS,
    classify_query,
    content_words,
    is_numeric,
    tokenize_query,
)
from enterprise_pdf_rag.processing.periods import find_periods
from ragspine.retrieval.lexical.retrieval import tokenize


@pytest.mark.parametrize(
    "question",
    [
        "VONB 1H26",
        "VONB margin 1H26",
        "What was VONB in 1H26?",
        "operating profit after tax",
        "EV equity 2026",
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


def test_a_question_the_lexical_channel_cannot_score_falls_back_to_the_vector_channel() -> None:
    # A Chinese question over an English corpus: every CJK token misses the index.
    assert classify_query("中国内地的新业务价值是多少", lexical_hits=0) == "vector_only"
    assert classify_query("VONB 1H26", lexical_hits=0) == "vector_only"


def test_the_token_budget_is_counted_on_ragspine_tokens() -> None:
    question = "alpha beta gamma delta epsilon"
    assert len(tokenize_query(question)) == MAX_BM25_ONLY_TOKENS
    assert classify_query(question, lexical_hits=1) == "bm25_only"
    assert classify_query(question + " zeta", lexical_hits=1) == "rrf"


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
