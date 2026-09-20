"""Orchestrate one answer: hybrid search → evidence blocks → one model call → verification.

The service never calls a model except through the injected bounded client, and it
does so at most once per request; retrieval, hydration and claim verification are
deterministic reads of the pinned snapshot.
"""

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from enterprise_pdf_rag.adapters.hybrid_search import HybridSearch, LexicalIndex
from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient, JsonCompletionError
from enterprise_pdf_rag.answers.member_filter import candidate_members, region_vocabulary
from enterprise_pdf_rag.answers.models import (
    AbstainReason,
    AnswerRequest,
    AnswerResult,
    AnswerStatus,
    FusedHit,
    MemberFilters,
    VerifiedClaim,
)
from enterprise_pdf_rag.answers.ports import MemberText, MountedDocument
from enterprise_pdf_rag.answers.prompt import SYSTEM_RULES, ModelAnswer, build_prompt
from enterprise_pdf_rag.answers.query_filters import derive_filters
from enterprise_pdf_rag.answers.verify import decide, verify_claims
from enterprise_pdf_rag.figures.chart_qa.displayed_models import (
    DISPLAYED_BAR_SCOPE,
    DisplayedLookupContext,
)
from enterprise_pdf_rag.figures.chart_qa.models import ChartContext
from enterprise_pdf_rag.figures.models import ValueKind
from enterprise_pdf_rag.processing.context_builder import (
    BlockKind,
    ContextBlock,
    budget_blocks,
    build_context_block,
)
from enterprise_pdf_rag.processing.models import ObjectKind
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge

_TASK = "rag-answer-v1"
_MODEL_OUTPUT_FAILURES = frozenset({"invalid_model_json", "truncated_response"})


class UnknownDocument(LookupError):
    """The requested document is not mounted."""


class AmbiguousDocument(ValueError):
    """No document was named and more than one is mounted."""


class DependencyUnavailable(RuntimeError):
    """A required provider (reranker or model transport/budget) is missing or failed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# The visual object kinds that may each hold one guaranteed prompt seat (ADR 0012, extended
# to the ADR 0015 objects); the order fixes which candidate is examined first.
_VISUAL_KINDS: Final = (ObjectKind.CHART, ObjectKind.DIAGRAM, ObjectKind.FORMULA)


def _citable_chart(block: ContextBlock) -> bool:
    """A chart block with at least one explicit value the model could cite."""
    return block.kind is BlockKind.CHART and any(
        field.field_path.endswith(".value")
        and field.value is not None
        and field.value_kind is ValueKind.EXPLICIT
        for field in block.chart_fields
    )


def _citable_visual(block: ContextBlock) -> ObjectKind | None:
    """The visual kind a block can be cited as: a chart with an explicit value, a diagram
    with at least one labelled node, a formula with a linear form; ``None`` otherwise."""
    if _citable_chart(block):
        return ObjectKind.CHART
    if block.kind is BlockKind.DIAGRAM and any(node.label.strip() for node in block.nodes):
        return ObjectKind.DIAGRAM
    if block.kind is BlockKind.FORMULA and block.formula_linear is not None:
        return ObjectKind.FORMULA
    return None


def select_context(
    document: MountedDocument,
    ranked: Sequence[FusedHit],
    top_k: int,
    member_texts: Sequence[MemberText] | None = None,
) -> tuple[tuple[FusedHit, ...], tuple[ContextBlock, ...]]:
    """Hydrate the top-k fused hits, with one guaranteed seat per citable visual kind.

    For each visual kind (chart / diagram / formula) with no citable block in the top-k,
    the first citable member of that kind within the next k fused positions takes a seat,
    given up from the last seat backward and never one that already holds a citable visual
    object (ADR 0012, generalised by ADR 0015's follow-up). A pending or label-only object
    never qualifies, and nothing outside ``2 * top_k`` is promoted. Only members of a still
    missing kind in that window are resolved for the check; ``member_texts`` supplies their
    kinds when the caller already holds them.
    """
    head = list(ranked[:top_k])
    blocks = {hit.member_id: build_context_block(document.resolve(hit.as_hit())) for hit in head}
    seated = {_citable_visual(blocks[hit.member_id]) for hit in head}
    missing = [kind for kind in _VISUAL_KINDS if kind not in seated]
    window = ranked[top_k : 2 * top_k]
    if missing and window:
        texts = document.member_texts() if member_texts is None else member_texts
        kinds = {member.member_id: member.kind for member in texts}
        evictable = [
            seat
            for seat in range(len(head) - 1, -1, -1)
            if _citable_visual(blocks[head[seat].member_id]) is None
        ]
        for hit in window:
            if not missing or not evictable:
                break
            kind = kinds.get(hit.member_id)
            if kind not in missing:
                continue
            block = build_context_block(document.resolve(hit.as_hit()))
            if _citable_visual(block) is not kind:
                continue
            blocks[hit.member_id] = block
            head[evictable.pop(0)] = hit
            missing.remove(kind)
    return tuple(head), tuple(blocks[hit.member_id] for hit in head)


@dataclass(frozen=True, slots=True)
class AnswerSettings:
    rrf_k: float = 60.0
    prompt_budget_chars: int = 18_000
    max_output_tokens: int = 1024


class AnswerService:
    def __init__(
        self,
        documents: Mapping[str, MountedDocument],
        llm: JsonCompletionClient,
        *,
        settings: AnswerSettings | None = None,
        reranker: ListwiseJudge | None = None,
        index_cache: MutableMapping[str, LexicalIndex] | None = None,
    ) -> None:
        self._documents = documents
        self._llm = llm
        self._settings = AnswerSettings() if settings is None else settings
        self._reranker = reranker
        self._index_cache: MutableMapping[str, LexicalIndex] = (
            {} if index_cache is None else index_cache
        )

    def _select(self, document_sha256: str | None) -> MountedDocument:
        if document_sha256 is not None:
            document = self._documents.get(document_sha256)
            if document is None:
                raise UnknownDocument(document_sha256)
            return document
        if len(self._documents) != 1:
            raise AmbiguousDocument(
                f"{len(self._documents)} documents are mounted; name one by sha256"
            )
        return next(iter(self._documents.values()))

    def answer(self, request: AnswerRequest) -> AnswerResult:
        document = self._select(request.document_sha256)
        reranker = None
        if request.rerank:
            if self._reranker is None:
                raise DependencyUnavailable("rerank requested but no reranker is configured")
            reranker = self._reranker
        search = HybridSearch(
            document,
            channel_limit=request.channel_limit,
            rrf_k=self._settings.rrf_k,
            reranker=reranker,
            index_cache=self._index_cache,
        )
        members = search.index.members
        filters = (
            derive_filters(request.question, region_vocabulary(members))
            if request.filters is None
            else request.filters
        )
        applied: MemberFilters | None = None if filters.is_empty else filters
        allowed = candidate_members(members, applied)
        starved = allowed is not None and len(allowed) < request.top_k
        # Fewer candidates than seats: the narrowing is dropped; when a filter was in play
        # the result says so (the built-in cover / agenda exclusion is not reported).
        relaxed = starved and applied is not None
        if starved:
            allowed = None
        ranked = search.search(request.question, top_k=2 * request.top_k, allowed=allowed)
        fused, selected = select_context(document, ranked, request.top_k, members)
        hits = {hit.member_id: hit.as_hit() for hit in fused}
        blocks = budget_blocks(selected, max_chars=self._settings.prompt_budget_chars)
        if not blocks:
            return self._abstained(
                document,
                fused,
                (),
                AbstainReason.NO_RELEVANT_MEMBER,
                "no retrieved member fits the context budget"
                if fused
                else "no member matched in either channel",
                filters_applied=applied,
                filters_relaxed=relaxed,
            )
        member_ids = tuple(block.member_id for block in blocks)
        before = self._llm.live_call_count
        try:
            completion = self._llm.complete_text_json(
                task=_TASK,
                prompt=build_prompt(request.question, blocks, request.history),
                response_model=ModelAnswer,
                system=SYSTEM_RULES,
                max_output_tokens=self._settings.max_output_tokens,
            )
        except JsonCompletionError as error:
            if error.code in _MODEL_OUTPUT_FAILURES:
                return self._abstained(
                    document,
                    fused,
                    member_ids,
                    AbstainReason.MODEL_OUTPUT_INVALID,
                    error.code,
                    request_fingerprint=error.request_fingerprint or None,
                    llm_live_calls=self._llm.live_call_count - before,
                    filters_applied=applied,
                    filters_relaxed=relaxed,
                )
            raise DependencyUnavailable(error.code) from error
        by_member = {block.member_id: block for block in blocks}

        def chart_evidence(member_id: str) -> ChartContext | DisplayedLookupContext:
            block: ContextBlock = by_member[member_id]
            if block.scope == DISPLAYED_BAR_SCOPE:
                return document.displayed_context(hits[member_id])
            return document.chart_context(hits[member_id])

        model = completion.parsed
        verification = verify_claims(model, by_member, chart_evidence=chart_evidence)
        status, reason, detail = decide(
            model, verification, blocks_present=True, question=request.question
        )
        return AnswerResult(
            status,
            model.answer if status is AnswerStatus.ANSWERED else None,
            _with_page_titles(verification.verified, members),
            verification.rejected,
            reason,
            detail,
            document.source_sha256,
            document.processing_id,
            document.retrieval_snapshot_id,
            member_ids,
            fused,
            completion.request_fingerprint,
            self._llm.live_call_count - before,
            completion.cache_hit,
            applied,
            relaxed,
        )

    @staticmethod
    def _abstained(
        document: MountedDocument,
        fused: tuple[FusedHit, ...],
        member_ids: tuple[str, ...],
        reason: AbstainReason,
        detail: str,
        *,
        request_fingerprint: str | None = None,
        llm_live_calls: int = 0,
        filters_applied: MemberFilters | None = None,
        filters_relaxed: bool = False,
    ) -> AnswerResult:
        return AnswerResult(
            AnswerStatus.ABSTAINED,
            None,
            (),
            (),
            reason,
            detail,
            document.source_sha256,
            document.processing_id,
            document.retrieval_snapshot_id,
            member_ids,
            fused,
            request_fingerprint,
            llm_live_calls,
            False,
            filters_applied,
            filters_relaxed,
        )


def _with_page_titles(
    claims: tuple[VerifiedClaim, ...], members: Sequence[MemberText]
) -> tuple[VerifiedClaim, ...]:
    """Label each citation with its member's verified page title, when the page has one."""
    titles = {member.member_id: member.page_title for member in members if member.page_title}
    if not titles:
        return claims
    return tuple(
        replace(
            claim,
            citations=tuple(
                replace(citation, page_title=titles.get(citation.member_id))
                for citation in claim.citations
            ),
        )
        for claim in claims
    )
