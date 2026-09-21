"""Public ``rag-chat-v1`` contract: OpenAI-compatible chat whose answers cite verified evidence."""

from typing import Annotated, Literal

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
    FusedHit,
    MemberFilters,
    PageWindowStat,
    RejectedClaim,
    TranslatedQuery,
    VerifiedClaim,
)
from enterprise_pdf_rag.answers.query_mode import QueryMode
from enterprise_pdf_rag.figures.chart_qa.models import FieldCitation
from enterprise_pdf_rag.processing.context_builder import BlockKind

_FilterValue = Annotated[str, Field(min_length=1, max_length=64)]


class MemberFiltersIn(BoundaryModel):
    """Explicit metadata pre-filters (ADR 0013); omitted, they are derived from the question.

    ``periods`` take any printed form (``1H26``, ``FY2024``, ``2026``); a bare year matches
    every period of that year. ``regions`` must match the document's own verified region
    strings verbatim (case-insensitive). An empty object disables filtering.
    """

    periods: list[_FilterValue] = Field(default_factory=list, max_length=8)
    regions: list[_FilterValue] = Field(default_factory=list, max_length=8)

    def to_domain(self) -> MemberFilters:
        return MemberFilters(tuple(self.periods), tuple(self.regions))


class MemberFiltersOut(BoundaryModel):
    periods: tuple[str, ...]
    regions: tuple[str, ...]

    @classmethod
    def from_domain(cls, filters: MemberFilters) -> "MemberFiltersOut":
        return cls(periods=filters.periods, regions=filters.regions)


class RagChatRequest(BoundaryModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1, max_length=32)
    stream: bool = False
    stream_options: StreamOptions | None = None
    # A document sha256 or a prefix of at least twelve hex digits; wins over ``model``.
    document: str | None = Field(default=None, pattern=r"^[0-9a-f]{12,64}$")
    rerank: bool = False
    filters: MemberFiltersIn | None = None
    # Print the rest of each hit's page beside it (ADR 0017); omitted, the server default wins.
    page_window: bool | None = None


class ClaimCitationOut(BoundaryModel):
    member_id: str
    kind: BlockKind
    page_index: int
    field_path: str
    evidence_ids: tuple[str, ...]
    bbox: tuple[float, float, float, float] | None
    quote: str
    chart_citation: FieldCitation | None
    # The verified title of the cited page, when its metadata stage found one (ADR 0013).
    page_title: str | None = None
    # The cell's proved grid position and header, when the table's grid re-proved (ADR 0014).
    row: int | None = None
    col: int | None = None
    header: str | None = None
    header_cell_id: str | None = None

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
            page_title=citation.page_title,
            row=citation.row,
            col=citation.col,
            header=citation.header,
            header_cell_id=citation.header_cell_id,
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


class MemberRankOut(BoundaryModel):
    """How one prompt member ranked in each retrieval channel and after fusion."""

    member_id: str
    fused_score: float
    vector_rank: int | None
    lexical_rank: int | None
    vector_score: float | None
    bm25_score: float | None

    @classmethod
    def from_domain(cls, hit: FusedHit) -> "MemberRankOut":
        return cls(
            member_id=hit.member_id,
            fused_score=hit.fused_score,
            vector_rank=hit.vector_rank,
            lexical_rank=hit.lexical_rank,
            vector_score=hit.vector_score,
            bm25_score=hit.bm25_score,
        )


class PageWindowOut(BoundaryModel):
    """One page context block that reached the prompt: the rest of a hit's page."""

    page_index: int
    member_count: int
    chars: int
    truncated: bool

    @classmethod
    def from_domain(cls, window: PageWindowStat) -> "PageWindowOut":
        return cls(
            page_index=window.page_index,
            member_count=window.member_count,
            chars=window.chars,
            truncated=window.truncated,
        )


class QueryTranslationOut(BoundaryModel):
    """The English restatement the two retrieval channels scored (ADR 0018).

    Present only when the question was not in the index's language. The answer itself is
    written in the question's language and every claim quotes the evidence verbatim, so
    nothing here was translated on the way out.
    """

    english: str
    source_language: str
    cache_hit: bool

    @classmethod
    def from_domain(cls, translation: TranslatedQuery) -> "QueryTranslationOut":
        return cls(
            english=translation.english,
            source_language=translation.source_language,
            cache_hit=translation.cache_hit,
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
    # One entry per prompt member, in ``member_ids`` order (added after rag-chat-v1 shipped).
    member_ranks: tuple[MemberRankOut, ...] = ()
    # Metadata pre-filters that narrowed the candidates, and whether they had to be dropped
    # because they left fewer candidates than prompt seats (ADR 0013).
    filters_applied: MemberFiltersOut | None = None
    filters_relaxed: bool = False
    # The page context printed beside the hits, one entry per block (added after
    # rag-chat-v1 shipped; ADR 0017). Empty when the page window is off.
    page_windows: tuple[PageWindowOut, ...] = ()
    # Which retrieval channels produced ``member_ranks``, and the query translation that fed
    # them when the question was not in the index's language (ADR 0018).
    fusion_mode: QueryMode = "rrf"
    query_translation: QueryTranslationOut | None = None

    @classmethod
    def from_domain(cls, result: AnswerResult) -> "AnswerEnvelope":
        by_member = {hit.member_id: hit for hit in result.fused}
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
            member_ranks=tuple(
                MemberRankOut.from_domain(by_member[member_id])
                for member_id in result.member_ids
                if member_id in by_member
            ),
            filters_applied=None
            if result.filters_applied is None
            else MemberFiltersOut.from_domain(result.filters_applied),
            filters_relaxed=result.filters_relaxed,
            page_windows=tuple(PageWindowOut.from_domain(window) for window in result.page_windows),
            fusion_mode=result.fusion_mode,
            query_translation=None
            if result.query_translation is None
            else QueryTranslationOut.from_domain(result.query_translation),
        )


class RagCompletionResponse(CompletionResponse):
    enterprise_pdf_rag: AnswerEnvelope


class RagCompletionChunk(CompletionChunk):
    """Stream trailer: empty ``choices`` plus the verified envelope, sent before ``[DONE]``."""

    enterprise_pdf_rag: AnswerEnvelope
