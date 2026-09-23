"""Deterministic period normalisation: one printed period label → one comparable form.

``1H26``, ``1H 2026``, ``H1'26`` and ``2026年上半年`` all become ``1H2026``; ``FY24`` /
``2024财年`` become ``FY2024``; ``Q1 2025`` / ``1Q25`` / ``2025年第一季度`` become
``Q1-2025``; a bare four-digit year becomes ``Y2025``. Anything else stays
unnormalised (``None``): nothing is inferred from a label the rules do not know.
A year-only query matches every period of that year; any finer query matches only
its exact canonical form.
"""

import re

_Y4 = r"(?:19|20)\d{2}"
_Y = rf"(?:{_Y4}|\d{{2}})"
_CN_QUARTER = {"一": "1", "二": "2", "三": "3", "四": "4"}
_CN_HALF = {"上": "1", "下": "2"}
# Ordered from the most specific label to the bare year. Group names carry the
# rule index so one combined scanner can tell which rule matched.
_RULES: tuple[tuple[str, str], ...] = (
    ("H", rf"(?P<h{{i}}>[12])H\s*(?:FY)?\s*'?(?P<y{{i}}>{_Y})"),
    ("H", rf"H(?P<h{{i}}>[12])\s*(?:FY)?\s*'?(?P<y{{i}}>{_Y})"),
    ("H", rf"(?P<y{{i}}>{_Y4})\s*年?\s*(?P<h{{i}}>上|下)半年"),
    ("H", rf"(?P<y{{i}}>{_Y4})\s*H(?P<h{{i}}>[12])"),
    ("Q", rf"Q(?P<q{{i}}>[1-4])\s*(?:FY)?\s*'?(?P<y{{i}}>{_Y})"),
    ("Q", rf"(?P<q{{i}}>[1-4])Q\s*(?:FY)?\s*'?(?P<y{{i}}>{_Y})"),
    ("Q", rf"(?P<y{{i}}>{_Y4})\s*年?\s*Q(?P<q{{i}}>[1-4])"),
    ("Q", rf"(?P<y{{i}}>{_Y4})\s*年?\s*第?(?P<q{{i}}>[一二三四1-4])季度?"),
    ("FY", rf"FY\s*'?(?P<y{{i}}>{_Y})"),
    ("FY", rf"(?P<y{{i}}>{_Y4})\s*(?:财年|年度|财政年度)"),
    ("Y", rf"(?P<y{{i}}>{_Y4})(?:\s*年)?"),
)
_RULE_PATTERNS = tuple(
    (kind, pattern.replace("{i}", str(index))) for index, (kind, pattern) in enumerate(_RULES)
)
_FULL = tuple(
    (index, kind, re.compile(rf"^{pattern}$", re.IGNORECASE))
    for index, (kind, pattern) in enumerate(_RULE_PATTERNS)
)
_SCAN = re.compile(
    "|".join(f"(?P<r{index}>{pattern})" for index, (_, pattern) in enumerate(_RULE_PATTERNS)),
    re.IGNORECASE,
)
_CANONICAL = re.compile(
    r"^(?:(?P<h>[12])H(?P<hy>\d{4})|Q(?P<q>[1-4])-(?P<qy>\d{4})|FY(?P<fy>\d{4})|Y(?P<y>\d{4}))$"
)


def _year(text: str) -> int:
    value = int(text)
    return value if value >= 100 else 2000 + value


def _canonical(index: int, kind: str, groups: dict[str, str | None]) -> str | None:
    year_text = groups.get(f"y{index}")
    if year_text is None:
        return None
    year = _year(year_text)
    if kind == "H":
        half = groups[f"h{index}"] or ""
        return f"{_CN_HALF.get(half, half)}H{year}"
    if kind == "Q":
        quarter = groups[f"q{index}"] or ""
        return f"Q{_CN_QUARTER.get(quarter, quarter)}-{year}"
    if kind == "FY":
        return f"FY{year}"
    return f"Y{year}"


def normalize_period(text: str) -> str | None:
    """The canonical form of one whole period label, or ``None`` when no rule applies."""
    label = " ".join(text.split())
    for index, kind, pattern in _FULL:
        match = pattern.match(label)
        if match is not None:
            return _canonical(index, kind, match.groupdict())
    return None


def _ascii_word(character: str) -> bool:
    return character.isascii() and character.isalnum()


def find_periods(text: str) -> tuple[str, ...]:
    """Every period label the rules recognise inside free text, canonical and deduplicated.

    A label glued to other ASCII letters or digits (``A1H26``, ``2026x``) is not a
    period mention; CJK neighbours are fine (``2026年上半年的``).
    """
    found: list[str] = []
    for match in _SCAN.finditer(text):
        start, end = match.start(), match.end()
        if start and _ascii_word(text[start - 1]):
            continue
        if end < len(text) and _ascii_word(text[end]):
            continue
        groups = match.groupdict()
        index = next(index for index in range(len(_RULES)) if groups.get(f"r{index}") is not None)
        canonical = _canonical(index, _RULES[index][0], groups)
        if canonical is not None and canonical not in found:
            found.append(canonical)
    return tuple(found)


def period_year(canonical: str) -> int | None:
    match = _CANONICAL.match(canonical)
    if match is None:
        return None
    return int(next(value for value in match.groups() if value is not None and len(value) == 4))


def period_matches(query: str, candidate: str) -> bool:
    """A year-only query matches any period of that year; otherwise forms must be equal."""
    query_year = period_year(query)
    candidate_year = period_year(candidate)
    if query_year is None or candidate_year is None or query_year != candidate_year:
        return False
    return query.startswith("Y") or query == candidate
