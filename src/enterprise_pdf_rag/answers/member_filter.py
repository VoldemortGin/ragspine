"""Narrow the retrieval candidates by verified page metadata before any channel scores them.

Only two dimensions filter: periods (matched by year, or exactly when the query names a
half / quarter / fiscal year) and regions (case-insensitively, by whole word, from the
document's own vocabulary). A member whose page carries no metadata for a requested
dimension is not a hit. Cover and agenda pages never enter the candidates by default.
Nothing here is final: the service relaxes the narrowing when it starves the ranking.
"""

import re
from collections.abc import Sequence

from enterprise_pdf_rag.answers.models import MemberFilters
from enterprise_pdf_rag.answers.ports import MemberText
from ragspine.extraction.evidence.metadata.periods import normalize_period, period_matches

EXCLUDED_PAGE_TYPES = frozenset({"cover", "agenda"})
# Words, split on whitespace and punctuation alike, so ``Taiwan (China)`` is two of them.
_WORD = re.compile(r"[^\W_]+")
_EXCLUDING_PREFIXES = ("ex-", "non-")
_EXCLUDING_WORDS = frozenset({"ex", "excluding"})


def _canonical(period: str) -> str:
    return normalize_period(period) or period


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def _excludes(region: str) -> bool:
    """Whether a region value names a place in order to leave it out (``Asia ex-Japan``)."""
    return any(
        word.startswith(_EXCLUDING_PREFIXES) or word in _EXCLUDING_WORDS
        for word in region.casefold().split()
    )


def _region_matches(wanted: str, have: str) -> bool:
    """Whether the page's region value satisfies the filter, by whole word.

    The pages qualify one place several ways — ``Thailand`` and ``AIA Thailand``, ``Hong Kong``
    and ``Hong Kong Special Administrative Region`` — so equality would drop most of them.
    Every word of the filter must appear as a whole word in the page's value instead, which
    keeps a broader filter matching a narrower value but not the other way round. A value that
    excludes the place is the opposite claim, not a narrower one, and never matches.
    """
    if _excludes(have):
        return False
    words = set(_WORD.findall(have.casefold()))
    wanted_words = _WORD.findall(wanted.casefold())
    return bool(wanted_words) and all(word in words for word in wanted_words)


def member_matches(member: MemberText, filters: MemberFilters) -> bool:
    if filters.periods and not any(
        period_matches(_canonical(wanted), have)
        for wanted in filters.periods
        for have in member.periods
    ):
        return False
    # A member the page geometry could name on its own answers for itself; everyone else
    # still answers for the whole page.
    have_regions = member.member_regions or member.regions
    return not filters.regions or any(
        _region_matches(wanted, have) for wanted in filters.regions for have in have_regions
    )


def candidate_members(
    members: Sequence[MemberText], filters: MemberFilters | None
) -> frozenset[str] | None:
    """Member ids allowed into ranking, or ``None`` when nothing narrows the corpus."""
    allowed: list[str] = []
    for member in members:
        if member.page_type in EXCLUDED_PAGE_TYPES:
            continue
        if filters is not None and not member_matches(member, filters):
            continue
        allowed.append(member.member_id)
    if len(allowed) == len(members):
        return None
    return frozenset(allowed)


def region_vocabulary(members: Sequence[MemberText]) -> tuple[str, ...]:
    """Every verified region string in the corpus, deduplicated case-insensitively."""
    vocabulary: list[str] = []
    seen: set[str] = set()
    for member in members:
        for region in member.regions:
            key = _fold(region)
            if key not in seen:
                seen.add(key)
                vocabulary.append(region)
    return tuple(vocabulary)
