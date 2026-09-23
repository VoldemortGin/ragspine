"""Derive metadata filters from a question, deterministically and without a model.

Periods come from the same rules that normalised the pages (``processing.periods``).
Regions are matched only against the document's own verified vocabulary — the region
strings its pages print — case-insensitively and verbatim, so no external gazetteer
and no company name is ever hardcoded.
"""

import re
from collections import Counter
from collections.abc import Collection, Iterable

from enterprise_pdf_rag.answers.models import MemberFilters
from ragspine.extraction.evidence.metadata.periods import find_periods, period_year

_CJK = re.compile(r"[㐀-鿿]{2,}")
_LATIN = re.compile(r"[A-Za-z][A-Za-z&'.-]{2,}")
# Report words that appear in almost every cover title and carry no identity.
_GENERIC = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "results",
        "result",
        "report",
        "interim",
        "annual",
        "presentation",
        "group",
        "limited",
        "ltd",
        "inc",
        "plc",
        "company",
        "corp",
        "corporation",
        "holdings",
        "first",
        "half",
        "full",
        "year",
        "quarter",
        "financial",
        "update",
        "overview",
        "summary",
    }
)


def fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def extract_periods(question: str) -> tuple[str, ...]:
    """Canonical period mentions in the question (``1H26`` → ``1H2026``, ``2026`` → ``Y2026``)."""
    return find_periods(question)


def extract_years(question: str) -> tuple[int, ...]:
    years: list[int] = []
    for period in find_periods(question):
        year = period_year(period)
        if year is not None and year not in years:
            years.append(year)
    return tuple(years)


def _mentions(question: str, term: str, *, exact_short_caps: bool = True) -> bool:
    """Case-insensitive verbatim mention; Latin terms must stand as whole words.

    ``question`` is the raw text; folding happens here so that a short all-caps region
    (``US``, ``HK``) can keep its case and not match the pronoun ``us``. Title words
    (``AIA``) are matched case-insensitively.
    """
    folded_term = fold(term)
    if not folded_term:
        return False
    if not folded_term.isascii():
        return folded_term in fold(question)
    exact = exact_short_caps and len(folded_term) <= 3 and term.isupper()
    haystack = " ".join(question.split()) if exact else fold(question)
    needle = re.escape(term.strip() if exact else folded_term)
    return re.search(rf"(?<![A-Za-z0-9]){needle}(?![A-Za-z0-9])", haystack) is not None


def match_regions(question: str, vocabulary: Iterable[str]) -> tuple[str, ...]:
    """Vocabulary entries the question mentions verbatim (case-insensitively), in vocabulary order."""
    matched: list[str] = []
    for term in vocabulary:
        if _mentions(question, term) and term not in matched:
            matched.append(term)
    return tuple(matched)


def derive_filters(question: str, region_vocabulary: Iterable[str]) -> MemberFilters:
    return MemberFilters(extract_periods(question), match_regions(question, region_vocabulary))


def title_tokens(title: str | None) -> frozenset[str]:
    """The folded Latin words and CJK bigrams of a title that could identify a document."""
    if not title:
        return frozenset()
    latin = {token.casefold() for token in _LATIN.findall(title)} - _GENERIC
    cjk = {run[i : i + 2] for run in _CJK.findall(title) for i in range(len(run) - 1)}
    return frozenset(latin | cjk)


def shared_title_tokens(titles: Iterable[str | None]) -> frozenset[str]:
    """Tokens that occur in more than one of the given titles."""
    counts = Counter(token for title in titles for token in title_tokens(title))
    return frozenset(token for token, count in counts.items() if count > 1)


def title_matches(question: str, title: str | None, *, shared: Collection[str] = ()) -> bool:
    """Does the question name this document? A distinctive title token must occur.

    ``shared`` lists tokens other mounted titles also carry; they cannot tell documents
    apart and are ignored.
    """
    folded = fold(question)
    for token in title_tokens(title) - set(shared):
        if token.isascii():
            if _mentions(question, token, exact_short_caps=False):
                return True
        elif token in folded:
            return True
    return False
