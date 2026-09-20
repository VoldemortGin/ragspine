"""Public ``rag-chat-v1`` contract: OpenAI-compatible chat whose answers cite verified evidence."""

from typing import Literal

from pydantic import Field

from enterprise_pdf_rag.adapters.http.openai_schemas import (
    ChatMessage,
    CompletionChunk,
    CompletionResponse,
    StreamOptions,
)
from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.answers.models import (
    AbstainReason,
    AnswerResult,
    AnswerStatus,
    ClaimCitation,
    ClaimKind,
    RejectedClaim,
    VerifiedClaim,
)
from enterprise_pdf_rag.figures.chart_qa.models import FieldCitation
from enterprise_pdf_rag.processing.context_builder import BlockKind


class RagChatRequest(BoundaryModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1, max_length=32)
    stream: bool = False
    stream_options: StreamOptions | None = None
    # A document sha256 or a prefix of at least twelve hex digits; wins over ``model``.
    document: str | None = Field(default=None, pattern=r"^[0-9a-f]{12,64}$")
    rerank: bool = False


class ClaimCitationOut(BoundaryModel):
    member_id: str
    kind: BlockKind
    page_index: int
    field_path: str
    evidence_ids: tuple[str, ...]
    bbox: tuple[float, float, float, float] | None
    quote: str
    chart_citation: FieldCitation | None

    @classmethod
    def from_domain(cls, citation: ClaimCitation) -> "ClaimCitationOut":
        return cls(
            member_id=citation.member_id,
            kind=citation.kind,
            page_index=citation.page_index,
            field_path=citation.field_path,
            evidence_ids=citation.evidence_ids,
            bbox=citation.bbox,
            quote=citation.quote,
            chart_citation=citation.chart_citation,
        )


class ClaimOut(BoundaryModel):
    claim_id: str
    kind: ClaimKind
    text: str
    value: str | None
    unit: str | None
    citations: tuple[ClaimCitationOut, ...]

    @classmethod
    def from_domain(cls, claim: VerifiedClaim) -> "ClaimOut":
        return cls(
            claim_id=claim.claim_id,
            kind=claim.kind,
            text=claim.text,
            value=None if claim.value is None else str(claim.value),
            unit=claim.unit,
            citations=tuple(ClaimCitationOut.from_domain(item) for item in claim.citations),
        )


class RejectedClaimOut(BoundaryModel):
    claim_id: str
    member_id: str
    field_path: str
    text: str
    reason: AbstainReason
    detail: str

    @classmethod
    def from_domain(cls, claim: RejectedClaim) -> "RejectedClaimOut":
        return cls(
            claim_id=claim.claim_id,
            member_id=claim.member_id,
            field_path=claim.field_path,
            text=claim.text,
            reason=claim.reason,
            detail=claim.detail,
        )


class AnswerEnvelope(BoundaryModel):
    """Verified claims, audit rejections and pinned provenance beside the OpenAI shape."""

    schema_version: Literal["rag-chat-v1"] = "rag-chat-v1"
    status: AnswerStatus
    abstain_reason: AbstainReason | None
    abstain_detail: str | None
    document_sha256: str
    processing_id: str
    snapshot_id: str
    member_ids: tuple[str, ...]
    claims: tuple[ClaimOut, ...]
    rejected: tuple[RejectedClaimOut, ...]
    llm_live_calls: int
    cache_hit: bool

    @classmethod
    def from_domain(cls, result: AnswerResult) -> "AnswerEnvelope":
        return cls(
            status=result.status,
            abstain_reason=result.abstain_reason,
            abstain_detail=result.abstain_detail,
            document_sha256=result.document_sha256,
            processing_id=result.processing_id,
            snapshot_id=result.snapshot_id,
            member_ids=result.member_ids,
            claims=tuple(ClaimOut.from_domain(claim) for claim in result.claims),
            rejected=tuple(RejectedClaimOut.from_domain(claim) for claim in result.rejected),
            llm_live_calls=result.llm_live_calls,
            cache_hit=result.cache_hit,
        )


class RagCompletionResponse(CompletionResponse):
    enterprise_pdf_rag: AnswerEnvelope


class RagCompletionChunk(CompletionChunk):
    """Stream trailer: empty ``choices`` plus the verified envelope, sent before ``[DONE]``."""

    enterprise_pdf_rag: AnswerEnvelope
