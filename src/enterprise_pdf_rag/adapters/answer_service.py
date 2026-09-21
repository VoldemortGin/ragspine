"""Orchestrate one answer: hybrid search → evidence blocks → one model call → verification.

The service never calls a model except through the injected bounded client: once per
request to synthesise the answer, plus at most one earlier call to restate a question the
lexical channel cannot score in the index's language (ADR 0018; bounded and cached like
any other, and skipped entirely when unavailable). Retrieval, hydration and claim
verification are deterministic reads of the pinned snapshot.
"""

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from enterprise_pdf_rag.adapters.hybrid_search import HybridSearch, LexicalIndex
from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient, JsonCompletionError
from enterprise_pdf_rag.adapters.query_translation import is_foreign_script, translate_query
from enterprise_pdf_rag.answers.member_filter import candidate_members, region_vocabulary
from enterprise_pdf_rag.answers.models import (
    AbstainReason,
    AnswerRequest,
    AnswerResult,
    AnswerStatus,
    FusedHit,
    MemberFilters,
    PageWindowStat,
    TranslatedQuery,
    VerifiedClaim,
)
from enterprise_pdf_rag.answers.page_window import with_page_context
from enterprise_pdf_rag.answers.ports import MemberText, MountedDocument
from enterprise_pdf_rag.answers.prompt import SYSTEM_RULES, ModelAnswer, build_prompt
from enterprise_pdf_rag.answers.query_filters import derive_filters
from enterprise_pdf_rag.answers.query_mode import FusionMode, QueryMode, content_probe
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
    PageContextBlock,
    PromptBlock,
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


def _within_a_channel(hit: FusedHit, limit: int) -> bool:
    """Either channel placed this hit inside ``limit`` on its own ranking."""
    return any(rank is not None and rank <= limit for rank in (hit.vector_rank, hit.lexical_rank))


def select_context(
    document: MountedDocument,
    ranked: Sequence[FusedHit],
    top_k: int,
    member_texts: Sequence[MemberText] | None = None,
) -> tuple[tuple[FusedHit, ...], tuple[ContextBlock, ...]]:
    """Hydrate the top-k fused hits, with one guaranteed seat per citable visual kind.

    For each visual kind (chart / diagram / formula) with no citable block in the top-k, the
    first citable member of that kind in the promotion window takes a seat, given up from the
    last seat backward and never one that already holds a citable visual object (ADR 0012,
    generalised by ADR 0015's follow-up). The window is the next k fused positions **plus
    every hit either channel ranked inside ``2 * top_k`` on its own ranking**: reciprocal rank
    fusion sorts a hit only one channel scored below every hit both channels contributed to,
    which is precisely the object the guaranteed seat exists for — one channel sees it, the
    other cannot score it at all. Candidates are examined in fused order, so the fused window
    is always offered first. A pending or label-only object never qualifies, and nothing
    outside both windows is promoted. Only members of a still missing kind are resolved for
    the check; ``member_texts`` supplies their kinds when the caller already holds them.
    """
    head = list(ranked[:top_k])
    blocks = {hit.member_id: build_context_block(document.resolve(hit.as_hit())) for hit in head}
    seated = {_citable_visual(blocks[hit.member_id]) for hit in head}
    missing = [kind for kind in _VISUAL_KINDS if kind not in seated]
    window = [
        hit
        for position, hit in enumerate(ranked[top_k:], start=top_k)
        if position < 2 * top_k or _within_a_channel(hit, 2 * top_k)
    ]
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
class _QueryPlan:
    """What the retrieval channels will score, and the translation that produced it.

    ``query`` is always the question as asked — what the vector channel and the rerank
    judge read. ``lexical_query`` is the restatement only the token-matching channel and
    the channel classifier score; ``None`` means both channels score the question.
    """

    query: str
    mode: FusionMode
    translation: TranslatedQuery | None
    lexical_query: str | None = None


@dataclass(frozen=True, slots=True)
class AnswerSettings:
    rrf_k: float = 60.0
    prompt_budget_chars: int = 18_000
    max_output_tokens: int = 1024
    # Print the rest of each hit's page beside it (ADR 0017), and how much of one page.
    page_window: bool = True
    page_window_budget_chars: int = 6_000


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

    def _plan(
        self, request: AnswerRequest, search: HybridSearch, allowed: frozenset[str] | None
    ) -> "_QueryPlan":
        """Decide what the lexical channel will score: the question, or an English restatement.

        A question is restated only when its *content words* score nothing lexically — the
        signature of a question written outside the index's language, and the one case where
        fusion has nothing to fuse. The probe drops figures deliberately: a Chinese question
        naming ``1H26`` matches that token and would otherwise look scoreable. Whenever such
        a question goes untranslated — switched off, or no usable output — the vector channel
        answers alone rather than fusing a ranking built from a stray year (ADR 0018).
        """
        probe = content_probe(request.question)
        if (
            not probe
            or not is_foreign_script(request.question)
            or search.lexical_hits(probe, allowed=allowed) > 0
        ):
            return _QueryPlan(request.question, request.fusion_mode, None)
        # Foreign to the index's vocabulary from here on. The plan depends on the question
        # alone, never on how much budget is left, so the same question always replays from
        # the same cache entries; a translated question simply costs two live calls.
        translation = (
            translate_query(request.question, self._llm) if request.translate_query else None
        )
        if translation is not None:
            # Lexically only: the vector channel reads the question as language, and this
            # corpus ranks the asker's own wording above a restatement of it (ADR 0018).
            return _QueryPlan(
                request.question, request.fusion_mode, translation, translation.english
            )
        unfused: FusionMode = (
            "vector_only" if request.fusion_mode == "auto" else request.fusion_mode
        )
        return _QueryPlan(request.question, unfused, None)

    def answer(self, request: AnswerRequest) -> AnswerResult:
        document = self._select(request.document_sha256)
        # Spans the whole request: a translated question costs one call before synthesis.
        before = self._llm.live_call_count
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
        vocabulary = region_vocabulary(members)
        filters = (
            derive_filters(request.question, vocabulary)
            if request.filters is None
            else request.filters
        )
        applied, allowed, relaxed = _narrow(members, filters, request.top_k)
        plan = self._plan(request, search, allowed)
        translation = plan.translation
        # A translated question is one the vocabulary cannot see either, so the pre-filters
        # are re-derived from the English and unioned with the question's own; an explicitly
        # supplied filter is never widened. The prompt and the prose gate below still keep
        # the question the user actually asked.
        if translation is not None and request.filters is None:
            filters = _union(filters, derive_filters(translation.english, vocabulary))
            applied, allowed, relaxed = _narrow(members, filters, request.top_k)
        # The whole fused ranking, not just its head: a hit's own channel ranks decide the
        # guaranteed visual seats below, and fusion can bury such a hit anywhere (ADR 0012).
        # Both channels together rank at most ``2 * channel_limit`` members.
        outcome = search.search(
            plan.query,
            top_k=2 * request.channel_limit,
            allowed=allowed,
            mode=plan.mode,
            lexical_query=plan.lexical_query,
        )
        fused, selected = select_context(document, outcome.hits, request.top_k, members)
        hits = {hit.member_id: hit.as_hit() for hit in fused}
        enabled = self._settings.page_window if request.page_window is None else request.page_window
        windowed: tuple[PromptBlock, ...] = (
            with_page_context(selected, members, max_chars=self._settings.page_window_budget_chars)
            if enabled
            else tuple(selected)
        )
        blocks = budget_blocks(windowed, max_chars=self._settings.prompt_budget_chars)
        member_blocks = tuple(block for block in blocks if isinstance(block, ContextBlock))
        page_blocks = tuple(block for block in blocks if isinstance(block, PageContextBlock))
        if not member_blocks:
            return self._abstained(
                document,
                fused,
                (),
                AbstainReason.NO_RELEVANT_MEMBER,
                "no retrieved member fits the context budget"
                if fused
                else "no member matched in the channels that ran",
                llm_live_calls=self._llm.live_call_count - before,
                filters_applied=applied,
                filters_relaxed=relaxed,
                fusion_mode=outcome.mode,
                query_translation=translation,
            )
        member_ids = tuple(block.member_id for block in member_blocks)
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
                    fusion_mode=outcome.mode,
                    query_translation=translation,
                )
            raise DependencyUnavailable(error.code) from error
        by_member = {block.member_id: block for block in member_blocks}

        def chart_evidence(member_id: str) -> ChartContext | DisplayedLookupContext:
            block: ContextBlock = by_member[member_id]
            if block.scope == DISPLAYED_BAR_SCOPE:
                return document.displayed_context(hits[member_id])
            return document.chart_context(hits[member_id])

        model = completion.parsed
        verification = verify_claims(model, by_member, chart_evidence=chart_evidence)
        status, reason, detail = decide(
            model,
            verification,
            blocks_present=True,
            question=request.question,
            # The members' own text, never the rendering: a block's ``page_index=`` is
            # metadata about the page, not a figure printed on it.
            context_texts=tuple(member.text for block in page_blocks for member in block.members),
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
            tuple(
                PageWindowStat(
                    block.page_index, len(block.members), len(block.prompt_text()), block.truncated
                )
                for block in page_blocks
            ),
            outcome.mode,
            translation,
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
        fusion_mode: QueryMode = "rrf",
        query_translation: TranslatedQuery | None = None,
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
            fusion_mode=fusion_mode,
            query_translation=query_translation,
        )


def _narrow(
    members: Sequence[MemberText], filters: MemberFilters, top_k: int
) -> tuple[MemberFilters | None, frozenset[str] | None, bool]:
    """What the pre-filters leave for the ranking: applied filter, candidates, relaxed.

    Fewer candidates than seats: the narrowing is dropped; when a filter was in play the
    result says so (the built-in cover / agenda exclusion is not reported).
    """
    applied: MemberFilters | None = None if filters.is_empty else filters
    allowed = candidate_members(members, applied)
    if allowed is not None and len(allowed) < top_k:
        return applied, None, applied is not None
    return applied, allowed, False


def _union(first: MemberFilters, second: MemberFilters) -> MemberFilters:
    """``first``'s values, then whatever ``second`` adds; order kept, duplicates dropped."""

    def merge(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
        return left + tuple(value for value in right if value not in left)

    return MemberFilters(merge(first.periods, second.periods), merge(first.regions, second.regions))


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
