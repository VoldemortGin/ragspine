"""One-call folder pipeline: every PDF under a folder ingested, published, then evaluated.

The existing stages run in their documented order, unchanged — ``ingest_pdf`` (semantics),
``requalify_visual_objects`` (the generic ingest pins ``qualification_policy="none"``, so a
chart only earns the ``qualified_ir`` retrieval eligibility asks for here), ``qualify_draft``,
``index_draft``, ``publish_draft`` and ``annotate_document_tree`` on the published id. An
optional question set is then asked of exactly this run's documents in process, through the
same ``document-catalog`` app a service would build, without opening a port.

Every model call is budgeted: ``max_live_calls_per_pdf`` per document, an optional total shared
by ingest, tree and answers, and cache replay once a budget is spent, so a rerun of a finished
folder makes no call at all. Configuration is read through the provider loaders and
``get_settings()`` only; the pipeline never starts the local-model tunnel itself.
"""

import asyncio
import json
import re
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any, Final, Literal

import httpx
from fastapi import FastAPI
from pydantic import Field

from enterprise_pdf_rag.adapters.answer_audit import open_audit_store
from enterprise_pdf_rag.adapters.answer_llm import make_answer_llm
from enterprise_pdf_rag.adapters.document_catalog import DocumentCatalog, scan_catalog
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.document_tree_extraction import (
    DocumentTreeSummary,
    annotate_document_tree,
)
from enterprise_pdf_rag.adapters.draft_publication import (
    DraftIndex,
    DraftPublication,
    DraftQualification,
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.http.documents import create_documents_app
from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.adapters.hybrid_search import LocalRerankJudge
from enterprise_pdf_rag.adapters.nl_gold import NlGoldCase, NlGoldSet, answer_prose, load_gold
from enterprise_pdf_rag.adapters.nl_gold_runner import ENVELOPE_KEY, ChatPost, run_case
from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.visual_requalification import (
    RequalificationSummary,
    requalify_visual_objects,
)
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.common.evidence.providers.local_models import (
    LocalEmbeddingAdapter,
    LocalRerankAdapter,
)
from ragspine.common.evidence.providers.providers import load_llm_config, load_local_model_config
from ragspine.eval.retrieval_only import (
    BatchQuestion,
    content_hit,
    gold_rank,
    load_questions,
    recall_ks,
    retrieval_metrics,
)
from ragspine.extraction.evidence.figures.ports import EmbeddingPort
from ragspine.extraction.evidence.page.models import StageState
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge

type DocumentStatus = Literal[
    "published", "duplicate_of", "nothing_to_index", "failed", "budget_starved"
]
type PipelineStage = Literal["ingest", "requalify", "qualify", "index", "publish", "tree"]
type CaseVerdict = Literal[
    "pass",
    "FAIL",
    "known-gap-holds",
    "known-gap-moved",
    "answered",
    "abstained",
    "routing_failed",
    "http_error",
]
type Progress = Callable[[str, dict[str, object]], None]

GOLD_FORMAT: Final = "nl-answers-gold-v1"
_CHAT_PATH = "/v1/chat/completions"
_MAX_LIVE_CALLS = 200
# The ranks ``ragspine.eval.retrieval_only`` reports at; the prompt seats rarely pass ten.
_METRIC_TOP_K = 10
_SHA_PREFIX = re.compile(r"[0-9a-f]{12,64}")
_GOLD_VERDICTS: dict[str, CaseVerdict] = {
    "pass": "pass",
    "FAIL": "FAIL",
    "known-gap-holds": "known-gap-holds",
    "known-gap-moved": "known-gap-moved",
}
_UNHEALTHY_VERDICTS = frozenset({"FAIL", "http_error", "routing_failed"})
_HEALTHY_STATUSES = frozenset({"published", "duplicate_of", "nothing_to_index"})
_LLM_HINT = (
    "set OPENAI_API_KEY, OPENAI_BASE_URL and OPENAI_MODEL (or APP_LLM_API_KEY, "
    "APP_LLM_BASE_URL and APP_LLM_MODEL) in the project .env (see .env.example)"
)
_LOCAL_HINT = (
    "set {prefix}_BASE_URL, {prefix}_MODEL and {prefix}_API_KEY in the project .env and start "
    "the local-model tunnel first: .venv/bin/python "
    "scripts/enterprise_pdf_rag/local_model_tunnel.py start (this pipeline never starts it)"
)
_EMBEDDING_HINT = (
    "simplest: set OPENAI_EMBEDDING_MODEL in the project .env to use the same OPENAI_BASE_URL "
    "gateway as the LLM; for a separate loopback service instead, "
    + _LOCAL_HINT.format(prefix="APP_EMBEDDING")
)


class PreflightError(ValueError):
    """A dependency is missing or unreachable; raised before any ingest or model call."""


class RequalificationCounts(BoundaryModel):
    """``RequalificationSummary`` as a boundary model: verdicts counted as ``kind:outcome``."""

    processing_id: str
    draft_processing_id: str | None
    outcomes: dict[str, int]

    @classmethod
    def from_summary(cls, summary: RequalificationSummary) -> "RequalificationCounts":
        counts = Counter(f"{item.kind.value}:{item.outcome}" for item in summary.objects)
        return cls(
            processing_id=summary.processing_id,
            draft_processing_id=summary.draft_processing_id,
            outcomes=dict(sorted(counts.items())),
        )


class DocumentRun(BoundaryModel):
    pdf_path: str
    sha256: str
    status: DocumentStatus
    duplicate_of: str | None = None
    failed_stage: PipelineStage | None = None
    error: str | None = None
    ingestion: IngestionSummary | None = None
    requalification: RequalificationCounts | None = None
    qualification: DraftQualification | None = None
    index: DraftIndex | None = None
    publication: DraftPublication | None = None
    tree: DocumentTreeSummary | None = None
    # The published index already described this exact draft with this embedder.
    index_reused: bool = False
    # The ingest budget this document was given, after the shared total was applied.
    live_call_budget: int = 0
    live_calls: int = 0
    elapsed_s: float = 0.0


class EvalCase(BoundaryModel):
    case_id: str
    question: str
    document_id: str | None
    verdict: CaseVerdict
    failures: tuple[str, ...]
    status: str | None = None
    abstain_reason: str | None = None
    # The question set's expected answer; None when the question set has none (gold sets, unrouted).
    expected: str | None = None
    # The assistant's answer prose as the user saw it (citation block removed); None when no answer came back.
    answer: str | None = None
    claim_count: int = 0
    # 1-based physical pages the verified claims cite, in first-citation order.
    cited_pages: tuple[int, ...] = ()
    # Distinct-page rank of the expected pages among the prompt members; None is a miss.
    page_rank: int | None = None
    llm_live_calls: int = 0
    elapsed_ms: float = 0.0
    envelope: dict[str, Any] = Field(default_factory=dict)


class EvalSummary(BoundaryModel):
    format: Literal["nl-answers-gold-v1", "questions"]
    totals: dict[str, int]
    # ``ragspine.eval.retrieval_only.retrieval_metrics`` over the cases that name pages.
    metrics: dict[str, Any]
    cases: tuple[EvalCase, ...]


class LiveCalls(BoundaryModel):
    ingest: int = 0
    tree: int = 0
    answer: int = 0
    total: int = 0


class FolderPipelineResult(BoundaryModel):
    folder: str
    ingestion_root: str
    documents: tuple[DocumentRun, ...]
    eval: EvalSummary | None
    live_calls: LiveCalls
    budget_exhausted: bool
    report_dir: str | None = None

    @property
    def ok(self) -> bool:
        """Every document usable and no evaluated case failed, errored or went unrouted."""
        documents = all(
            item.status in _HEALTHY_STATUSES and item.error is None for item in self.documents
        )
        cases = () if self.eval is None else self.eval.cases
        return documents and not any(case.verdict in _UNHEALTHY_VERDICTS for case in cases)


class _Budget:
    """The optional shared total; every allotment smaller than asked marks exhaustion."""

    def __init__(self, total: int | None) -> None:
        self.total = total
        self.used = 0
        self.exhausted = False

    def allot(self, wanted: int) -> int:
        if self.total is None:
            return wanted
        granted = max(0, min(wanted, self.total - self.used))
        if granted < wanted:
            self.exhausted = True
        return granted

    def spend(self, calls: int) -> None:
        self.used += calls


def discover_pdfs(folder: Path) -> tuple[Path, ...]:
    """Every non-hidden ``*.pdf`` (any case) below ``folder``, in relative-path order."""
    found = (
        path
        for path in folder.rglob("*")
        if path.is_file()
        and path.suffix.casefold() == ".pdf"
        and not any(part.startswith(".") for part in path.relative_to(folder).parts)
    )
    return tuple(sorted(found, key=lambda path: path.relative_to(folder).as_posix()))


def _check_budget(name: str, value: int | None) -> None:
    if value is not None and not 0 <= value <= _MAX_LIVE_CALLS:
        raise ValueError(f"{name} must be within 0..{_MAX_LIVE_CALLS}")


def _check_total(value: int | None) -> None:
    if value is not None and value < 0:
        raise ValueError("max_live_calls_total must not be negative")


def _check_max_questions(value: int | None) -> None:
    if value is not None and value < 1:
        raise ValueError("max_questions must be at least 1 (or None for every question)")


def _limit_questions(
    question_set: NlGoldSet | tuple[BatchQuestion, ...], limit: int | None
) -> NlGoldSet | tuple[BatchQuestion, ...]:
    """Keep the first ``limit`` questions in set order (a gold set counts runnable cases only)."""
    if limit is None:
        return question_set
    if not isinstance(question_set, NlGoldSet):
        return question_set[:limit]
    kept = 0
    cases: list[NlGoldCase] = []
    for case in question_set.cases:
        if case.offline_only:
            cases.append(case)
        elif kept < limit:
            kept += 1
            cases.append(case)
    return question_set.model_copy(update={"cases": tuple(cases)})


def _load_questions(path: Path) -> NlGoldSet | tuple[BatchQuestion, ...]:
    """A frozen ``nl-answers-gold-v1`` set, or the light question formats of ``retrieval_only``."""
    if not path.is_file():
        raise FileNotFoundError(f"question set not found: {path}")
    if path.suffix.casefold() == ".json":
        payload = path.read_bytes()
        try:
            schema = json.loads(payload).get("schema_version")
        except (AttributeError, json.JSONDecodeError):
            schema = None
        if schema == GOLD_FORMAT:
            return load_gold(payload)
    return load_questions(path)


def _preflight(
    *,
    embedder: EmbeddingPort | None,
    reranker: ListwiseJudge | None,
    needs_rerank: bool,
) -> tuple[EmbeddingPort, ListwiseJudge | None]:
    try:
        load_llm_config()
    except ValueError as error:
        raise PreflightError(f"LLM is not configured ({error}); {_LLM_HINT}") from error
    if embedder is None:
        hint = _EMBEDDING_HINT
        try:
            embedder = LocalEmbeddingAdapter(load_local_model_config("embedding"))
        except ValueError as error:
            raise PreflightError(f"Embedding is not configured ({error}); {hint}") from error
        try:
            embedder.embed_query("preflight")
        except (ValueError, OSError) as error:
            raise PreflightError(
                f"Embedding service did not answer a probe ({error}); {hint}"
            ) from error
    if needs_rerank and reranker is None:
        hint = _LOCAL_HINT.format(prefix="APP_RERANK")
        try:
            reranker = LocalRerankJudge(LocalRerankAdapter(load_local_model_config("rerank")))
        except ValueError as error:
            raise PreflightError(
                f"The question set asks for rerank but it is not configured ({error}); {hint}"
            ) from error
    return embedder, reranker


def _starved(outputs: ProcessingStore, processing_id: str) -> bool:
    """Did ingest leave a layout, object or page-metadata stage waiting for a budget?"""
    manifest = outputs.load(processing_id)
    return any(
        outcome is not None and outcome.state is StageState.DEFERRED
        for page in manifest.pages
        for outcome in (
            page.partition,
            page.metadata,
            *(stage for item in page.objects for stage in item.stages),
        )
    )


def _reusable_index(outputs: ProcessingStore, draft_id: str, embedder: EmbeddingPort) -> bool:
    """Is the published release exactly this draft indexed by this embedder?

    ``index_draft`` saves ``replace(draft, retrieval=publication)``, so a release whose
    manifest minus its retrieval equals the draft, and whose every member carries the
    embedder's fingerprint, is what indexing the draft again would rebuild.
    """
    if not (outputs.root / "current-processing").is_file():
        return False
    _, current = outputs.load_current()
    if current.retrieval is None or replace(current, retrieval=None) != outputs.load(draft_id):
        return False
    plan, _ = outputs.load_retrieval(current.retrieval)
    return {member.embedding_fingerprint for member in plan.members} == {embedder.fingerprint}


def _emit(progress: Progress | None, event: str, **payload: object) -> None:
    if progress is not None:
        progress(event, payload)


def _run_document(
    pdf: Path,
    digest: str,
    *,
    root: Path,
    pages: str,
    per_pdf: int,
    budget: _Budget,
    requalify: bool,
    build_tree: bool,
    tree_max_live_calls: int,
    embedder: EmbeddingPort,
    continue_on_error: bool,
    progress: Progress | None,
) -> tuple[DocumentRun, int]:
    """One PDF through every stage; returns the run and its tree's live calls."""
    started = perf_counter()
    allotted = budget.allot(per_pdf)
    cut = allotted < per_pdf
    run: dict[str, Any] = {
        "pdf_path": str(pdf),
        "sha256": digest,
        "live_call_budget": allotted,
    }
    tree_calls = 0
    stage: PipelineStage = "ingest"

    def finish(status: DocumentStatus) -> tuple[DocumentRun, int]:
        done = DocumentRun(status=status, elapsed_s=round(perf_counter() - started, 3), **run)
        # A recorded failure travels with the event, so a progress line shows its reason.
        reason = {key: run[key] for key in ("failed_stage", "error") if key in run}
        _emit(progress, "document_done", pdf=str(pdf), status=status, **reason)
        return done, tree_calls

    _emit(progress, "document_start", pdf=str(pdf), sha256=digest, budget=allotted)
    try:
        ingestion = ingest_pdf(
            pdf=pdf, pages=pages, output_dir=root, stage="semantics", max_live_calls=allotted
        )
        budget.spend(ingestion.live_call_count)
        run.update(ingestion=ingestion, live_calls=ingestion.live_call_count)
        source_store = Path(ingestion.source_store)
        processing_store = Path(ingestion.processing_store)
        sources = LocalDocumentStore(source_store, activate_on_publish=False)
        outputs = ProcessingStore(processing_store)
        if cut and _starved(outputs, ingestion.processing_id):
            return finish("budget_starved")
        draft_id = ingestion.processing_id
        if requalify:
            stage = "requalify"
            summary = requalify_visual_objects(
                sources, outputs, processing_id=ingestion.processing_id
            )
            run["requalification"] = RequalificationCounts.from_summary(summary)
            draft_id = summary.draft_processing_id or ingestion.processing_id
        stage = "qualify"
        qualification = qualify_draft(
            source_store=source_store, processing_store=processing_store, processing_id=draft_id
        )
        run["qualification"] = qualification
        if qualification.eligible_member_count == 0:
            return finish("nothing_to_index")
        stage = "index"
        indexed_id = draft_id
        if _reusable_index(outputs, draft_id, embedder):
            indexed_id = outputs.load_current()[0]
            run["index_reused"] = True
        else:
            indexed = index_draft(
                source_store=source_store,
                processing_store=processing_store,
                processing_id=draft_id,
                embedder=embedder,
            )
            run["index"] = indexed
            indexed_id = indexed.indexed_processing_id
        stage = "publish"
        publication = publish_draft(
            source_store=source_store,
            processing_store=processing_store,
            processing_id=indexed_id,
            activate_source=True,
        )
        run["publication"] = publication
        if build_tree:
            stage = "tree"
            client = JsonCompletionClient(
                load_llm_config(),
                cache_dir=processing_store / "model-cache",
                max_live_calls=budget.allot(tree_max_live_calls),
                timeout=180.0,
            )
            tree = annotate_document_tree(
                sources,
                outputs,
                processing_id=publication.published_processing_id,
                client=client,
            )
            tree_calls = client.live_call_count
            budget.spend(tree_calls)
            run["tree"] = tree
    except (ValueError, OSError) as error:
        if not continue_on_error:
            raise
        run.update(failed_stage=stage, error=str(error) or type(error).__name__)
        # A published document whose tree failed is still published and answerable.
        return finish("published" if "publication" in run else "failed")
    return finish("published")


def _member_pages(catalog: DocumentCatalog) -> dict[str, tuple[str, int]]:
    """member id → (document id, 1-based page) for every mounted release."""
    pages: dict[str, tuple[str, int]] = {}
    for entry in catalog.ready:
        assert entry.current_processing_id is not None
        outputs = ProcessingStore(Path(entry.processing_store))
        manifest = outputs.load(entry.current_processing_id)
        if manifest.retrieval is None:
            continue
        plan, _ = outputs.load_retrieval(manifest.retrieval)
        for member in plan.members:
            pages[member.member_id] = (entry.document_id, member.page_index + 1)
    return pages


def _asgi_post(app: FastAPI) -> ChatPost:
    """POST to the app in process, on a private event loop in a worker thread.

    The caller's thread may already run a loop (a notebook cell), where ``asyncio.run``
    refuses to start; a worker thread owns a fresh loop either way, and no port is opened.
    """

    async def send(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://folder-pipeline"
        ) as client:
            response = await client.post(_CHAT_PATH, json=body, timeout=None)
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = {"detail": response.text[:500]}
        return response.status_code, payload if isinstance(payload, dict) else {"body": payload}

    def post(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with ThreadPoolExecutor(max_workers=1) as worker:
            return worker.submit(asyncio.run, send(body)).result()

    return post


def _observed(response: dict[str, Any]) -> dict[str, Any]:
    envelope = response.get(ENVELOPE_KEY)
    return envelope if isinstance(envelope, dict) else {}


def _cited_pages(envelope: dict[str, Any]) -> tuple[int, ...]:
    cited: list[int] = []
    for claim in envelope.get("claims", ()):
        for citation in claim.get("citations", ()):
            page = citation.get("page_index")
            if isinstance(page, int) and page + 1 not in cited:
                cited.append(page + 1)
    return tuple(cited)


def _ranks(
    envelope: dict[str, Any],
    groups: Sequence[frozenset[int]],
    member_pages: dict[str, tuple[str, int]],
) -> tuple[int | None, int | None]:
    hits = [
        member_pages[member_id]
        for member_id in envelope.get("member_ids", ())
        if member_id in member_pages
    ]
    return gold_rank(hits, groups), gold_rank(hits, groups, distinct=True)


def _gold_groups(case: NlGoldCase) -> tuple[frozenset[int], ...]:
    return tuple(
        frozenset(claim.page_index + 1 for claim in requirement.alternatives)
        for requirement in case.expected.required_claims
    )


def _metrics(ranks: Sequence[tuple[int | None, int | None]]) -> dict[str, Any]:
    return retrieval_metrics(ranks, recall_ks(_METRIC_TOP_K)) if ranks else {}


def _answer_text(response: dict[str, Any] | None) -> str | None:
    """The answer prose of a chat-completion response, or None when it carries no message."""
    try:
        content = (response or {})["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return answer_prose(content) if isinstance(content, str) else None


def _eval_gold(
    gold: NlGoldSet,
    *,
    post: ChatPost | None,
    mounted: frozenset[str],
    member_pages: dict[str, tuple[str, int]],
    progress: Progress | None,
) -> EvalSummary:
    runnable = tuple(case for case in gold.cases if not case.offline_only)
    cases: list[EvalCase] = []
    ranks: list[tuple[int | None, int | None]] = []
    for case in runnable:
        groups = _gold_groups(case)
        if post is None or case.document_sha256 not in mounted:
            cases.append(
                EvalCase(
                    case_id=case.case_id,
                    question=case.question.text,
                    document_id=case.document_sha256,
                    verdict="routing_failed",
                    failures=(
                        f"document_sha256 {case.document_sha256[:12]} is not among this run's "
                        "published documents; not asked",
                    ),
                )
            )
            if groups:
                ranks.append((None, None))
            continue
        outcome = run_case(case, post)
        envelope = _observed(outcome.response or {})
        if groups:
            rank, page_rank = _ranks(envelope, groups, member_pages)
            ranks.append((rank, page_rank))
        else:
            page_rank = None
        verdict = "http_error" if outcome.status_code != 200 else _GOLD_VERDICTS[outcome.verdict]
        cases.append(
            EvalCase(
                case_id=case.case_id,
                question=case.question.text,
                document_id=case.document_sha256,
                verdict=verdict,
                failures=outcome.failures,
                status=envelope.get("status"),
                abstain_reason=envelope.get("abstain_reason"),
                answer=_answer_text(outcome.response) if outcome.status_code == 200 else None,
                claim_count=len(envelope.get("claims", ())),
                cited_pages=_cited_pages(envelope),
                page_rank=page_rank,
                llm_live_calls=int(envelope.get("llm_live_calls") or 0),
                elapsed_ms=round(outcome.elapsed_ms, 1),
                envelope=envelope,
            )
        )
        _emit(progress, "eval_case", case_id=case.case_id, verdict=verdict)
    verdicts = Counter(case.verdict for case in cases)
    totals = {
        "run": len(cases),
        "passed": verdicts["pass"],
        "failed": verdicts["FAIL"],
        "known_gap": verdicts["known-gap-holds"] + verdicts["known-gap-moved"],
        "routing_failed": verdicts["routing_failed"],
        "http_error": verdicts["http_error"],
        "offline_only_skipped": len(gold.cases) - len(runnable),
    }
    return EvalSummary(
        format=GOLD_FORMAT, totals=totals, metrics=_metrics(ranks), cases=tuple(cases)
    )


def _resolve_reference(reference: str, documents: Sequence[DocumentRun]) -> set[str]:
    folded = reference.strip().casefold()
    if _SHA_PREFIX.fullmatch(folded):
        return {item.sha256 for item in documents if item.sha256.startswith(folded)}
    return {
        item.sha256
        for item in documents
        if folded in {Path(item.pdf_path).name.casefold(), Path(item.pdf_path).stem.casefold()}
    }


def _eval_questions(
    questions: Sequence[BatchQuestion],
    *,
    post: ChatPost | None,
    documents: Sequence[DocumentRun],
    mounted: frozenset[str],
    member_pages: dict[str, tuple[str, int]],
    progress: Progress | None,
) -> EvalSummary:
    cases: list[EvalCase] = []
    ranks: list[tuple[int | None, int | None]] = []
    only = next(iter(mounted)) if len(mounted) == 1 else None
    for question in questions:
        groups = question.page_groups
        document: str | None = only
        unrouted: str | None = None if mounted else "no document of this run is published"
        if question.doc and unrouted is None:
            resolved = set().union(*(_resolve_reference(doc, documents) for doc in question.doc))
            if len(resolved & mounted) == 1:
                document = next(iter(resolved & mounted))
            else:
                unrouted = (
                    f"doc {list(question.doc)} names "
                    f"{'no' if not resolved & mounted else 'more than one'} published document "
                    "of this run"
                )
        if post is None or unrouted is not None:
            cases.append(
                EvalCase(
                    case_id=question.id,
                    question=question.question,
                    document_id=None,
                    verdict="routing_failed",
                    failures=(unrouted or "no published document",),
                    expected=question.expected,
                )
            )
            if groups:
                ranks.append((None, None))
            continue
        body: dict[str, Any] = {
            "model": "enterprise-pdf-rag",
            "messages": [{"role": "user", "content": question.question}],
        }
        if document is not None:
            body["document"] = document
        started = perf_counter()
        status_code, response = post(body)
        elapsed_ms = round((perf_counter() - started) * 1000, 1)
        envelope = _observed(response)
        verdict: CaseVerdict
        failures: list[str] = []
        page_rank: int | None = None
        answer: str | None = None
        if status_code != 200 or not envelope:
            verdict = "routing_failed" if status_code == 422 else "http_error"
            failures.append(f"HTTP {status_code}: {json.dumps(response, ensure_ascii=False)[:200]}")
            if groups:
                ranks.append((None, None))
        else:
            answered = envelope.get("status") == "answered"
            verdict = "answered" if answered else "abstained"
            if groups:
                rank, page_rank = _ranks(envelope, groups, member_pages)
                ranks.append((rank, page_rank))
            expects = bool(groups) or bool(question.expected)
            if not answered and expects:
                failures.append("abstained, but the question expects an answer")
            prose = answer_prose(response["choices"][0]["message"]["content"])
            answer = prose
            if answered and question.expected and not content_hit(prose, question.expected):
                failures.append(f"answer does not contain the expected {question.expected!r}")
        cases.append(
            EvalCase(
                case_id=question.id,
                question=question.question,
                document_id=envelope.get("document_sha256", document),
                verdict=verdict,
                failures=tuple(failures),
                status=envelope.get("status"),
                abstain_reason=envelope.get("abstain_reason"),
                expected=question.expected,
                answer=answer,
                claim_count=len(envelope.get("claims", ())),
                cited_pages=_cited_pages(envelope),
                page_rank=page_rank,
                llm_live_calls=int(envelope.get("llm_live_calls") or 0),
                elapsed_ms=elapsed_ms,
                envelope=envelope,
            )
        )
        _emit(progress, "eval_case", case_id=question.id, verdict=verdict)
    verdicts = Counter(case.verdict for case in cases)
    totals = {
        "run": len(cases),
        **{
            name: verdicts[name]
            for name in ("answered", "abstained", "routing_failed", "http_error")
        },
        "with_failures": sum(bool(case.failures) for case in cases),
    }
    return EvalSummary(
        format="questions", totals=totals, metrics=_metrics(ranks), cases=tuple(cases)
    )


def _markdown(result: FolderPipelineResult) -> str:
    lines = [
        "# Folder pipeline report",
        "",
        f"- folder: `{result.folder}`",
        f"- ingestion root: `{result.ingestion_root}`",
        f"- ok: **{result.ok}**; budget exhausted: {result.budget_exhausted}",
        f"- live calls: ingest {result.live_calls.ingest}, tree {result.live_calls.tree}, "
        f"answer {result.live_calls.answer}, total {result.live_calls.total}",
        "",
        "| pdf | sha256 | status | stage | eligible | index reused | live calls | s | error |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in result.documents:
        eligible = "-" if item.qualification is None else item.qualification.eligible_member_count
        error = (item.error or item.duplicate_of or "-").replace("|", "\\|")[:160]
        lines.append(
            f"| `{Path(item.pdf_path).name}` | `{item.sha256[:12]}` | {item.status} | "
            f"{item.failed_stage or '-'} | {eligible} | {item.index_reused} | {item.live_calls} | "
            f"{item.elapsed_s:.1f} | {error} |"
        )
    if result.eval is not None:
        lines += [
            "",
            f"## Evaluation ({result.eval.format})",
            "",
            f"- totals: `{json.dumps(result.eval.totals)}`",
            f"- metrics: `{json.dumps(result.eval.metrics)}`",
            "",
            "| case | verdict | status | claims | cited pages | page rank | ms | failures |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for case in result.eval.cases:
            failures = "; ".join(case.failures).replace("|", "\\|")[:180] or "-"
            lines.append(
                f"| `{case.case_id}` | {case.verdict} | {case.status or '-'} | "
                f"{case.claim_count} | {list(case.cited_pages)} | {case.page_rank} | "
                f"{case.elapsed_ms:.0f} | {failures} |"
            )
    return "\n".join(lines) + "\n"


def run_folder_pipeline(
    folder: Path | None = None,
    *,
    questions: Path | None = None,
    ingestion_root: Path | None = None,
    pages: str = "all",
    max_live_calls_per_pdf: int,
    max_live_calls_total: int | None = None,
    requalify: bool = True,
    build_tree: bool = True,
    tree_max_live_calls: int = 50,
    answer_max_live_calls: int | None = None,
    max_questions: int | None = None,
    continue_on_error: bool = True,
    report_dir: Path | None = None,
    embedder: EmbeddingPort | None = None,
    reranker: ListwiseJudge | None = None,
    answer_llm: JsonCompletionClient | None = None,
    progress: Progress | None = None,
) -> FolderPipelineResult:
    """Ingest, requalify, qualify, index, publish and tree every PDF in ``folder``, then evaluate.

    ``folder``, ``questions`` and ``report_dir`` default to ``NB_PDF_DIR`` / ``NB_QUESTIONS_PATH`` /
    ``NB_REPORT_DIR`` (``get_settings()``); an argument always wins, and with neither the
    question set is not run and no report is written. Without a folder from either place this
    raises ``ValueError``. ``max_questions`` answers only the first N questions of the set in its
    own order (``None`` = all); it never limits ingestion, which always covers every PDF.

    Raises ``ValueError`` for an invalid budget, ``FileNotFoundError`` for a missing folder or
    question set and ``PreflightError`` for a missing or unreachable dependency, all before
    any ingest or model call. An injected ``embedder`` / ``reranker`` / ``answer_llm`` skips
    its own check. A failing document is recorded and the rest continue unless
    ``continue_on_error`` is false, when its ``ValueError`` / ``OSError`` propagates.
    """
    _check_budget("max_live_calls_per_pdf", max_live_calls_per_pdf)
    _check_budget("tree_max_live_calls", tree_max_live_calls)
    _check_budget("answer_max_live_calls", answer_max_live_calls)
    _check_total(max_live_calls_total)
    _check_max_questions(max_questions)
    settings = get_settings()
    if folder is None:
        folder = settings.pdf_source_dir
    if folder is None:
        raise ValueError("no PDF folder: pass folder or set NB_PDF_DIR in the project .env")
    if questions is None:
        questions = settings.questions_path
    if report_dir is None:
        report_dir = settings.report_dir
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"folder not found: {folder}")
    question_set = None if questions is None else _load_questions(questions)
    if question_set is not None:
        question_set = _limit_questions(question_set, max_questions)
    needs_rerank = isinstance(question_set, NlGoldSet) and any(
        case.request.rerank for case in question_set.cases if not case.offline_only
    )
    embedder, reranker = _preflight(embedder=embedder, reranker=reranker, needs_rerank=needs_rerank)
    root = ingestion_root if ingestion_root is not None else settings.ingestion_root
    root = root.expanduser().resolve()
    budget = _Budget(max_live_calls_total)

    pdfs = discover_pdfs(folder)
    _emit(progress, "discovered", folder=str(folder), count=len(pdfs))

    documents: list[DocumentRun] = []
    first: dict[str, str] = {}
    tree_total = 0
    for pdf in pdfs:
        digest = sha256(pdf.read_bytes()).hexdigest()
        if digest in first:
            documents.append(
                DocumentRun(
                    pdf_path=str(pdf),
                    sha256=digest,
                    status="duplicate_of",
                    duplicate_of=first[digest],
                )
            )
            continue
        first[digest] = str(pdf)
        run, tree_calls = _run_document(
            pdf,
            digest,
            root=root,
            pages=pages,
            per_pdf=max_live_calls_per_pdf,
            budget=budget,
            requalify=requalify,
            build_tree=build_tree,
            tree_max_live_calls=tree_max_live_calls,
            embedder=embedder,
            continue_on_error=continue_on_error,
            progress=progress,
        )
        documents.append(run)
        tree_total += tree_calls
    ingest_total = sum(item.live_calls for item in documents)

    summary: EvalSummary | None = None
    answer_total = 0
    if question_set is not None:
        shas = {item.sha256 for item in documents if item.status == "published"}
        scanned = scan_catalog(root)
        catalog = scanned.model_copy(
            update={
                "documents": tuple(
                    entry for entry in scanned.documents if entry.document_id in shas
                )
            }
        )
        mounted = frozenset(entry.document_id for entry in catalog.ready)
        llm = answer_llm
        if llm is None:
            llm = make_answer_llm(
                cache_dir=root / "model-cache",
                max_live_calls=budget.allot(
                    settings.answer_max_live_calls
                    if answer_max_live_calls is None
                    else answer_max_live_calls
                ),
            )
        before = llm.live_call_count
        post: ChatPost | None = None
        member_pages: dict[str, tuple[str, int]] = {}
        if mounted:
            audit_path = (
                settings.answer_audit_path
                if settings.answer_audit_path is not None
                else root / "answers-audit.sqlite"
            )
            app = create_documents_app(
                catalog,
                embedder=embedder,
                llm=llm,
                reranker=reranker,
                verify_every_request=settings.verify_every_request,
                audit=open_audit_store(audit_path) if settings.answer_audit_enabled else None,
            )
            post = _asgi_post(app)
            member_pages = _member_pages(catalog)
        if isinstance(question_set, NlGoldSet):
            summary = _eval_gold(
                question_set,
                post=post,
                mounted=mounted,
                member_pages=member_pages,
                progress=progress,
            )
        else:
            summary = _eval_questions(
                question_set,
                post=post,
                documents=documents,
                mounted=mounted,
                member_pages=member_pages,
                progress=progress,
            )
        answer_total = llm.live_call_count - before
        budget.spend(answer_total)

    calls = LiveCalls(
        ingest=ingest_total,
        tree=tree_total,
        answer=answer_total,
        total=ingest_total + tree_total + answer_total,
    )
    result = FolderPipelineResult(
        folder=str(folder),
        ingestion_root=str(root),
        documents=tuple(documents),
        eval=summary,
        live_calls=calls,
        budget_exhausted=budget.exhausted
        or any(item.status == "budget_starved" for item in documents),
        report_dir=None if report_dir is None else str(report_dir.expanduser().resolve()),
    )
    if report_dir is not None:
        target = report_dir.expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        (target / "report.json").write_text(
            result.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (target / "report.md").write_text(_markdown(result), encoding="utf-8")
    _emit(progress, "done", ok=result.ok, live_calls=calls.total)
    return result
