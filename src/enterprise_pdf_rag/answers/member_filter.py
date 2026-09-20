"""Narrow the retrieval candidates by verified page metadata before any channel scores them.

Only two dimensions filter: periods (matched by year, or exactly when the query names a
half / quarter / fiscal year) and regions (verbatim, case-insensitive, from the
document's own vocabulary). A member whose page carries no metadata for a requested
dimension is not a hit. Cover and agenda pages never enter the candidates by default.
Nothing here is final: the service relaxes the narrowing when it starves the ranking.
"""

from collections.abc import Sequence

from enterprise_pdf_rag.answers.models import MemberFilters
from enterprise_pdf_rag.answers.ports import MemberText
from enterprise_pdf_rag.processing.periods import normalize_period, period_matches

EXCLUDED_PAGE_TYPES = frozenset({"cover", "agenda"})


def _canonical(period: str) -> str:
    return normalize_period(period) or period


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def member_matches(member: MemberText, filters: MemberFilters) -> bool:
    if filters.periods and not any(
        period_matches(_canonical(wanted), have)
        for wanted in filters.periods
        for have in member.periods
    ):
        return False
    return not filters.regions or any(
        _fold(wanted) == _fold(have) for wanted in filters.regions for have in member.regions
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
