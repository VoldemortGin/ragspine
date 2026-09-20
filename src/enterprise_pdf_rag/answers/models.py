"""Immutable answer-chain values: requests, verified claims, rejections and results."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from enterprise_pdf_rag.figures.chart_qa.displayed_models import DisplayedRefusalReason
from enterprise_pdf_rag.figures.chart_qa.models import FieldCitation, RefusalReason
from enterprise_pdf_rag.processing.context_builder import BlockKind
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    ABSTAINED = "abstained"


class ClaimKind(StrEnum):
    QUOTE = "quote"
    CELL = "cell"
    CHART_VALUE = "chart_value"


class AbstainReason(StrEnum):
    """Superset of both chart-QA refusal enums plus the answer-chain-only reasons."""

    NO_RELEVANT_MEMBER = "no_relevant_member"
    MODEL_DECLINED = "model_declined"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    CLAIM_NOT_IN_EVIDENCE = "claim_not_in_evidence"
    NO_VERIFIED_CLAIM = "no_verified_claim"
    UNQUALIFIED_MEMBER = "unqualified_member"
    UNSUPPORTED_GRAMMAR = "unsupported_grammar"
    UNKNOWN_POINT = "unknown_point"
    SERIES_MISMATCH = "series_mismatch"
    CATEGORY_MISMATCH = "category_mismatch"
    PERIOD_MISMATCH = "period_mismatch"
    UNIT_MISMATCH = "unit_mismatch"
    UNSUPPORTED_VALUE_KIND = "unsupported_value_kind"
    VALUE_UNAVAILABLE = "value_unavailable"
    UNSUPPORTED_PRECISION = "unsupported_precision"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


def from_refusal(reason: RefusalReason | DisplayedRefusalReason) -> AbstainReason:
    """Both chart-QA refusal enums share their member names and values with this one."""
    return AbstainReason(reason.value)


@dataclass(frozen=True, slots=True)
class FusedHit:
    """One member after reciprocal rank fusion, keeping each channel's rank and score."""

    snapshot_id: str
    member_id: str
    fused_score: float
    vector_rank: int | None
    lexical_rank: int | None
    vector_score: float | None
    bm25_score: float | None

    def as_hit(self) -> PinnedRetrievalHit:
        return PinnedRetrievalHit(self.snapshot_id, self.member_id, self.fused_score)


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    question: str
    document_sha256: str | None = None
    top_k: int = 10
    channel_limit: int = 50
    rerank: bool = False
    history: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("A nonempty question is required")
        if self.top_k < 1:
            raise ValueError("top_k must be at least one")
        if self.channel_limit < 1:
            raise ValueError("channel_limit must be at least one")


@dataclass(frozen=True, slots=True)
class ClaimCitation:
    member_id: str
    kind: BlockKind
    page_index: int
    field_path: str
    evidence_ids: tuple[str, ...]
    bbox: tuple[float, float, float, float] | None
    quote: str
    chart_citation: FieldCitation | None = None


@dataclass(frozen=True, slots=True)
class VerifiedClaim:
    claim_id: str
    kind: ClaimKind
    text: str
    value: Decimal | None
    unit: str | None
    citations: tuple[ClaimCitation, ...]


@dataclass(frozen=True, slots=True)
class RejectedClaim:
    claim_id: str
    member_id: str
    field_path: str
    text: str
    reason: AbstainReason
    detail: str


@dataclass(frozen=True, slots=True)
class AnswerResult:
    status: AnswerStatus
    answer: str | None
    claims: tuple[VerifiedClaim, ...]
    rejected: tuple[RejectedClaim, ...]
    abstain_reason: AbstainReason | None
    abstain_detail: str | None
    document_sha256: str
    processing_id: str
    snapshot_id: str
    member_ids: tuple[str, ...]
    fused: tuple[FusedHit, ...]
    request_fingerprint: str | None
    llm_live_calls: int
    cache_hit: bool
