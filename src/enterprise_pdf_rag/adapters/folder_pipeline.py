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
from _thread import LockType
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from queue import SimpleQueue
from threading import Event, Lock
from time import perf_counter
from typing import Any, Final, Literal

import httpx
from fastapi import FastAPI
from pydantic import Field

from enterprise_pdf_rag.adapters import pdfspine_tsr
from enterprise_pdf_rag.adapters.answer_audit import open_audit_store
from enterprise_pdf_rag.adapters.answer_llm import make_answer_llm
from enterprise_pdf_rag.adapters.answer_service import AnswerSettings
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
from enterprise_pdf_rag.adapters.ingest_mode import (
    IngestMode,
    IngestPlan,
    LayoutPolicy,
    UnverifiedTableStructure,
    ingest_plan,
    published_ingest_mode,
)
from enterprise_pdf_rag.adapters.nl_gold import NlGoldCase, NlGoldSet, answer_prose, load_gold
from enterprise_pdf_rag.adapters.nl_gold_runner import ENVELOPE_KEY, ChatPost, run_case
from enterprise_pdf_rag.adapters.pdf_ingestion import (
    MAX_INGEST_LIVE_CALLS,
    IngestionSummary,
    IngestProgress,
    ingest_pdf,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.question_docs import (
    QuestionDocsCheck,
    QuestionDocsError,
    QuestionSelection,
    QuestionSelectionMode,
    Resolver,
    SelectedQuestion,
    SkippedQuestion,
    check_references,
    make_resolver,
    skip_reason,
    unresolved_message,
)
from enterprise_pdf_rag.adapters.visual_requalification import (
    RequalificationSummary,
    requalify_visual_objects,
)
from enterprise_pdf_rag.answers.prompt import answer_system
from enterprise_pdf_rag.processing.index_text import ONE_UNIT_EACH, IndexTextOptions
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.file_placement import recording_repairs
from ragspine.common.evidence.object_backend.probe import probe_directory
from ragspine.common.evidence.object_backend.protocol import StoreBusy
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    one_sampling_probe,
    unsupported_sampling_parameters,
)
from ragspine.common.evidence.providers.local_models import (
    LocalEmbeddingAdapter,
    LocalRerankAdapter,
)
from ragspine.common.evidence.providers.providers import (
    ProviderConfigurationError,
    load_llm_config,
    load_local_model_config,
)
from ragspine.eval.retrieval_only import (
    BatchQuestion,
    content_hit,
    gold_rank,
    load_questions,
    recall_ks,
    retrieval_metrics,
)
from ragspine.extraction.evidence.figures.ports import BatchEmbeddingPort, EmbeddingPort
from ragspine.extraction.evidence.page.models import StageState
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge

type DocumentStatus = Literal[
    "published",
    "duplicate_of",
    "nothing_to_index",
    "failed",
    "budget_starved",
    "skipped_not_referenced",
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
# Where a question was searched (ADR 0032): every published document of the run, the one
# document its ``doc`` names (``restrict_to_question_doc``), or nowhere (not asked).
type CaseRouting = Literal["cross_document", "doc", "failed"]
type RetrievalScope = Literal["all_documents", "question_doc"]

GOLD_FORMAT: Final = "nl-answers-gold-v1"
_CHAT_PATH = "/v1/chat/completions"
# Tree and answer budgets keep their old ceiling; the per-PDF ingest one is
# ``MAX_INGEST_LIVE_CALLS`` (ADR 0022).
_MAX_LIVE_CALLS = 200
# ``max_live_calls_per_pdf="auto"``: pages * per page + base, capped at the ceiling. A page costs
# one layout and one page-metadata call and every chart / image / diagram / formula object two
# more, so 4 per page leaves room for one chart-like object per page on average (ADR 0022).
AUTO_CALLS_PER_PAGE: Final = 4
AUTO_CALLS_BASE: Final = 50
# ``document_progress``: at most one line per this many pages, or per this many seconds.
_PROGRESS_PAGES = 10
_PROGRESS_SECONDS = 30.0
# ``max_parallel_documents``: documents ingested at once, at most (ADR 0033), and how often
# the waiting caller looks up, so an interrupt is seen within this many seconds.
MAX_PARALLEL_DOCUMENTS: Final = 16
_POLL_SECONDS = 0.2
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
_HEALTHY_STATUSES = frozenset(
    {"published", "duplicate_of", "nothing_to_index", "skipped_not_referenced"}
)
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
    # None only for a PDF skipped as not referenced without its bytes ever being read.
    sha256: str | None
    status: DocumentStatus
    duplicate_of: str | None = None
    failed_stage: PipelineStage | None = None
    error: str | None = None
    ingestion: IngestionSummary | None = None
    # Which store backend held this document's bytes (ADR 0036); None before ingest ran.
    object_backend: str | None = None
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
    # ADR 0025: the mode this run ingested in, and the mode of the snapshot the document's
    # ``current-processing`` names once the run is over (a failed run leaves the previous
    # release, possibly of the other mode, published; None when nothing is published).
    ingest_mode: IngestMode = "full"
    published_ingest_mode: IngestMode | None = None
    # ADR 0029: stored entries this run found damaged (missing / empty / truncated / not their
    # digest, e.g. lost by an asynchronous flush) and repaired or recomputed, by kind
    # (object, stage_cache, model_cache, source); counts only.
    storage_repairs: dict[str, int] = Field(default_factory=dict)
    # ADR 0035, over this document's ingest, index embeddings and tree: requests resent after a
    # transient failure (429, 5xx, timeout, connection), and calls that still failed
    # transiently once their retries were spent — left unrecorded, so a rerun calls them again.
    retries: int = 0
    transient_failures: int = 0


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
    # ADR 0032: where the question was searched, and how many published documents that was.
    routing: CaseRouting = "doc"
    searched_documents: int = 0
    # The documents the verified citations were read from, in first-citation order.
    cited_documents: tuple[str, ...] = ()
    # Evaluation labels only — they never steer retrieval: the published document the
    # question's ``doc`` resolves to (None when it names none, several or nothing), and
    # whether a verified citation landed in it (None when there is no such document or no
    # citation). ``page_rank`` is judged inside that document whenever several were searched.
    expected_doc: str | None = None
    cited_doc_hit: bool | None = None


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
    # Sampling parameters the LLM endpoint refused, so this run's requests went without them
    # (ADR 0021); empty when it accepted everything or the settings already omit them.
    sampling_parameters_dropped: tuple[str, ...] = ()
    # Which PDF each reference of the asked questions names, and which questions were asked
    # (``question_selection``); None without a question set (ADR 0022).
    question_docs: QuestionDocsCheck | None = None
    # The ingest mode of this run (ADR 0025) and the page layout it used (ADR 0028).
    ingest_mode: IngestMode = "full"
    layout_policy: LayoutPolicy = "model"
    # The store backend this run's ingestion root resolved to (ADR 0036).
    object_backend: str = "files"
    # What a light question set's answers searched (ADR 0032): every published document of
    # the run (the default), or only the document each question's ``doc`` names.
    retrieval_scope: RetrievalScope = "all_documents"

    @property
    def ok(self) -> bool:
        """Every document usable and no evaluated case failed, errored or went unrouted."""
        documents = all(
            item.status in _HEALTHY_STATUSES and item.error is None for item in self.documents
        )
        cases = () if self.eval is None else self.eval.cases
        return documents and not any(case.verdict in _UNHEALTHY_VERDICTS for case in cases)


class _Budget:
    """The optional shared total; every allotment smaller than asked marks exhaustion.

    An allotment stays reserved until ``spend`` settles it with the calls actually made, under
    one lock, so documents running at once are never granted more than the total between them;
    one document at a time, every allotment sees exactly what it always saw (ADR 0033).
    """

    def __init__(self, total: int | None) -> None:
        self.total = total
        self.used = 0
        self.reserved = 0
        self.exhausted = False
        self._lock = Lock()

    def allot(self, wanted: int) -> int:
        if self.total is None:
            return wanted
        with self._lock:
            granted = max(0, min(wanted, self.total - self.used - self.reserved))
            self.reserved += granted
            if granted < wanted:
                self.exhausted = True
            return granted

    def spend(self, calls: int, granted: int = 0) -> None:
        """Count ``calls`` and release the ``granted`` allotment they were made under."""
        with self._lock:
            self.used += calls
            if self.total is not None:
                self.reserved -= granted


class _Cancelled(BaseException):
    """A document stopped at a page or stage boundary because the run is stopping (ADR 0033).

    A ``BaseException``, like the ``KeyboardInterrupt`` it usually follows, so no stage's
    ``except Exception`` records it as that stage's failure."""


class _DocumentEmbedder:
    """One document's view of an embedder shared by documents running at once (ADR 0033).

    Calls go one at a time through the shared ``lock`` and ``request_count`` counts only this
    document's requests, so ``DraftIndex.embedding_requests`` reads what the serial run read.
    """

    def __init__(self, inner: EmbeddingPort, lock: LockType) -> None:
        self._inner = inner
        self._lock = lock
        self._requests = 0
        self._retries = 0
        self._transient = 0

    @property
    def fingerprint(self) -> str:
        return self._inner.fingerprint

    @property
    def request_count(self) -> int:
        return self._requests

    @property
    def retry_count(self) -> int:
        return self._retries

    @property
    def transient_failure_count(self) -> int:
        return self._transient

    def _counted[T](self, call: Callable[[], T]) -> T:
        with self._lock:
            before = _embedding_counts(self._inner)
            try:
                return call()
            finally:
                requests, retries, failures = (
                    after - start
                    for after, start in zip(_embedding_counts(self._inner), before, strict=True)
                )
                self._requests += requests
                self._retries += retries
                self._transient += failures

    def embed_description(self, text: str) -> tuple[float, ...]:
        return self._counted(lambda: self._inner.embed_description(text))

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._counted(lambda: self._inner.embed_query(text))


class _DocumentBatchEmbedder(_DocumentEmbedder):
    """``_DocumentEmbedder`` of a ``BatchEmbeddingPort``, batching as the shared one does."""

    def __init__(self, inner: BatchEmbeddingPort, lock: LockType) -> None:
        super().__init__(inner, lock)
        self._batch = inner

    def embed_descriptions(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return self._counted(lambda: self._batch.embed_descriptions(texts))


def _embedding_counts(embedder: EmbeddingPort) -> tuple[int, int, int]:
    """``(request_count, retry_count, transient_failure_count)``; 0 where it keeps none."""
    return (
        getattr(embedder, "request_count", 0),
        getattr(embedder, "retry_count", 0),
        getattr(embedder, "transient_failure_count", 0),
    )


def _document_embedder(embedder: EmbeddingPort, lock: LockType) -> EmbeddingPort:
    if isinstance(embedder, BatchEmbeddingPort):
        return _DocumentBatchEmbedder(embedder, lock)
    return _DocumentEmbedder(embedder, lock)


type _DocumentWork = Callable[[Path, str, Progress | None, Event], tuple[DocumentRun, int]]


def _run_in_parallel(
    jobs: Sequence[tuple[Path, str]],
    work: _DocumentWork,
    *,
    workers: int,
    progress: Progress | None,
) -> list[tuple[DocumentRun, int]]:
    """Every job on a pool of ``workers`` threads; results in job order (ADR 0033).

    The progress callback is entered by one thread at a time and every event of a document
    carries its ``slot`` (1..workers). Whatever stops the run — an interrupt, or a failure that
    ``continue_on_error=False`` lets through — sets ``cancel``: queued documents never start,
    running ones stop at their next page or stage boundary, after their calls in flight have
    been recorded, and only then is it raised. Waiting polls, so an interrupt is seen at once.
    """
    lock = Lock()
    slots: SimpleQueue[int] = SimpleQueue()
    for slot in range(1, workers + 1):
        slots.put(slot)
    cancel = Event()

    def say(event: str, payload: dict[str, object]) -> None:
        if progress is not None:
            with lock:
                progress(event, payload)

    def one(pdf: Path, digest: str) -> tuple[DocumentRun, int]:
        slot = slots.get()
        try:
            if cancel.is_set():
                raise _Cancelled
            tagged: Progress | None = (
                None
                if progress is None
                else lambda event, payload: say(event, payload | {"slot": slot})
            )
            return work(pdf, digest, tagged, cancel)
        finally:
            slots.put(slot)

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="run-folder")
    futures = [pool.submit(one, pdf, digest) for pdf, digest in jobs]
    try:
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=_POLL_SECONDS, return_when=FIRST_COMPLETED)
            for future in done:
                error = future.exception()
                if error is not None:
                    raise error
    except BaseException as error:
        cancel.set()
        running = sum(future.running() for future in futures)
        say("stopping", {"reason": type(error).__name__, "running": running})
        # ``cancel_futures``: a plain shutdown would still run every queued document first.
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return [future.result() for future in futures]


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


def _check_budget(name: str, value: int | None, ceiling: int = _MAX_LIVE_CALLS) -> None:
    if value is not None and not 0 <= value <= ceiling:
        raise ValueError(f"{name} must be within 0..{ceiling}")


def auto_live_call_budget(pages: int) -> int:
    """The ``"auto"`` per-PDF ingest ceiling for this many selected pages."""
    return min(MAX_INGEST_LIVE_CALLS, pages * AUTO_CALLS_PER_PAGE + AUTO_CALLS_BASE)


def _check_per_pdf(value: int | str) -> None:
    if isinstance(value, str):
        if value != "auto":
            raise ValueError('max_live_calls_per_pdf must be an integer or "auto"')
        return
    _check_budget("max_live_calls_per_pdf", value, MAX_INGEST_LIVE_CALLS)


def _check_total(value: int | None) -> None:
    if value is not None and value < 0:
        raise ValueError("max_live_calls_total must not be negative")


def _check_parallel(value: int) -> None:
    if not 1 <= value <= MAX_PARALLEL_DOCUMENTS:
        raise ValueError(f"max_parallel_documents must be within 1..{MAX_PARALLEL_DOCUMENTS}")


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


def _question_docs(question: NlGoldCase | BatchQuestion) -> tuple[str, ...]:
    return (question.document_sha256,) if isinstance(question, NlGoldCase) else question.doc


def _question_id(question: NlGoldCase | BatchQuestion) -> str:
    return question.case_id if isinstance(question, NlGoldCase) else question.id


def _select_matched(
    question_set: NlGoldSet | tuple[BatchQuestion, ...],
    limit: int,
    resolve: Resolver,
) -> tuple[NlGoldSet | tuple[BatchQuestion, ...], QuestionSelection]:
    """The first ``limit`` questions, in set order, whose references name exactly one PDF.

    A gold set's offline-only cases are kept and never counted, as in ``_limit_questions``.
    """
    asked = question_set.cases if isinstance(question_set, NlGoldSet) else question_set
    kept: list[Any] = []
    selected: list[SelectedQuestion] = []
    skipped: list[SkippedQuestion] = []
    for question in asked:
        if isinstance(question, NlGoldCase) and question.offline_only:
            kept.append(question)
            continue
        if len(selected) == limit:
            continue
        docs = _question_docs(question)
        reason = skip_reason(docs, resolve)
        if reason is not None:
            skipped.append(
                SkippedQuestion(question_id=_question_id(question), docs=docs, reason=reason)
            )
            continue
        kept.append(question)
        selected.append(
            SelectedQuestion(
                question_id=_question_id(question),
                docs=docs,
                pdf=str(resolve(docs[0]).pdf),
                rules=tuple(dict.fromkeys(rule for doc in docs if (rule := resolve(doc).rule))),
            )
        )
    selection = QuestionSelection(
        mode="first_matched",
        max_questions=limit,
        selected=tuple(selected),
        skipped=tuple(skipped),
        short=len(selected) < limit,
    )
    if not selected:
        raise QuestionDocsError(
            f"题目选取 first_matched: 题集里没有一道题引用的 PDF 在文件夹里, 没有可跑的题"
            f"(扫描了 {len(skipped)} 道)。前几道的原因: "
            + "; ".join(f"{item.question_id}: {item.reason}" for item in skipped[:3])
            + "。可在 DOC_ALIASES 里写明对应关系, 或检查 NB_PDF_DIR / 题集。"
        )
    limited: NlGoldSet | tuple[BatchQuestion, ...] = (
        question_set.model_copy(update={"cases": tuple(kept)})
        if isinstance(question_set, NlGoldSet)
        else tuple(kept)
    )
    return limited, selection


def _plan_questions(
    question_set: NlGoldSet | tuple[BatchQuestion, ...],
    pdfs: Sequence[Path],
    folder: Path,
    *,
    max_questions: int | None,
    selection_mode: QuestionSelectionMode,
    doc_aliases: Mapping[str, str] | None,
    digest: Callable[[Path], str],
) -> tuple[NlGoldSet | tuple[BatchQuestion, ...], QuestionDocsCheck]:
    """Limit the question set and resolve every reference of what is left — the one
    resolution both the ``only_question_docs`` selection and the answer routing read.

    PDF bytes are read (through ``digest``) only for a sha reference.
    """
    resolve = make_resolver(pdfs, folder, digest, doc_aliases)
    if selection_mode == "first_matched" and max_questions is not None:
        limited, selection = _select_matched(question_set, max_questions, resolve)
    else:
        limited = _limit_questions(question_set, max_questions)
        selection = QuestionSelection(mode=selection_mode, max_questions=max_questions)
    references: dict[str, list[str]] = {}
    without: list[str] = []
    asked = limited.cases if isinstance(limited, NlGoldSet) else limited
    for question in asked:
        if isinstance(question, NlGoldCase) and question.offline_only:
            continue
        docs = _question_docs(question)
        if not docs:
            without.append(_question_id(question))
        for doc in docs:
            references.setdefault(doc, []).append(_question_id(question))
    return limited, check_references(
        references,
        questions_without_doc=without,
        pdf_count=len(pdfs),
        folder=folder,
        resolve=resolve,
        selection=selection,
    )


def _hash_file(pdf: Path) -> str:
    return sha256(pdf.read_bytes()).hexdigest()


def check_question_docs(
    folder: Path | None = None,
    questions: Path | None = None,
    *,
    max_questions: int | None = None,
    question_selection: QuestionSelectionMode = "first",
    doc_aliases: Mapping[str, str] | None = None,
) -> QuestionDocsCheck | None:
    """The question-set → PDF check ``run_folder_pipeline`` does first, on its own.

    Read-only: lists the folder's PDF names and reads the question set; hashes a PDF only
    for a sha reference. ``folder`` / ``questions`` default to ``NB_PDF_DIR`` /
    ``NB_QUESTIONS_PATH``; None without a question set. Never ingests, writes or calls a
    model. Raises ``QuestionDocsError`` for an invalid alias or a ``first_matched``
    selection with nothing to run.
    """
    _check_max_questions(max_questions)
    settings = get_settings()
    folder = folder if folder is not None else settings.pdf_source_dir
    questions = questions if questions is not None else settings.questions_path
    if folder is None:
        raise ValueError("no PDF folder: pass folder or set NB_PDF_DIR in the project .env")
    if questions is None:
        return None
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"folder not found: {folder}")
    _, check = _plan_questions(
        _load_questions(questions),
        discover_pdfs(folder),
        folder,
        max_questions=max_questions,
        selection_mode=question_selection,
        doc_aliases=doc_aliases,
        digest=_hash_file,
    )
    return check


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


def _reusable_index(
    outputs: ProcessingStore,
    draft_id: str,
    embedder: EmbeddingPort,
    index_options: IndexTextOptions = ONE_UNIT_EACH,
) -> bool:
    """Is the published release exactly this draft indexed by this embedder and layout?

    ``index_draft`` saves ``replace(draft, retrieval=publication)``, so a release whose
    manifest minus its retrieval equals the draft, whose index layout is ``index_options``
    and whose every member carries the embedder's fingerprint, is what indexing the draft
    again would rebuild.
    """
    current_id = outputs.current_id()
    if current_id is None:
        return False
    try:
        current = outputs.load(current_id)
    except (OSError, ValueError):  # a damaged release is rebuilt, never reused (ADR 0029)
        return False
    if current.retrieval is None or replace(current, retrieval=None) != outputs.load(draft_id):
        return False
    plan, _ = outputs.load_retrieval(current.retrieval)
    if plan.index_version != index_options.index_version:
        return False
    return {member.embedding_fingerprint for member in plan.members} == {embedder.fingerprint}


def _emit(progress: Progress | None, event: str, **payload: object) -> None:
    if progress is not None:
        progress(event, payload)


def _page_reporter(
    progress: Progress | None,
    pdf: Path,
    budget: Callable[[], int],
    cancel: Event | None = None,
) -> Callable[[IngestProgress], None] | None:
    """``document_progress`` for ingest pages: each stage's first and last page, and in
    between at most one event per ``_PROGRESS_PAGES`` pages or ``_PROGRESS_SECONDS``.

    With ``cancel``, a finished page is where a stopping run leaves the document: that page's
    calls are recorded and their claims released, the next page's never start (ADR 0033)."""
    if progress is None and cancel is None:
        return None
    last: dict[str, Any] = {"stage": None, "done": 0, "at": 0.0}

    def report(update: IngestProgress) -> None:
        if cancel is not None and cancel.is_set():
            raise _Cancelled
        if progress is None:
            return
        now = perf_counter()
        if not (
            update.stage != last["stage"]
            or update.pages_done == update.pages_total
            or update.pages_done - last["done"] >= _PROGRESS_PAGES
            or now - last["at"] >= _PROGRESS_SECONDS
        ):
            return
        last.update(stage=update.stage, done=update.pages_done, at=now)
        _emit(
            progress,
            "document_progress",
            pdf=str(pdf),
            stage=update.stage,
            pages_done=update.pages_done,
            pages_total=update.pages_total,
            live_calls=update.live_calls,
            budget=budget(),
            cache_hits=update.cache_hits,
        )

    return report


def _current_mode(processing_store: Path) -> IngestMode | None:
    """The ingest mode of the snapshot ``current-processing`` names, or None without one."""
    outputs = ProcessingStore(processing_store)
    try:
        current_id = outputs.current_id()
        if current_id is None:
            return None
        try:
            return published_ingest_mode(outputs.load(current_id))
        except (OSError, ValueError):
            return None
    finally:
        outputs.close()


def _resolved_backend_kind(ingestion_root: Path) -> str:
    """The store backend this run will use under ``ingestion_root`` (ADR 0036): ``files``
    when pinned, else what the directory's availability probe says ``auto`` resolves to
    (explicit ``sqlite`` is reported as such; an unavailable directory then fails the
    stores themselves, never silently)."""
    mode = get_settings().object_store_backend
    if mode == "files":
        return "files"
    ingestion_root.mkdir(parents=True, exist_ok=True)
    return "sqlite" if mode == "sqlite" or probe_directory(ingestion_root).ok else "files"


def _run_document(
    pdf: Path,
    digest: str,
    *,
    root: Path,
    plan: IngestPlan,
    pages: str,
    per_pdf: int | Literal["auto"],
    budget: _Budget,
    requalify: bool,
    build_tree: bool,
    tree_max_live_calls: int,
    embedder: EmbeddingPort,
    continue_on_error: bool,
    progress: Progress | None,
    repairs: Counter[str] | None = None,
    backend_kind: str = "files",
    errors: tuple[type[Exception], ...] = (ValueError, OSError, StoreBusy),
    cancel: Event | None = None,
) -> tuple[DocumentRun, int]:
    """One PDF through every stage; returns the run and its tree's live calls.

    ``errors`` are recorded as the document's failure (``continue_on_error``); ``cancel``, once
    set, stops the document at its next page or stage boundary with ``_Cancelled``."""
    started = perf_counter()
    run: dict[str, Any] = {
        "pdf_path": str(pdf),
        "sha256": digest,
        "live_call_budget": 0,
        "ingest_mode": plan.mode,
        "retries": 0,
        "transient_failures": 0,
    }
    # ``held``: the shared-total allotments of this document not yet settled by ``spend``.
    allotment: dict[str, Any] = {"started": False, "cut": False, "held": 0}
    tree_calls = 0
    stage: PipelineStage = "ingest"
    # The stage-scoped stores of this document; closed when the document is over so the
    # sqlite backend's connections and writer lease are released (ADR 0036).
    sources: LocalDocumentStore | None = None
    outputs: ProcessingStore | None = None

    def allot(wanted: int) -> int:
        """Grant this document's ingest budget from the shared total and announce it."""
        granted = budget.allot(wanted)
        allotment.update(started=True, cut=granted < wanted, held=granted)
        run["live_call_budget"] = granted
        _emit(
            progress,
            "document_start",
            pdf=str(pdf),
            sha256=digest,
            budget=granted,
            ingest_mode=plan.mode,
            object_backend=backend_kind,
        )
        return granted

    def transient(retries: int, failures: int) -> None:
        run["retries"] += retries
        run["transient_failures"] += failures

    def finish(status: DocumentStatus) -> tuple[DocumentRun, int]:
        if not allotment["started"]:
            # "auto" fails before the page count is known: no budget was ever granted.
            _emit(
                progress,
                "document_start",
                pdf=str(pdf),
                sha256=digest,
                budget=0,
                ingest_mode=plan.mode,
                object_backend=backend_kind,
            )
        ingested: IngestionSummary | None = run.get("ingestion")
        if ingested is not None:
            run["published_ingest_mode"] = _current_mode(Path(ingested.processing_store))
        if repairs:
            run["storage_repairs"] = dict(sorted(repairs.items()))
        done = DocumentRun(status=status, elapsed_s=round(perf_counter() - started, 3), **run)
        # A recorded failure travels with the event, so a progress line shows its reason.
        reason: dict[str, object] = {
            key: run[key] for key in ("failed_stage", "error") if key in run
        }
        if ingested is not None:
            # A partly ingested document is still ``published``; these say how partly.
            reason.update(
                pages=f"{ingested.pages_complete}/{len(ingested.selected_physical_pages)}",
                pages_budget_deferred=ingested.pages_budget_deferred,
                pages_claim_blocked=ingested.pages_claim_blocked,
                retries=run["retries"],
                transient_failures=run["transient_failures"],
            )
        if repairs:
            reason["storage_repairs"] = sum(repairs.values())
        _emit(progress, "document_done", pdf=str(pdf), status=status, **reason)
        return done, tree_calls

    def enter(name: PipelineStage) -> PipelineStage:
        if cancel is not None and cancel.is_set():
            raise _Cancelled
        _emit(progress, "document_progress", pdf=str(pdf), stage=name)
        return name

    # "auto": granted once the source stage knows how many pages are selected, before any
    # model call; an integer is granted up front, as before.
    limit: int | Callable[[int], int] = (
        (lambda selected: allot(auto_live_call_budget(selected)))
        if per_pdf == "auto"
        else allot(per_pdf)
    )
    try:
        if cancel is not None and cancel.is_set():
            raise _Cancelled
        ingestion = ingest_pdf(
            pdf=pdf,
            pages=pages,
            output_dir=root,
            stage="semantics",
            max_live_calls=limit,
            progress=_page_reporter(progress, pdf, lambda: int(run["live_call_budget"]), cancel),
            ingest_mode=plan.mode,
            layout_policy=plan.layout,
            unverified_tables_as_rows=plan.unverified_tables_as_rows,
            unverified_table_structure=plan.unverified_table_structure,
        )
        budget.spend(ingestion.live_call_count, allotment["held"])
        allotment["held"] = 0
        run.update(ingestion=ingestion, live_calls=ingestion.live_call_count)
        transient(ingestion.retries, ingestion.transient_failures)
        source_store = Path(ingestion.source_store)
        processing_store = Path(ingestion.processing_store)
        sources = LocalDocumentStore(source_store, activate_on_publish=False)
        outputs = ProcessingStore(processing_store)
        run["object_backend"] = ingestion.object_backend
        if allotment["cut"] and _starved(outputs, ingestion.processing_id):
            return finish("budget_starved")
        draft_id = ingestion.processing_id
        if requalify:
            stage = enter("requalify")
            summary = requalify_visual_objects(
                sources, outputs, processing_id=ingestion.processing_id
            )
            run["requalification"] = RequalificationCounts.from_summary(summary)
            draft_id = summary.draft_processing_id or ingestion.processing_id
        stage = enter("qualify")
        qualification = qualify_draft(
            source_store=source_store, processing_store=processing_store, processing_id=draft_id
        )
        run["qualification"] = qualification
        if qualification.eligible_member_count == 0:
            return finish("nothing_to_index")
        stage = enter("index")
        indexed_id = draft_id
        if _reusable_index(outputs, draft_id, embedder, plan.index_options):
            indexed_id = outputs.load_current()[0]
            run["index_reused"] = True
        else:
            before = _embedding_counts(embedder)
            try:
                indexed = index_draft(
                    source_store=source_store,
                    processing_store=processing_store,
                    processing_id=draft_id,
                    embedder=embedder,
                    review=plan.review_exports,
                    index_options=plan.index_options,
                )
            finally:
                _, retries, failures = _embedding_counts(embedder)
                transient(retries - before[1], failures - before[2])
            run["index"] = indexed
            indexed_id = indexed.indexed_processing_id
        stage = enter("publish")
        publication = publish_draft(
            source_store=source_store,
            processing_store=processing_store,
            processing_id=indexed_id,
            activate_source=True,
        )
        run["publication"] = publication
        if build_tree:
            stage = enter("tree")
            granted = allotment["held"] = budget.allot(tree_max_live_calls)
            client = JsonCompletionClient(
                load_llm_config(),
                cache_dir=processing_store / "model-cache",
                max_live_calls=granted,
                timeout=180.0,
            )
            try:
                tree = annotate_document_tree(
                    sources,
                    outputs,
                    processing_id=publication.published_processing_id,
                    client=client,
                )
            finally:
                transient(client.retry_count, client.transient_failure_count)
            tree_calls = client.live_call_count
            budget.spend(tree_calls, granted)
            allotment["held"] = 0
            run["tree"] = tree
    except errors as error:
        if not continue_on_error:
            raise
        message = str(error) or type(error).__name__
        if str(error) and not isinstance(error, (ValueError, OSError)):
            # Only a worker thread records these (ADR 0033); the type says what broke.
            message = f"{type(error).__name__}: {message}"
        run.update(failed_stage=stage, error=message)
        # A published document whose tree failed is still published and answerable.
        return finish("published" if "publication" in run else "failed")
    finally:
        # An allotment a failed or stopped stage never settled goes back, uncounted, as before.
        budget.spend(0, allotment["held"])
        if outputs is not None:
            outputs.close()
        if sources is not None:
            sources.close()
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
    docs: Sequence[str] = (),
) -> tuple[int | None, int | None]:
    """Rank of the expected pages among the prompt members; ``docs`` keeps one document's pages."""
    hits = [
        member_pages[member_id]
        for member_id in envelope.get("member_ids", ())
        if member_id in member_pages
    ]
    return gold_rank(hits, groups, docs=docs), gold_rank(hits, groups, docs=docs, distinct=True)


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


def _routed_shas(
    reference: str, check: QuestionDocsCheck | None, documents: Sequence[DocumentRun]
) -> set[str]:
    """The document a reference routes to: the run of the PDF its resolution named."""
    resolution = None if check is None else check.resolution(reference)
    if resolution is None or resolution.pdf is None:
        return set()
    target = Path(check.folder if check is not None else "") / resolution.pdf
    return {
        item.sha256
        for item in documents
        if item.sha256 is not None and Path(item.pdf_path) == target
    }


def _unrouted_reason(question: BatchQuestion, check: QuestionDocsCheck | None) -> str:
    """Why the folder had no PDF for these references, from the pre-ingest check."""
    reasons = []
    for doc in question.doc:
        resolution = None if check is None else check.resolution(doc)
        if resolution is None or resolution.status == "matched":
            continue
        if resolution.status == "ambiguous":
            reasons.append(f"{doc!r} names several PDFs {list(resolution.ambiguous_with)}")
        else:
            close = [candidate.pdf for candidate in resolution.candidates]
            reasons.append(
                f"{doc!r} names no PDF of the folder"
                + (f" (closest, not used: {close})" if close else "")
            )
    return "; ".join(reasons)


def _cited_documents(envelope: dict[str, Any]) -> tuple[str, ...]:
    cited: list[str] = []
    for claim in envelope.get("claims", ()):
        for citation in claim.get("citations", ()):
            document = citation.get("document_sha256")
            if isinstance(document, str) and document not in cited:
                cited.append(document)
    return tuple(cited)


def _eval_questions(
    questions: Sequence[BatchQuestion],
    *,
    post: ChatPost | None,
    documents: Sequence[DocumentRun],
    mounted: frozenset[str],
    member_pages: dict[str, tuple[str, int]],
    progress: Progress | None,
    check: QuestionDocsCheck | None = None,
    restrict_to_question_doc: bool = False,
) -> EvalSummary:
    """Ask every question; by default across every published document of the run (ADR 0032).

    A question's ``doc`` then only labels the evaluation (``expected_doc``, ``cited_doc_hit``,
    the page rank judged inside it). ``restrict_to_question_doc`` restores the routing of ADR
    0022 for comparison: a question is asked of the one document its ``doc`` names, and one
    naming none or several is ``routing_failed``.
    """
    cases: list[EvalCase] = []
    ranks: list[tuple[int | None, int | None]] = []
    only = next(iter(mounted)) if len(mounted) == 1 else None
    for question in questions:
        groups = question.page_groups
        named: set[str] = (
            set().union(*(_routed_shas(doc, check, documents) for doc in question.doc)) & mounted
            if question.doc
            else set()
        )
        expected_doc = next(iter(named)) if len(named) == 1 else None
        document: str | None = only
        unrouted: str | None = None if mounted else "no document of this run is published"
        if restrict_to_question_doc and question.doc and unrouted is None:
            if expected_doc is not None:
                document = expected_doc
            else:
                unrouted = (
                    f"doc {list(question.doc)} names "
                    f"{'no' if not named else 'more than one'} published document "
                    "of this run"
                )
                reason = _unrouted_reason(question, check)
                if reason:
                    unrouted += f": {reason}"
        if post is None or unrouted is not None:
            cases.append(
                EvalCase(
                    case_id=question.id,
                    question=question.question,
                    document_id=None,
                    verdict="routing_failed",
                    failures=(unrouted or "no published document",),
                    expected=question.expected,
                    routing="failed",
                    expected_doc=expected_doc,
                )
            )
            if groups:
                ranks.append((None, None))
            continue
        body: dict[str, Any] = {
            "model": "enterprise-pdf-rag",
            "messages": [{"role": "user", "content": question.question}],
        }
        across = not restrict_to_question_doc and len(mounted) > 1
        if across:
            body["cross_document"] = True
        elif document is not None:
            body["document"] = document
        started = perf_counter()
        status_code, response = post(body)
        elapsed_ms = round((perf_counter() - started) * 1000, 1)
        envelope = _observed(response)
        verdict: CaseVerdict
        failures: list[str] = []
        page_rank: int | None = None
        answer: str | None = None
        # Across several documents a page number alone names no page: it is judged inside the
        # expected document, and not at all when the question names none (ADR 0032).
        judged = bool(groups) and (not across or expected_doc is not None)
        if status_code != 200 or not envelope:
            verdict = "routing_failed" if status_code == 422 else "http_error"
            failures.append(f"HTTP {status_code}: {json.dumps(response, ensure_ascii=False)[:200]}")
            if judged:
                ranks.append((None, None))
        else:
            answered = envelope.get("status") == "answered"
            verdict = "answered" if answered else "abstained"
            if judged:
                scope = (expected_doc,) if across and expected_doc is not None else ()
                rank, page_rank = _ranks(envelope, groups, member_pages, scope)
                ranks.append((rank, page_rank))
            expects = bool(groups) or bool(question.expected)
            if not answered and expects:
                failures.append("abstained, but the question expects an answer")
            prose = answer_prose(response["choices"][0]["message"]["content"])
            answer = prose
            if answered and question.expected and not content_hit(prose, question.expected):
                failures.append(f"answer does not contain the expected {question.expected!r}")
        cited = _cited_documents(envelope)
        # A question without ``doc`` is answered across documents even when restricted.
        routing: CaseRouting = (
            "cross_document"
            if not restrict_to_question_doc or envelope.get("searched_documents")
            else "doc"
        )
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
                routing=routing,
                searched_documents=len(envelope.get("searched_documents") or ())
                or (1 if envelope else 0),
                cited_documents=cited,
                expected_doc=expected_doc,
                cited_doc_hit=None if expected_doc is None or not cited else expected_doc in cited,
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
        "cross_document": sum(case.routing == "cross_document" for case in cases),
    }
    return EvalSummary(
        format="questions", totals=totals, metrics=_metrics(ranks), cases=tuple(cases)
    )


def _question_docs_lines(check: QuestionDocsCheck | None) -> list[str]:
    if check is None:
        return []
    lines = [
        f"- question docs: {len(check.resolutions)} referenced, "
        f"{len(check.resolutions) - len(check.unresolved)} matched {check.rule_counts}"
        + (
            f", unresolved {[item.reference for item in check.unresolved]}"
            if check.unresolved
            else ""
        )
    ]
    selection = check.selection
    if selection is not None and selection.mode == "first_matched":
        lines.append(
            f"- question selection first_matched (the PDF is in the folder, not that it holds the "
            f"answer): {len(selection.selected)} of {selection.max_questions} selected, "
            f"{len(selection.skipped)} skipped"
            + (
                " — the set has no more questions whose PDF is in the folder"
                if selection.short
                else ""
            )
        )
    return lines


def _ingest_count_lines(result: FolderPipelineResult) -> list[str]:
    """Per PDF: deterministic / fallback layout pages (ADR 0028), row tables (ADR 0027), embeds."""
    partition: list[str] = []
    rows: list[str] = []
    embeds: list[str] = []
    layout: list[str] = []
    for item in result.documents:
        name = Path(item.pdf_path).name
        ingested = item.ingestion
        if ingested is not None and (
            ingested.pages_partitioned_deterministically
            or ingested.pages_partitioned_onnx
            or ingested.pages_partition_model_fallback
        ):
            reasons = ", ".join(
                f"{code} {count}"
                for code, count in sorted(ingested.partition_fallback_reasons.items())
            )
            # ADR 0030: 只有 "onnx-layout" 策略会产生 onnx 页; 为 0 时行文与 ADR 0028 逐字节一致.
            onnx_part = (
                f"onnx {ingested.pages_partitioned_onnx}, "
                if ingested.pages_partitioned_onnx
                else ""
            )
            partition.append(
                f"- `{name}`: deterministic {ingested.pages_partitioned_deterministically}, "
                f"{onnx_part}model fallback {ingested.pages_partition_model_fallback}"
                + (f" ({reasons})" if reasons else "")
            )
        if ingested is not None and ingested.table_row_transcriptions:
            rows.append(
                f"- `{name}`: {ingested.table_row_transcriptions} tables, "
                f"{ingested.table_row_lines} rows"
            )
        if item.index is not None:
            embeds.append(
                f"- `{name}`: embedding requests {item.index.embedding_requests}, "
                f"objects embedded {item.index.embedded_objects}"
            )
            if item.index.row_unit_tables or item.index.unscored_running_members:
                layout.append(
                    f"- `{name}`: {item.index.row_unit_tables} tables as "
                    f"{item.index.row_units} row units, "
                    f"{item.index.unscored_running_members} running header / footer "
                    "objects unscored"
                )
    out: list[str] = []
    for title, block in (
        ("Page layout without a model call (ADR 0028):", partition),
        ("Tables indexed as verbatim rows (ADR 0027):", rows),
        ("Index embeddings (ADR 0026):", embeds),
        ("Index text layout (ADR 0027 / 0028 amendments):", layout),
    ):
        if block:
            out += ["", title, "", *block]
    return out


def _transient_lines(result: FolderPipelineResult) -> list[str]:
    """One line when any document met a transient provider error (ADR 0035); none otherwise."""
    retries = sum(item.retries for item in result.documents)
    failures = sum(item.transient_failures for item in result.documents)
    if not retries and not failures:
        return []
    return [
        f"- transient provider errors (429 / 5xx / timeout / connection): {retries} retries, "
        f"{failures} calls still failing after their retries (not cached; a rerun retries them)"
    ]


def _markdown(result: FolderPipelineResult) -> str:
    lines = [
        "# Folder pipeline report",
        "",
        f"- folder: `{result.folder}`",
        f"- ingestion root: `{result.ingestion_root}`",
        f"- ok: **{result.ok}**; budget exhausted: {result.budget_exhausted}",
        f"- ingest mode: **{result.ingest_mode}**",
        f"- layout: **{result.layout_policy}**",
        f"- object store backend: **{result.object_backend}** (ADR 0036)",
        f"- live calls: ingest {result.live_calls.ingest}, tree {result.live_calls.tree}, "
        f"answer {result.live_calls.answer}, total {result.live_calls.total}",
        *(
            [
                "- sampling parameters dropped (the endpoint refused them): "
                + ", ".join(result.sampling_parameters_dropped)
            ]
            if result.sampling_parameters_dropped
            else []
        ),
        *_transient_lines(result),
        *_question_docs_lines(result.question_docs),
        "",
        "| pdf | sha256 | status | stage | pages | eligible | index reused | live calls | s "
        "| mode | published mode | error |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in result.documents:
        eligible = "-" if item.qualification is None else item.qualification.eligible_member_count
        error = (item.error or item.duplicate_of or "-").replace("|", "\\|")[:160]
        pages = (
            "-"
            if item.ingestion is None
            else f"{item.ingestion.pages_complete}/{len(item.ingestion.selected_physical_pages)}"
        )
        lines.append(
            f"| `{Path(item.pdf_path).name}` | `{(item.sha256 or '-')[:12]}` | {item.status} | "
            f"{item.failed_stage or '-'} | {pages} | {eligible} | {item.index_reused} | "
            f"{item.live_calls} | {item.elapsed_s:.1f} | {item.ingest_mode} | "
            f"{item.published_ingest_mode or '-'} | {error} |"
        )
    skipped = [
        (Path(item.pdf_path).name, item.ingestion)
        for item in result.documents
        if item.ingestion is not None and item.ingestion.skipped_calls
    ]
    if skipped:
        lines += ["", "Model calls left unsent by the ingest mode (ADR 0025):", ""]
        lines += [
            f"- `{name}`: "
            + (
                ", ".join(
                    f"{kind} {count}" for kind, count in sorted(ingested.skipped_calls.items())
                )
                or "none"
            )
            for name, ingested in skipped
        ]
    lines += _ingest_count_lines(result)
    if result.eval is not None:
        scope = (
            "every published document of this run; a question's `doc` only labels the "
            "evaluation (expected_doc / cited_doc_hit)"
            if result.retrieval_scope == "all_documents"
            else "the one published document each question's `doc` names"
        )
        lines += [
            "",
            f"## Evaluation ({result.eval.format})",
            "",
            *([f"- retrieval scope: {scope}"] if result.eval.format == "questions" else []),
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
    max_live_calls_per_pdf: int | Literal["auto"],
    max_live_calls_total: int | None = None,
    requalify: bool = True,
    build_tree: bool | None = None,
    tree_max_live_calls: int = 50,
    answer_max_live_calls: int | None = None,
    ingest_mode: IngestMode = "full",
    layout_policy: LayoutPolicy | None = None,
    unverified_tables_as_rows: bool | None = None,
    table_row_index_units: bool | None = None,
    drop_running_lines_from_index: bool | None = None,
    unverified_table_structure: UnverifiedTableStructure | None = None,
    max_questions: int | None = None,
    question_selection: QuestionSelectionMode = "first",
    only_question_docs: bool = False,
    doc_aliases: Mapping[str, str] | None = None,
    on_unmatched_docs: Literal["error", "skip"] = "error",
    restrict_to_question_doc: bool = False,
    continue_on_error: bool = True,
    report_dir: Path | None = None,
    embedder: EmbeddingPort | None = None,
    reranker: ListwiseJudge | None = None,
    answer_llm: JsonCompletionClient | None = None,
    progress: Progress | None = None,
    max_parallel_documents: int = 1,
    answer_system_rules: str | None = None,
    answer_derivations: bool = False,
    answer_constants: Mapping[str, float] | None = None,
) -> FolderPipelineResult:
    """Ingest, requalify, qualify, index, publish and tree every PDF in ``folder``, then evaluate.

    ``folder``, ``questions`` and ``report_dir`` default to ``NB_PDF_DIR`` / ``NB_QUESTIONS_PATH`` /
    ``NB_REPORT_DIR`` (``get_settings()``); an argument always wins, and with neither the
    question set is not run and no report is written. Without a folder from either place this
    raises ``ValueError``. ``max_questions`` answers only N questions (``None`` = all):
    ``question_selection="first"`` the first N of the set, ``"first_matched"`` the first N, in
    set order, whose references name exactly one PDF of the folder (the others are skipped
    and listed; none at all is a ``QuestionDocsError`` before any work).

    Before any ingest, model call or write, every reference the asked questions make is
    resolved against the folder (``adapters/question_docs.py``: alias, exact name, stem, sha
    prefix, normalized name; never a near miss) and reported as ``question_docs`` / the
    ``question_docs_resolved`` event. That resolution decides what ``only_question_docs``
    ingests and labels the evaluation (``EvalCase.expected_doc`` / ``cited_doc_hit``); it does
    not steer the answer: every light question is asked across every published document of
    the run (ADR 0032). ``restrict_to_question_doc=True`` routes each question to the one
    document its ``doc`` names instead (ADR 0022), for comparison.
    ``doc_aliases`` maps a reference as the question set writes it to a file name / stem /
    sha prefix naming exactly one PDF (else ``QuestionDocsError``). ``only_question_docs``
    (with a question set) ingests only the PDFs those questions name; every other PDF is
    ``skipped_not_referenced`` and, unless a reference is a sha, never read. With it, a
    reference naming no PDF or several, or a light question without ``doc``, is a
    ``QuestionDocsError`` before any work (``on_unmatched_docs="error"``) or is let through
    (``"skip"``) and answered across the documents that were ingested.

    ``max_live_calls_per_pdf="auto"`` gives each PDF ``auto_live_call_budget(selected pages)``
    = pages * ``AUTO_CALLS_PER_PAGE`` + ``AUTO_CALLS_BASE``, capped at ``MAX_INGEST_LIVE_CALLS``,
    computed from the page count the source stage reads anyway (no extra open) and still
    bounded by ``max_live_calls_total``.

    ``ingest_mode`` (ADR 0025): ``"full"`` sends every model call, exactly as before;
    ``"lite"`` sends only the page layout, a chart's IR and a diagram's two branches, derives
    page metadata and chart descriptions deterministically and writes no review pages
    (``ingest_mode.IngestPlan``). ``build_tree=None``
    follows the mode (full builds the tree, lite does not); an explicit bool wins.
    ``layout_policy`` and ``unverified_tables_as_rows`` override that mode's preset (``None``
    keeps it): ``"deterministic-text-pages"`` partitions pages without figures or images from
    pdfspine blocks with no layout call and falls back to the model per page (ADR 0028, counted
    in each ``IngestionSummary``); ``unverified_tables_as_rows`` indexes a Table with no
    detected grid as its verbatim printed rows (ADR 0027; on in lite, off in full);
    ``unverified_table_structure="tsr"`` first gives such a table a grid inferred by the local
    SLANet-plus model, kept pending (ADR 0031), and needs its weights before any work.
    ``table_row_index_units`` / ``drop_running_lines_from_index`` override the index-text
    layout the same way (on in lite, off in full): a long row table scores as one unit per
    figure row with its header repeated, and a running header / footer scores as nothing.

    Raises ``ValueError`` for an invalid budget, ``FileNotFoundError`` for a missing folder or
    question set and ``PreflightError`` for a missing or unreachable dependency, all before
    any ingest or model call. An injected ``embedder`` / ``reranker`` / ``answer_llm`` skips
    its own check. A failing document is recorded and the rest continue unless
    ``continue_on_error`` is false, when its ``ValueError`` / ``OSError`` propagates.

    ``max_parallel_documents`` (ADR 0033; 1 = one at a time, exactly as before, at most
    ``MAX_PARALLEL_DOCUMENTS``) ingests that many PDFs at once on worker threads, each in its
    own ``<root>/<sha>/`` with its own stores and clients, and writes the very bytes the serial
    run writes. ``documents`` keeps discovery order; per-document events may interleave, each
    carrying its worker ``slot``, and the progress callback is entered by one thread at a
    time. The shared total is reserved per allotment, so it is never overspent; a worker's
    unexpected exception is recorded as that document's failure (``continue_on_error``). An
    interrupt stops queued documents from starting and running ones at their next page, then
    is raised. The question set is still answered one question at a time.

    ``answer_system_rules`` 追加到答案生成 system 文本之后(``answers.prompt.answer_system``);
    ``None`` 或空白时 system 文本逐字节不变。超长在任何 ingest 之前抛 ``ValueError``。规则正文
    不写进报告。``answer_derivations=True`` 放开模型计算/换算(ADR 0038:每个算出的数写成
    derivation 由代码复算),``answer_constants`` 是派生可引用的常量白名单(须同时放开);
    常量名或值非法、只给常量不放开,同样在任何写入之前抛 ``ValueError``,常量也不写进报告。
    """
    answer_system(answer_system_rules, derivations=answer_derivations, constants=answer_constants)
    _check_per_pdf(max_live_calls_per_pdf)
    _check_parallel(max_parallel_documents)
    _check_budget("tree_max_live_calls", tree_max_live_calls)
    _check_budget("answer_max_live_calls", answer_max_live_calls)
    _check_total(max_live_calls_total)
    _check_max_questions(max_questions)
    plan = ingest_plan(
        ingest_mode,
        layout_policy=layout_policy,
        unverified_tables_as_rows=unverified_tables_as_rows,
        table_row_index_units=table_row_index_units,
        drop_running_lines_from_index=drop_running_lines_from_index,
        unverified_table_structure=unverified_table_structure,
    )
    tree = plan.build_tree if build_tree is None else build_tree
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
    pdfs = discover_pdfs(folder)
    digests: dict[Path, str] = {}

    def digest_of(pdf: Path) -> str:
        if pdf not in digests:
            digests[pdf] = _hash_file(pdf)
        return digests[pdf]

    check: QuestionDocsCheck | None = None
    if question_set is not None:
        question_set, check = _plan_questions(
            question_set,
            pdfs,
            folder,
            max_questions=max_questions,
            selection_mode=question_selection,
            doc_aliases=doc_aliases,
            digest=digest_of,
        )
        _emit(
            progress,
            "question_docs_resolved",
            referenced=len(check.resolutions),
            matched=len(check.resolutions) - len(check.unresolved),
            rules=check.rule_counts,
            unmatched=[item.reference for item in check.unresolved],
            questions_without_doc=len(check.questions_without_doc),
            selected_questions=None
            if check.selection is None or check.selection.mode == "first"
            else len(check.selection.selected),
            skipped_questions=None
            if check.selection is None or check.selection.mode == "first"
            else len(check.selection.skipped),
        )
        if only_question_docs and on_unmatched_docs == "error":
            message = unresolved_message(check)
            if message is not None:
                raise QuestionDocsError(message)
    selected = (
        {folder / pdf for pdf in check.matched_pdfs()}
        if only_question_docs and check is not None
        else None
    )
    needs_rerank = isinstance(question_set, NlGoldSet) and any(
        case.request.rerank for case in question_set.cases if not case.offline_only
    )
    embedder, reranker = _preflight(embedder=embedder, reranker=reranker, needs_rerank=needs_rerank)
    if plan.unverified_table_structure == "tsr":
        try:
            pdfspine_tsr.table_structure_recognizer()
        except pdfspine_tsr.TableStructureUnavailable as error:
            raise PreflightError(str(error)) from error
    root = ingestion_root if ingestion_root is not None else settings.ingestion_root
    root = root.expanduser().resolve()
    budget = _Budget(max_live_calls_total)

    backend_kind = _resolved_backend_kind(root)
    _emit(
        progress,
        "discovered",
        folder=str(folder),
        count=len(pdfs),
        ingest_mode=plan.mode,
        object_backend=backend_kind,
    )

    parallel = max_parallel_documents > 1
    embedding_lock = Lock()

    def work(
        pdf: Path, digest: str, said: Progress | None, cancel: Event | None = None
    ) -> tuple[DocumentRun, int]:
        with recording_repairs() as repairs:
            return _run_document(
                pdf,
                digest,
                root=root,
                plan=plan,
                pages=pages,
                per_pdf=max_live_calls_per_pdf,
                budget=budget,
                requalify=requalify,
                build_tree=tree,
                tree_max_live_calls=tree_max_live_calls,
                embedder=_document_embedder(embedder, embedding_lock) if parallel else embedder,
                continue_on_error=continue_on_error,
                progress=said,
                repairs=repairs,
                backend_kind=backend_kind,
                errors=(Exception,) if parallel else (ValueError, OSError, StoreBusy),
                cancel=cancel,
            )

    documents: list[DocumentRun] = []
    jobs: list[tuple[int, Path, str]] = []
    first: dict[str, str] = {}
    tree_total = 0
    for pdf in pdfs:
        if selected is not None and pdf not in selected:
            documents.append(
                DocumentRun(
                    pdf_path=str(pdf), sha256=digests.get(pdf), status="skipped_not_referenced"
                )
            )
            _emit(progress, "document_skipped", pdf=str(pdf), reason="not referenced")
            continue
        digest = digest_of(pdf)
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
        if parallel:
            # Its place in discovery order, filled in once every job has finished.
            jobs.append((len(documents) + len(jobs), pdf, digest))
            continue
        run, tree_calls = work(pdf, digest, progress)
        documents.append(run)
        tree_total += tree_calls
    if jobs:
        with one_sampling_probe():
            finished = _run_in_parallel(
                [(pdf, digest) for _, pdf, digest in jobs],
                work,
                workers=min(max_parallel_documents, len(jobs)),
                progress=progress,
            )
        for (index, _, _), (run, tree_calls) in zip(jobs, finished, strict=True):
            documents.insert(index, run)
            tree_total += tree_calls
    ingest_total = sum(item.live_calls for item in documents)

    summary: EvalSummary | None = None
    answer_total = 0
    answer_client: JsonCompletionClient | None = None
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
        answer_granted = 0
        if llm is None:
            answer_granted = budget.allot(
                settings.answer_max_live_calls
                if answer_max_live_calls is None
                else answer_max_live_calls
            )
            llm = make_answer_llm(cache_dir=root / "model-cache", max_live_calls=answer_granted)
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
                answer_settings=(
                    None
                    if answer_system_rules is None
                    and not answer_derivations
                    and answer_constants is None
                    else AnswerSettings(
                        extra_system_rules=answer_system_rules,
                        allow_derivations=answer_derivations,
                        answer_constants=answer_constants,
                    )
                ),
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
                check=check,
                restrict_to_question_doc=restrict_to_question_doc,
            )
        answer_total = llm.live_call_count - before
        answer_client = llm
        budget.spend(answer_total, answer_granted)

    try:
        dropped = set(unsupported_sampling_parameters(load_llm_config()))
    except ProviderConfigurationError:
        dropped = set()
    if answer_client is not None:
        dropped.update(answer_client.dropped_parameters)
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
        sampling_parameters_dropped=tuple(sorted(dropped)),
        question_docs=check,
        ingest_mode=plan.mode,
        layout_policy=plan.layout,
        object_backend=backend_kind,
        retrieval_scope="question_doc" if restrict_to_question_doc else "all_documents",
    )
    if report_dir is not None:
        target = report_dir.expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        (target / "report.json").write_text(
            result.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (target / "report.md").write_text(_markdown(result), encoding="utf-8")
    if result.sampling_parameters_dropped:
        _emit(
            progress,
            "sampling_parameters_dropped",
            parameters=list(result.sampling_parameters_dropped),
        )
    _emit(progress, "done", ok=result.ok, live_calls=calls.total)
    return result
