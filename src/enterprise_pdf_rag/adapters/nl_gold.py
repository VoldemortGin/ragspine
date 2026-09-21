"""The frozen natural-language answer gold set: its schema, its self-checks and its judge.

The gold file itself lives beside the chart-QA golds under
``benchmarks/enterprise-pdf-rag/<corpus>/nl-answers-gold-v1.json`` and is pinned to one
published release (``pinned``). This module only parses a payload and judges an observed
answer; it performs no I/O and knows nothing about HTTP or a store, so both runners share
exactly one pass/fail rule and cannot drift apart:

* the offline replay ``tests/enterprise_pdf_rag/answers/test_nl_gold.py`` (real pinned
  evidence, replayed retrieval, scripted model output), and
* the real-model ``scripts/enterprise_pdf_rag/nl_gold_eval.py`` (live ``document-catalog``
  service over ``POST /v1/chat/completions``).

What a case may freeze is limited by what is actually stable across runs. ``field_path``,
``quote``, ``page_index`` and the ``evidence_ids`` are content-addressed and stable;
``claim_id``, ``member_id``, ``processing_id``, ``snapshot_id`` and the answer prose
wording are not, and no expectation names them.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from enterprise_pdf_rag.answers.models import AbstainReason, AnswerStatus

# The assistant message is the verified prose followed by the rendered citation block
# (``adapters/http/chat.render_message``); only the prose may be searched for a number
# that no claim backs. ``test_nl_gold`` re-proves this split against ``render_message``.
CITATION_BLOCK = "\n\n引用:\n"

ClaimKindName = Literal["quote", "cell", "chart_value", "diagram_node", "diagram_edge", "formula"]
ModelAbstainReason = Literal["not_in_context", "ambiguous", "needs_calculation"]
# Citation fields a case may require; ``required_citation_fields`` is checked as
# "present and non-empty", so a nullable field (``bbox``, ``row``) is never demanded.
CITATION_FIELDS = frozenset(
    {"member_id", "kind", "page_index", "field_path", "evidence_ids", "quote", "page_title"}
)


def answer_prose(message: str) -> str:
    """The verified prose of an assistant message, without its rendered citation block."""
    return message.split(CITATION_BLOCK, 1)[0]


class _GoldModel(BaseModel):
    """Same strictness as the chart-QA golds: a frozen file, parsed exactly as written."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class _ObservedModel(BaseModel):
    """The runner's view of one answer; lenient because the envelope keeps growing."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class GoldFilters(_GoldModel):
    """Metadata pre-filters, as a request sends them or as the envelope reports them."""

    periods: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()


class GoldQuestion(_GoldModel):
    """The question in each language it was frozen for; at least one is required."""

    en: str | None = None
    zh: str | None = None

    @model_validator(mode="after")
    def _one_language(self) -> "GoldQuestion":
        if not (self.en or "").strip() and not (self.zh or "").strip():
            raise ValueError("A gold question needs a nonempty `en` or `zh` text")
        return self

    @property
    def text(self) -> str:
        """The text a runner sends: the frozen language, ``en`` first when both exist."""
        return (self.en or self.zh or "").strip()


class GoldRequest(_GoldModel):
    """Request-level extras beyond the question itself."""

    rerank: bool = False
    filters: GoldFilters | None = None
    # Re-send the named case's question immediately before this one so the answer must come
    # from the completion cache. Only the real runner can observe this (the offline replay
    # scripts its own transport), so it is skipped there.
    repeat_of: str | None = None


class RequiredClaim(_GoldModel):
    """One claim the answer must carry, named only by stable, content-addressed anchors."""

    kind: ClaimKindName
    page_index: int
    # Exactly one of the two: the printed path verbatim, or its stable prefix.
    field_path: str | None = None
    field_path_prefix: str | None = None
    # The cited evidence verbatim (whitespace folded before comparison, as `verify._exact`).
    quote: str | None = None
    # The claim's own text, e.g. `72%` for a chart value; the prose wording is never frozen.
    text: str | None = None
    value: str | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def _one_path(self) -> "RequiredClaim":
        if self.page_index < 0:
            raise ValueError("A required claim needs a zero-based page index")
        if (self.field_path is None) == (self.field_path_prefix is None):
            raise ValueError("A required claim names either `field_path` or `field_path_prefix`")
        return self

    def matches_path(self, field_path: str) -> bool:
        if self.field_path is not None:
            return field_path == self.field_path
        assert self.field_path_prefix is not None
        return field_path.startswith(self.field_path_prefix)


class GoldExpectation(_GoldModel):
    """Everything a case asserts about one answer. Omitted fields are not checked."""

    status: AnswerStatus
    abstain_reason: AbstainReason | None = None
    # A substring of `abstain_detail`; the detail carries the model's own reason word.
    abstain_detail_contains: str | None = None
    min_claims: int = 0
    required_claims: tuple[RequiredClaim, ...] = ()
    required_citation_fields: tuple[str, ...] = ()
    # The pinned pages state no unambiguous answer, so *which* verified fact the model
    # grounds on is not frozen - only that it grounded on one, and how it was filtered.
    # A positive case uses this instead of `required_claims`, never beside it.
    grounded_only: bool = False
    # Numbers that must not occur in the answer prose, e.g. a difference nobody printed.
    # An abstention has no prose to police, so the check applies to answered results only.
    forbidden_numbers: tuple[str, ...] = ()
    # Given, the applied pre-filters must equal it exactly; an empty object asserts that
    # no filter was applied.
    filters_expected: GoldFilters | None = None
    filters_relaxed: bool | None = None
    cache_hit: bool | None = None
    # The recorded behaviour is not the behaviour we want. The runner reports such a case
    # separately and never fails the run on it.
    known_gap: bool = False
    known_gap_detail: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "GoldExpectation":
        if self.min_claims < 0:
            raise ValueError("`min_claims` cannot be negative")
        if self.status is AnswerStatus.ANSWERED and self.abstain_reason is not None:
            raise ValueError("An answered expectation cannot also name an abstain reason")
        unknown = set(self.required_citation_fields) - CITATION_FIELDS
        if unknown:
            raise ValueError(f"Unknown required citation fields: {sorted(unknown)}")
        if self.known_gap and not (self.known_gap_detail or "").strip():
            raise ValueError("A known gap must say what the gap is")
        if not self.known_gap and self.known_gap_detail is not None:
            raise ValueError("`known_gap_detail` belongs to a known gap only")
        if self.grounded_only and (self.required_claims or self.min_claims < 1):
            raise ValueError("A grounded-only expectation freezes no claim but still needs one")
        return self


class ScriptedClaim(_GoldModel):
    """One claim of the scripted model output; its member is resolved from the snapshot."""

    claim_id: str
    kind: ClaimKindName
    page_index: int
    field_path: str
    text: str
    row: int | None = None
    col: int | None = None
    header: str | None = None


class ScriptedAnswer(_GoldModel):
    """A legal model output for the offline replay, in the shape of ``answers.prompt``.

    It is not an expectation: verification still re-reads every claim from the pinned
    evidence, so a scripted claim the evidence does not support is rejected exactly as a
    real model's would be. That is how the adversarial cases are built.
    """

    abstain: bool = False
    abstain_reason: ModelAbstainReason | None = None
    answer: str = ""
    claims: tuple[ScriptedClaim, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> "ScriptedAnswer":
        if self.abstain and (self.claims or self.abstain_reason is None):
            raise ValueError("A declining model output carries a reason and no claim")
        if not self.abstain and self.abstain_reason is not None:
            raise ValueError("An answering model output carries no abstain reason")
        return self


class NlGoldCase(_GoldModel):
    case_id: str
    case_class: Literal["positive", "abstain", "adversarial"]
    question: GoldQuestion
    document_sha256: str
    request: GoldRequest = GoldRequest()
    expected: GoldExpectation
    # The offline replay's scripted model output; without it the case is real-model only.
    model_output: ScriptedAnswer | None = None
    # The case never runs against a live service (it scripts an illegal model output).
    offline_only: bool = False
    rationale: str
    # Where the recorded behaviour came from, e.g. a `data/validation/...` response file.
    evidence: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "NlGoldCase":
        if len(self.document_sha256) != 64 or not _is_hex(self.document_sha256):
            raise ValueError("A gold case names its document by sha256")
        if not self.rationale.strip():
            raise ValueError("A gold case must say why it exists")
        if self.case_class == "positive" and self.expected.status is not AnswerStatus.ANSWERED:
            raise ValueError("A positive case must expect an answer")
        if self.case_class != "positive" and self.expected.status is not AnswerStatus.ABSTAINED:
            raise ValueError("An abstain or adversarial case must expect an abstention")
        if self.offline_only and self.model_output is None:
            raise ValueError("An offline-only case needs its scripted model output")
        if self.offline_only and self.request.repeat_of is not None:
            raise ValueError("A cache repeat can only be observed against a live service")
        return self


class PinnedRelease(_GoldModel):
    """The immutable release every case was recorded against."""

    document_sha256: str
    processing_id: str
    snapshot_id: str
    selected_physical_pages: tuple[int, ...]
    member_count: int
    embedding_fingerprint: str

    @model_validator(mode="after")
    def _digests(self) -> "PinnedRelease":
        digests = (self.document_sha256, self.processing_id, self.snapshot_id)
        if any(len(value) != 64 or not _is_hex(value) for value in digests):
            raise ValueError("A pinned release is identified by sha256 digests")
        if not self.selected_physical_pages or self.member_count < 1:
            raise ValueError("A pinned release has selected pages and members")
        return self


class NlGoldSet(_GoldModel):
    """The frozen set. Loading it re-checks that it is internally consistent."""

    schema_version: Literal["nl-answers-gold-v1"]
    corpus: str
    review: tuple[str, ...]
    pinned: PinnedRelease
    minimum_positive_cases: int
    cases: tuple[NlGoldCase, ...]

    @model_validator(mode="after")
    def _validate_set(self) -> "NlGoldSet":
        ids = tuple(case.case_id for case in self.cases)
        if len(set(ids)) != len(ids):
            raise ValueError("Gold case ids must be unique")
        positives = tuple(case for case in self.cases if case.case_class == "positive")
        if len(positives) < self.minimum_positive_cases:
            raise ValueError("The gold set has too few positive cases")
        for case in positives:
            if case.expected.min_claims < 1:
                raise ValueError(f"Positive case {case.case_id} verifies no claim")
            if not case.expected.required_claims and not case.expected.grounded_only:
                raise ValueError(f"Positive case {case.case_id} freezes no claim")
        for case in self.cases:
            if case.request.repeat_of is not None and case.request.repeat_of not in set(ids):
                raise ValueError(f"Case {case.case_id} repeats an unknown case")
            if case.model_output is None:
                continue
            declined = case.model_output.abstain
            if case.case_class == "positive" and declined:
                raise ValueError(f"Positive case {case.case_id} scripts a declining model")
        return self

    def case(self, case_id: str) -> NlGoldCase:
        for case in self.cases:
            if case.case_id == case_id:
                return case
        raise KeyError(case_id)


def load_gold(payload: bytes) -> NlGoldSet:
    """Parse and self-check a frozen natural-language gold set."""
    return NlGoldSet.model_validate_json(payload, strict=True)


class ObservedFilters(_ObservedModel):
    """The applied pre-filters as the envelope reports them (JSON arrays, not tuples)."""

    periods: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()


class ObservedCitation(_ObservedModel):
    member_id: str | None = None
    kind: str | None = None
    page_index: int | None = None
    field_path: str | None = None
    evidence_ids: tuple[str, ...] = ()
    quote: str | None = None
    page_title: str | None = None

    def has(self, field: str) -> bool:
        """Is this citation field present and non-empty, as ``required_citation_fields`` asks?"""
        value = getattr(self, field, None)
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, tuple):
            return bool(value)
        return True


class ObservedClaim(_ObservedModel):
    kind: str
    text: str
    value: str | None = None
    unit: str | None = None
    citations: tuple[ObservedCitation, ...] = ()


class ObservedAnswer(_ObservedModel):
    """The envelope fields a case can assert, parsed straight from ``rag-chat-v1``."""

    status: str
    abstain_reason: str | None = None
    abstain_detail: str | None = None
    claims: tuple[ObservedClaim, ...] = ()
    filters_applied: ObservedFilters | None = None
    filters_relaxed: bool = False
    cache_hit: bool = False


def _is_hex(value: str) -> bool:
    return all(character in "0123456789abcdef" for character in value)


def _fold(text: str) -> str:
    """Whitespace-folded comparison, the criterion ``answers/verify._exact`` already uses."""
    return " ".join(text.split())


def _citation_matches(required: RequiredClaim, citation: ObservedCitation) -> bool:
    if citation.page_index != required.page_index:
        return False
    if citation.field_path is None or not required.matches_path(citation.field_path):
        return False
    return required.quote is None or _fold(citation.quote or "") == _fold(required.quote)


def _claim_failures(
    required: RequiredClaim, observed: ObservedAnswer, fields: tuple[str, ...]
) -> tuple[str, ...]:
    path = required.field_path or f"{required.field_path_prefix}*"
    label = f"{required.kind} p{required.page_index} {path}"
    matched = [
        (claim, citation)
        for claim in observed.claims
        if claim.kind == required.kind
        for citation in claim.citations
        if _citation_matches(required, citation)
    ]
    if not matched:
        return (f"missing required claim: {label}",)
    failures: list[str] = []
    claim, citation = matched[0]
    for name, expected in (
        ("text", required.text),
        ("value", required.value),
        ("unit", required.unit),
    ):
        if expected is None:
            continue
        actual = getattr(claim, name)
        if actual is None or _fold(str(actual)) != _fold(expected):
            failures.append(f"{label}: {name} is {actual!r}, expected {expected!r}")
    missing = tuple(field for field in fields if not citation.has(field))
    if missing:
        failures.append(f"{label}: citation lacks {', '.join(missing)}")
    return tuple(failures)


def judge(
    case: NlGoldCase,
    observed: ObservedAnswer,
    prose: str,
    *,
    skip: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Every way ``observed`` departs from ``case.expected``; empty means the case passed.

    ``skip`` names expectation fields a runner cannot observe — the offline replay scripts
    its own completion transport, so it skips ``cache_hit``.
    """
    expected = case.expected
    failures: list[str] = []
    if observed.status != expected.status.value:
        failures.append(f"status is {observed.status!r}, expected {expected.status.value!r}")
    if expected.abstain_reason is not None:
        if observed.abstain_reason != expected.abstain_reason.value:
            failures.append(
                f"abstain_reason is {observed.abstain_reason!r}, "
                f"expected {expected.abstain_reason.value!r}"
            )
    elif expected.status is AnswerStatus.ANSWERED and observed.abstain_reason is not None:
        failures.append(f"answered but carries abstain_reason {observed.abstain_reason!r}")
    if expected.abstain_detail_contains is not None:
        detail = observed.abstain_detail or ""
        if expected.abstain_detail_contains not in detail:
            failures.append(f"abstain_detail {detail!r} lacks {expected.abstain_detail_contains!r}")
    if len(observed.claims) < expected.min_claims:
        failures.append(f"{len(observed.claims)} claims, expected at least {expected.min_claims}")
    for required in expected.required_claims:
        failures.extend(_claim_failures(required, observed, expected.required_citation_fields))
    if expected.grounded_only:
        # No claim is frozen, but whatever the answer grounded on must still be fully cited.
        for claim in observed.claims:
            citation = claim.citations[0] if claim.citations else ObservedCitation()
            missing = tuple(
                field for field in expected.required_citation_fields if not citation.has(field)
            )
            if missing:
                failures.append(
                    f"{claim.kind} claim {claim.text!r}: citation lacks {', '.join(missing)}"
                )
    if observed.status == AnswerStatus.ANSWERED.value:
        for number in expected.forbidden_numbers:
            if number in prose:
                failures.append(f"answer prose contains the forbidden number {number!r}")
    if expected.filters_expected is not None:
        applied = observed.filters_applied or ObservedFilters()
        wanted = expected.filters_expected
        if (applied.periods, applied.regions) != (wanted.periods, wanted.regions):
            failures.append(
                f"filters_applied is {applied.model_dump()}, expected {wanted.model_dump()}"
            )
    if (
        expected.filters_relaxed is not None
        and observed.filters_relaxed != expected.filters_relaxed
    ):
        failures.append(
            f"filters_relaxed is {observed.filters_relaxed}, expected {expected.filters_relaxed}"
        )
    checks_cache = expected.cache_hit is not None and "cache_hit" not in skip
    if checks_cache and observed.cache_hit != expected.cache_hit:
        failures.append(f"cache_hit is {observed.cache_hit}, expected {expected.cache_hit}")
    return tuple(failures)
