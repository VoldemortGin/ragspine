"""Pick one retrieval channel for a question, deterministically and without a model.

ADR 0018. Over the 125 indexed facts of the 2026-09-21 coverage probe the lexical
channel alone recalls 74.4% within ten seats against 70.4% for RRF fusion, and it leads
at every cut (r@3 54.4% vs 46.4%, MRR 0.482 vs 0.381). Every query that probe carried
has one shape — a short label plus a period — so the rule reroutes only that shape and
leaves longer, narrative questions on fusion, where the probe says nothing.
``MAX_BM25_ONLY_TOKENS`` and ``MAX_BM25_ONLY_CONTENT_WORDS`` are the narrowest pair of
thresholds that reached the offline sweep's ceiling; the sweep table is in the ADR.

A question the lexical channel cannot score at all (``lexical_hits == 0`` — a Chinese
question over an English index) is no fusion candidate either: fusing an empty ranking
with one ranking is that one ranking. It takes the vector channel alone, and the answer
service prefers translating it into the index's language first, falling back to this
mode only when no translation is available (``adapters/query_translation``).

``answers/`` imports no SDK, so ``tokenize_query`` restates the lexical channel's
tokenizer rather than importing it; ``test_query_mode`` pins the two to the same output,
because a token budget is meaningless unless it counts the tokens BM25 actually scores.
"""

import re
from collections.abc import Sequence
from typing import Final, Literal

# CJK unified ideographs (base, extension A, compatibility), as the lexical channel splits
# them: one token per character plus one per adjacent pair.
_CJK_RANGE = "㐀-䶿一-鿿豈-﫿"
_TOKEN_RE = re.compile(rf"[a-z0-9]+|[{_CJK_RANGE}]+")


def tokenize_query(text: str) -> list[str]:
    """The lexical channel's tokenizer, restated: lowercased ASCII runs, CJK uni/bigrams."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        run = match.group(0)
        if run[0].isascii():
            tokens.append(run)
        else:
            tokens.extend(run)
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


QueryMode = Literal["bm25_only", "rrf", "vector_only"]
# What a caller may ask for: ``auto`` defers to ``classify_query``, the rest pin a channel.
FusionMode = Literal["auto", "bm25_only", "rrf", "vector_only"]

MAX_BM25_ONLY_TOKENS: Final = 5
MAX_BM25_ONLY_CONTENT_WORDS: Final = 1

# Function words that carry no retrievable content. Kept deliberately small and English:
# the classifier only ever sees the index's own language (a foreign question is translated
# or sent to the vector channel before it reaches here). ``s`` and ``t`` are what the
# tokenizer leaves behind of ``what's`` and ``don't``.
STOPWORDS: Final = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "for",
        "to",
        "from",
        "by",
        "with",
        "and",
        "or",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "what",
        "which",
        "how",
        "much",
        "many",
        "did",
        "does",
        "do",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "their",
        "his",
        "her",
        "s",
        "t",
    ]
)


def is_numeric(token: str) -> bool:
    """Whether a token carries a figure.

    Every period label ``processing.periods`` normalises (``1H26``, ``FY24``, ``Q1 2025``,
    ``2026年上半年``) contains a digit, so this one test covers figures and periods alike.
    """
    return any(character.isdigit() for character in token)


def content_words(tokens: Sequence[str]) -> tuple[str, ...]:
    """The tokens that name something: neither a function word nor a figure."""
    return tuple(token for token in tokens if token not in STOPWORDS and not is_numeric(token))


def content_probe(question: str) -> str:
    """The question reduced to its content words — what the index must recognise for the
    question to be scoreable at all.

    Figures and function words are dropped on purpose: every financial deck prints years
    and periods, so ``2026`` scores for any question in any language and would hide the
    fact that nothing else in the question is in the index's vocabulary.
    """
    return " ".join(content_words(tokenize_query(question)))


def classify_query(question: str, *, lexical_hits: int) -> QueryMode:
    """The channel to answer ``question`` from, given how many members BM25 could score."""
    if not question.strip():
        raise ValueError("A nonempty question is required")
    if lexical_hits < 0:
        raise ValueError("A lexical hit count cannot be negative")
    tokens = tokenize_query(question)
    if lexical_hits == 0 or not tokens:
        return "vector_only"
    if len(tokens) <= MAX_BM25_ONLY_TOKENS:
        return "bm25_only"
    if any(is_numeric(token) for token in tokens) and (
        len(content_words(tokens)) <= MAX_BM25_ONLY_CONTENT_WORDS
    ):
        return "bm25_only"
    return "rrf"
