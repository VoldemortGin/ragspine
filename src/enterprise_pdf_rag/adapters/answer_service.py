"""Orchestrate one answer: hybrid search → evidence blocks → one model call → verification.

The service never calls a model except through the injected bounded client: once per
request to synthesise the answer, plus at most one earlier call to restate a question the
lexical channel cannot score in the index's language (ADR 0018) and at most one to route it
over the document's outline tree (ADR 0019; both bounded and cached like any other, and
skipped entirely when unavailable). Retrieval, hydration and claim verification are
deterministic reads of the pinned snapshot.
"""

from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from enterprise_pdf_rag.adapters.answer_audit import AnswerAuditContext, AnswerAuditStore
from enterprise_pdf_rag.adapters.cross_document import CrossDocument
from enterprise_pdf_rag.adapters.hybrid_search import HybridSearch, LexicalIndex, lexical_rank
from enterprise_pdf_rag.adapters.query_translation import is_foreign_script, translate_query
from enterprise_pdf_rag.adapters.tree_retrieval import route_tree
from enterprise_pdf_rag.answers.derivations import verify_derivations
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
    TreeRoute,
    VerifiedClaim,
)
from enterprise_pdf_rag.answers.page_window import with_page_context
from enterprise_pdf_rag.answers.ports import MemberText, MountedDocument
from enterprise_pdf_rag.answers.prompt import (
    ModelAnswer,
    ModelAnswerWithDerivations,
    answer_system,
    build_prompt,
    member_aliases,
    resolve_member_aliases,
)
from enterprise_pdf_rag.answers.query_filters import derive_filters
from enterprise_pdf_rag.answers.query_mode import (
    FusionMode,
    QueryMode,
    content_probe,
    is_label_query,
)
from enterprise_pdf_rag.answers.verify import decide, verify_claims
from enterprise_pdf_rag.processing.context_builder import (
    BlockKind,
    ContextBlock,
    PageContextBlock,
    PromptBlock,
    budget_blocks,
    build_context_block,
)
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
)
from ragspine.extraction.evidence.figures.chart_qa.displayed_models import (
    DISPLAYED_BAR_SCOPE,
    DisplayedLookupContext,
)
from ragspine.extraction.evidence.figures.chart_qa.models import ChartContext
from ragspine.extraction.evidence.figures.models import ValueKind
from ragspine.extraction.evidence.metadata.document_tree import DocumentTree
from ragspine.extraction.evidence.page.models import ObjectKind
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge

_TASK = "rag-answer-v1"
_MODEL_OUTPUT_FAILURES = frozenset({"invalid_model_json", "truncated_response"})
# Whether a question with a mounted tree is routed when the request says nothing (ADR 0019).
# On since ADR 0019 Amendment 1: at ``tree_rrf_k`` 600 a member only the tree reached sorts below
# every scored one, and on the 20-page sample both arms scored 22/22 citing the same pages, so the
# channel cannot cost an answer and is left on for the documents it was built for — one live call
# per question. A caller pins it either way with ``AnswerRequest.tree_route``.
ROUTE_BY_DEFAULT: Final = True


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
    """Any of the three channels placed this hit inside ``limit`` on its own ranking."""
    return any(
        rank is not None and rank <= limit
        for rank in (hit.vector_rank, hit.lexical_rank, hit.tree_rank)
    )


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
    # 追加在 ``SYSTEM_RULES`` 之后的调用方规则;``None`` 时 system 文本逐字节不变。
    extra_system_rules: str | None = None
    # ADR 0038:放开模型计算/换算,每个算出的数由代码复算;``answer_constants`` 是派生可引用的
    # 常量白名单(须同时放开)。默认关闭时 system、schema 与请求指纹逐字节不变。
    allow_derivations: bool = False
    answer_constants: Mapping[str, float] | None = None


class AnswerService:
    def __init__(
        self,
        documents: Mapping[str, MountedDocument],
        llm: JsonCompletionClient,
        *,
        settings: AnswerSettings | None = None,
        reranker: ListwiseJudge | None = None,
        index_cache: MutableMapping[str, LexicalIndex] | None = None,
        trees: Mapping[str, DocumentTree] | None = None,
        audit: AnswerAuditStore | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._documents = documents
        self._llm = llm
        self._settings = AnswerSettings() if settings is None else settings
        # 构造期解析一次:附加规则超长在此抛 ValueError,而不是每题失败。
        self._system = answer_system(
            self._settings.extra_system_rules,
            derivations=self._settings.allow_derivations,
            constants=self._settings.answer_constants,
        )
        self._response_model: type[ModelAnswer] = (
            ModelAnswerWithDerivations if self._settings.allow_derivations else ModelAnswer
        )
        self._reranker = reranker
        self._index_cache: MutableMapping[str, LexicalIndex] = (
            {} if index_cache is None else index_cache
        )
        # Keyed by ``source_sha256``; ``None`` means the ADR 0019 channel does not exist here.
        self._trees: Mapping[str, DocumentTree] = {} if trees is None else trees
        # Optional local journal (``adapters/answer_audit``): ``None`` writes nothing.
        self._audit = audit
        # A readable name per document id, printed beside each block of a cross-document
        # prompt (ADR 0032); the short sha256 stands in for a document without one.
        self._labels: Mapping[str, str] = {} if labels is None else labels
        self._cross: CrossDocument | None = None

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

    def _route(
        self, request: AnswerRequest, plan: "_QueryPlan", document: MountedDocument
    ) -> TreeRoute | None:
        """The pages this document's outline tree routes the question to; ``None`` when unrouted.

        The channel exists only where a tree was built for the document, so a service mounted
        without one behaves exactly as it did before ADR 0019. Given a tree, a question is
        routed unless the request says otherwise: ``ROUTE_BY_DEFAULT`` is **True** since ADR
        0019 Amendment 1, because at ``tree_rrf_k`` 600 a routed page cannot displace a scored
        member and the 20-page sample scored 22/22 in both arms citing exactly the same pages
        — the cost is one live call, not an answer. A caller pins it either way with
        ``AnswerRequest.tree_route``. A short label query is still never routed: BM25 already
        matches a printed label wherever it appears and a map of the document would buy
        nothing for that call.

        The question routed is the English restatement when there is one: the outline is
        written in the index's language, which is the only wording a router can match it on.
        """
        tree = self._trees.get(document.source_sha256)
        if tree is None:
            return None
        question = request.question if plan.translation is None else plan.translation.english
        if request.tree_route is None:
            routed = ROUTE_BY_DEFAULT and not is_label_query(question)
        else:
            routed = request.tree_route
        return route_tree(question, tree, self._llm) if routed else None

    def _corpus(self) -> CrossDocument:
        """Every mounted document as one corpus, built once per service (ADR 0032)."""
        if self._cross is None:
            self._cross = CrossDocument(tuple(self._documents.values()))
        return self._cross

    def _label(self, document: MountedDocument) -> str:
        """How a cross-document prompt names a document: its name and short sha256."""
        sha = document.source_sha256
        name = self._labels.get(sha)
        return sha[:12] if not name else f"{name} ({sha[:12]})"

    def _leading(
        self,
        search: HybridSearch,
        plan: "_QueryPlan",
        allowed: frozenset[str] | None,
        corpus: CrossDocument,
    ) -> MountedDocument | None:
        """The document whose tree a cross-document question is routed over, if any.

        One routing call per question, as for one document: the outline routed is the one of
        the document owning the corpus's best BM25 member. A question the lexical channel
        cannot score is not routed at all (ADR 0032).
        """
        if plan.mode == "vector_only":
            return None
        best = lexical_rank(
            search.index, plan.lexical_query or plan.query, limit=1, allowed=allowed
        )
        return corpus.owner(best[0].member_id) if best else None

    def answer(self, request: AnswerRequest) -> AnswerResult:
        if request.cross_document and len(self._documents) > 1:
            corpus = self._corpus()
            return self._answer(request, corpus, corpus)
        return self._answer(request, self._select(request.document_sha256), None)

    def _answer(
        self, request: AnswerRequest, document: MountedDocument, corpus: CrossDocument | None
    ) -> AnswerResult:
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
        tree_members: frozenset[str] | None = None
        if corpus is None:
            route = self._route(request, plan, document)
        else:
            leading = self._leading(search, plan, allowed, corpus)
            route = None if leading is None else self._route(request, plan, leading)
            if route is not None and leading is not None:
                tree_members = corpus.members_of(leading)
        # The whole fused ranking, not just its head: a hit's own channel ranks decide the
        # guaranteed visual seats below, and fusion can bury such a hit anywhere (ADR 0012).
        # Both channels together rank at most ``2 * channel_limit`` members.
        outcome = search.search(
            plan.query,
            top_k=2 * request.channel_limit,
            allowed=allowed,
            mode=plan.mode,
            lexical_query=plan.lexical_query,
            tree_pages=() if route is None else route.pages,
            tree_members=tree_members,
        )
        # Across documents every hit is pinned back to its own document's snapshot here, so
        # everything downstream reads, cites and journals the real document (ADR 0032).
        ranked = outcome.hits if corpus is None else corpus.repin(outcome.hits)
        fused, selected = select_context(document, ranked, request.top_k, members)
        selected = _with_member_regions(selected, members)
        owners: Mapping[str, str] | None = None
        if corpus is not None:
            labels = {item.source_sha256: self._label(item) for item in corpus.documents}
            owners = {
                member.member_id: labels[corpus.owner(member.member_id).source_sha256]
                for member in members
            }
            selected = tuple(replace(block, document=owners[block.member_id]) for block in selected)
        hits = {hit.member_id: hit.as_hit() for hit in fused}
        enabled = self._settings.page_window if request.page_window is None else request.page_window
        windowed: tuple[PromptBlock, ...] = (
            with_page_context(
                selected,
                members,
                max_chars=self._settings.page_window_budget_chars,
                documents=owners,
            )
            if enabled
            else tuple(selected)
        )
        blocks = budget_blocks(windowed, max_chars=self._settings.prompt_budget_chars)
        member_blocks = tuple(block for block in blocks if isinstance(block, ContextBlock))
        page_blocks = tuple(block for block in blocks if isinstance(block, PageContextBlock))
        # The document a result names: the one document, or across documents the document of
        # the first prompt member (else of the best fused hit). Claims and hits name their own.
        primary = document
        searched: tuple[str, ...] = ()
        if corpus is not None:
            searched = corpus.document_ids
            head = (
                member_blocks[0].member_id
                if member_blocks
                else (fused[0].member_id if fused else None)
            )
            primary = corpus.documents[0] if head is None else corpus.owner(head)
        if not member_blocks:
            return self._abstained(
                primary,
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
                tree_route=route,
                searched_documents=searched,
            )
        member_ids = tuple(block.member_id for block in member_blocks)
        # Minted after the budget pass, from the blocks that really reach the prompt.
        aliases = member_aliases(blocks)
        prompt = build_prompt(request.question, blocks, request.history, aliases)

        def document_of(member_id: str) -> str:
            return (
                document.source_sha256 if corpus is None else corpus.owner(member_id).source_sha256
            )

        page_windows = tuple(
            PageWindowStat(
                block.page_index,
                len(block.members),
                len(block.prompt_text()),
                block.truncated,
                None
                if corpus is None or not block.members
                else document_of(block.members[0].member_id),
            )
            for block in page_blocks
        )
        # The journal's first write: the prompt exactly as it is about to be sent.
        journal = self._begin_audit(
            AnswerAuditContext(
                request.question,
                primary.source_sha256,
                primary.processing_id,
                primary.retrieval_snapshot_id,
                self._system,
                prompt,
                member_ids,
                fused,
                page_windows,
                outcome.mode,
                applied,
                relaxed,
                None if translation is None else translation.english,
                ranked,
                {member.member_id: member.page_index for member in members},
                searched,
            )
        )
        try:
            completion = self._llm.complete_text_json(
                task=_TASK,
                prompt=prompt,
                response_model=self._response_model,
                system=self._system,
                max_output_tokens=self._settings.max_output_tokens,
            )
        except JsonCompletionError as error:
            if error.code in _MODEL_OUTPUT_FAILURES:
                invalid = self._abstained(
                    primary,
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
                    tree_route=route,
                    searched_documents=searched,
                )
                self._close_audit(journal, invalid, error=error.code)
                return invalid
            self._close_audit(journal, None, error=error.code)
            raise DependencyUnavailable(error.code) from error
        by_member = {block.member_id: block for block in member_blocks}

        def chart_evidence(member_id: str) -> ChartContext | DisplayedLookupContext:
            block: ContextBlock = by_member[member_id]
            if block.scope == DISPLAYED_BAR_SCOPE:
                return document.displayed_context(hits[member_id])
            return document.chart_context(hits[member_id])

        # Aliases exist only inside the prompt; everything downstream — the verifier, the
        # citations, the HTTP contract — keeps naming members by their real id.
        model = resolve_member_aliases(completion.parsed, aliases)
        verification = verify_claims(model, by_member, chart_evidence=chart_evidence)
        derivations = (
            verify_derivations(model, verification.verified, self._settings.answer_constants or {})
            if self._settings.allow_derivations
            else None
        )
        status, reason, detail = decide(
            model,
            verification,
            blocks_present=True,
            question=request.question,
            # The members' own text, never the rendering: a block's ``page_index=`` is
            # metadata about the page, not a figure printed on it.
            context_texts=tuple(member.text for block in page_blocks for member in block.members),
            derivations=derivations,
        )
        result = AnswerResult(
            status,
            model.answer if status is AnswerStatus.ANSWERED else None,
            _with_documents(_with_page_titles(verification.verified, members), document_of),
            verification.rejected,
            reason,
            detail,
            primary.source_sha256,
            primary.processing_id,
            primary.retrieval_snapshot_id,
            member_ids,
            fused,
            completion.request_fingerprint,
            self._llm.live_call_count - before,
            completion.cache_hit,
            applied,
            relaxed,
            page_windows,
            outcome.mode,
            translation,
            route,
            searched,
            derivations=() if derivations is None else derivations.verified,
            rejected_derivations=() if derivations is None else derivations.rejected,
        )
        self._close_audit(journal, result, model_output_raw=completion.json_text)
        return result

    def _begin_audit(self, context: AnswerAuditContext) -> int | None:
        return None if self._audit is None else self._audit.begin(context)

    def _close_audit(
        self,
        journal: int | None,
        result: AnswerResult | None,
        *,
        model_output_raw: str | None = None,
        error: str | None = None,
    ) -> None:
        if self._audit is not None and journal is not None:
            self._audit.finish(journal, result, model_output_raw=model_output_raw, error=error)

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
        tree_route: TreeRoute | None = None,
        searched_documents: tuple[str, ...] = (),
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
            tree_route=tree_route,
            searched_documents=searched_documents,
        )


def _with_member_regions(
    blocks: Sequence[ContextBlock], members: Sequence[MemberText]
) -> tuple[ContextBlock, ...]:
    """Print the part of the page a block belongs to, where the layout bound it to one.

    A page of side-by-side charts hands every one of them the same page-level regions, and
    three blocks headed `VONB ($m)` are then indistinguishable — the model has to guess a
    column, and it cites its guess with real provenance. Where the page geometry named a
    column (``MemberText.member_regions``), the block says so. Nothing else changes: a
    member the layout could not name prints exactly what it always printed.
    """
    bound = {member.member_id: member.member_regions for member in members if member.member_regions}
    return tuple(
        block if block.member_id not in bound else replace(block, regions=bound[block.member_id])
        for block in blocks
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


def _with_documents(
    claims: tuple[VerifiedClaim, ...], document_of: Callable[[str], str]
) -> tuple[VerifiedClaim, ...]:
    """Name on each citation the document its member was read from (ADR 0032)."""
    return tuple(
        replace(
            claim,
            citations=tuple(
                replace(citation, document_sha256=document_of(citation.member_id))
                for citation in claim.citations
            ),
        )
        for claim in claims
    )
