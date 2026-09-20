"""Orchestrate one answer: hybrid search → evidence blocks → one model call → verification.

The service never calls a model except through the injected bounded client, and it
does so at most once per request; retrieval, hydration and claim verification are
deterministic reads of the pinned snapshot.
"""

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass

from enterprise_pdf_rag.adapters.hybrid_search import HybridSearch, LexicalIndex
from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient, JsonCompletionError
from enterprise_pdf_rag.answers.models import (
    AbstainReason,
    AnswerRequest,
    AnswerResult,
    AnswerStatus,
    FusedHit,
)
from enterprise_pdf_rag.answers.ports import MountedDocument
from enterprise_pdf_rag.answers.prompt import SYSTEM_RULES, ModelAnswer, build_prompt
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


def _citable_chart(block: ContextBlock) -> bool:
    """A chart block with at least one explicit value the model could cite."""
    return block.kind is BlockKind.CHART and any(
        field.field_path.endswith(".value")
        and field.value is not None
        and field.value_kind is ValueKind.EXPLICIT
        for field in block.chart_fields
    )


def select_context(
    document: MountedDocument, ranked: Sequence[FusedHit], top_k: int
) -> tuple[tuple[FusedHit, ...], tuple[ContextBlock, ...]]:
    """Hydrate the top-k fused hits, with one guaranteed seat for a citable chart.

    When no hit in the top-k is a chart with an explicit value but one sits within the
    next k fused positions, it replaces the last seat (ADR 0012). A pending or
    label-only chart never qualifies, and nothing outside ``2 * top_k`` is promoted.
    Only chart members in that window are resolved for the check.
    """
    head = list(ranked[:top_k])
    blocks = {hit.member_id: build_context_block(document.resolve(hit.as_hit())) for hit in head}
    if head and not any(_citable_chart(blocks[hit.member_id]) for hit in head):
        kinds: dict[str, ObjectKind] | None = None
        for hit in ranked[top_k : 2 * top_k]:
            if kinds is None:
                kinds = {member.member_id: member.kind for member in document.member_texts()}
            if kinds.get(hit.member_id) is not ObjectKind.CHART:
                continue
            block = build_context_block(document.resolve(hit.as_hit()))
            if _citable_chart(block):
                blocks[hit.member_id] = block
                head[-1] = hit
                break
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
        ranked = HybridSearch(
            document,
            channel_limit=request.channel_limit,
            rrf_k=self._settings.rrf_k,
            reranker=reranker,
            index_cache=self._index_cache,
        ).search(request.question, top_k=2 * request.top_k)
        fused, selected = select_context(document, ranked, request.top_k)
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
            verification.verified,
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
        )
