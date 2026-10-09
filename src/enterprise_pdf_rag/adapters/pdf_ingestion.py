"""Generic PDF entry: compose existing source and selected-page stages as a draft."""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pdfspine
from pydantic import Field, TypeAdapter, model_validator

from enterprise_pdf_rag.adapters.aia_ingestion import export_review
from enterprise_pdf_rag.adapters.aia_processing import (
    ProcessingPipeline,
    stage_fingerprint,
)
from enterprise_pdf_rag.adapters.deterministic_partition import (
    EMPTY_PARTITION_COUNTS,
    partition_counts,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.adapters.ingest_mode import (
    IngestMode,
    IngestPlan,
    LayoutFallback,
    LayoutPolicy,
    UnverifiedTableStructure,
    ingest_plan,
    make_partitioner,
)
from enterprise_pdf_rag.adapters.page_metadata_extraction import (
    PageMetadataSummary,
    annotate_page_metadata,
)
from enterprise_pdf_rag.adapters.pdf_password import open_pdf
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.processing_export import export_processing_review
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.semantic_objects import SemanticObjectAdapter
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.file_placement import note_repair
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.common.evidence.providers.providers import load_llm_config
from ragspine.extraction.evidence.document.models import AssetRef, DocumentSnapshot, DocumentSpec
from ragspine.extraction.evidence.document.service import ingest_document
from ragspine.extraction.evidence.objects.tables.table_inferred_grid import (
    TSR_FALLBACK,
    TSR_PRODUCER,
)
from ragspine.extraction.evidence.objects.tables.table_rows import (
    TABLE_ROWS_PRODUCER,
    TableRowsIR,
)
from ragspine.extraction.evidence.page.models import (
    ObjectKind,
    ProcessingManifest,
    StageOutcome,
    StageState,
)

type IngestionStage = Literal["source", "layout", "semantics", "metadata"]
# Stages that add the page metadata stage after the page pipeline (same budget and cache).
_METADATA_STAGES = frozenset({"semantics", "metadata"})
# The per-PDF live-call ceiling. A safety rail against a typo, not a cost model: a page costs
# one layout call, one metadata call and two per chart-like object, so a several-hundred-page
# report needs far more than the 200 this used to be (docs/enterprise-pdf-rag/adr/0022).
MAX_INGEST_LIVE_CALLS = 10_000
# The model-call outcomes a page can be left with without having been processed.
_BUDGET_CODE = "call_budget_exhausted"
_CLAIM_CODE = "request_in_progress_or_uncertain"


@dataclass(frozen=True, slots=True)
class IngestProgress:
    """One page finished in an ingest stage; counts and identifiers only, never content."""

    stage: Literal["layout", "metadata"]
    pages_done: int
    pages_total: int
    live_calls: int
    cache_hits: int


class _Options(BoundaryModel):
    stage: IngestionStage
    max_live_calls: int = Field(ge=0, le=MAX_INGEST_LIVE_CALLS)

    @model_validator(mode="after")
    def source_has_no_live_budget(self) -> "_Options":
        if self.stage == "source" and self.max_live_calls:
            raise ValueError("The source stage cannot use a model-call budget")
        return self


class IngestionSummary(BoundaryModel):
    source_sha256: str
    source_manifest_id: str
    processing_id: str
    source_store: str
    processing_store: str
    # Which store backend holds this document's bytes (ADR 0036): "files" or "sqlite".
    object_backend: str = "files"
    source_page_count: int
    selected_physical_pages: tuple[int, ...]
    page_selection_scope: Literal["downstream-only; complete source retained"] = (
        "downstream-only; complete source retained"
    )
    stage: IngestionStage
    source_cached: bool
    layout_succeeded_pages: int
    object_count: int
    failed_stage_count: int
    semantic_status: str
    metadata_status: str
    metadata_page_states: dict[str, int]
    display_title: str | None
    # Every source page, not only the selected ones; "unassessed" is a source cut before
    # text-layer diagnostics existed. OCR pages are physical (1-based) and not remedied.
    text_layer_page_states: dict[str, int]
    ocr_needed_pages: tuple[int, ...]
    live_call_count: int
    # Selected pages whose layout, page metadata and object stages all finished; pages left
    # deferred because the call budget ran out; pages whose model call is still claimed by
    # another (possibly dead) process. Zero in a report written before they existed.
    pages_complete: int = 0
    pages_budget_deferred: int = 0
    pages_claim_blocked: int = 0
    # Model calls not sent because another, possibly still running, attempt holds their claim
    # (``request_in_progress_or_uncertain``), and claims of dead attempts taken over and resent.
    calls_claim_blocked: int = 0
    claims_taken_over: int = 0
    # ADR 0035: requests resent after a transient failure (429, 5xx, timeout, connection), and
    # calls that still failed transiently once their retries were spent (no record is written,
    # so the next run calls them again).
    retries: int = 0
    transient_failures: int = 0
    # ADR 0025: the mode this ingest ran in and the model calls it left unsent by mode (by
    # category, ``ingest_mode.SKIPPED_CALL_KINDS``).
    ingest_mode: IngestMode = "full"
    skipped_calls: dict[str, int] = Field(default_factory=dict)
    # Table objects with no detected grid transcribed as verbatim printed rows, and their row
    # count (ADR 0027, ``IngestPlan.unverified_tables_as_rows``); counts only, never text.
    table_row_transcriptions: int = 0
    table_row_lines: int = 0
    # ADR 0031, ``unverified_table_structure="tsr"``: such tables given a model-inferred
    # (pending) grid, those that fell back to verbatim rows (also counted just above) and the
    # fallback reason codes. Counts only; all zero under ``"rows"``.
    table_tsr_grids: int = 0
    table_tsr_fallbacks: int = 0
    table_tsr_fallback_reasons: dict[str, int] = Field(default_factory=dict)
    # ADR 0028, ``layout="deterministic-text-pages"``: pages partitioned without a model call,
    # pages that fell back to the model layout and their reason codes; all zero under the
    # model layout. Re-derived from the saved partitions, so a cache replay reports the same.
    # ADR 0030, ``layout="onnx-layout"``: pages partitioned by the local ONNX layout model
    # (zero LLM calls) are counted apart in ``pages_partitioned_onnx``.
    pages_partitioned_deterministically: int = 0
    pages_partition_model_fallback: int = 0
    partition_fallback_reasons: dict[str, int] = Field(default_factory=dict)
    pages_partitioned_onnx: int = 0
    # ADR 0039, ``layout_fallback`` other than ``"model"``: pages the routers handed to the
    # text-layer partitioner instead of the model (zero calls; not in the model count above),
    # their innermost reason codes, and ONNX pages that kept a low-confidence result.
    pages_partition_text_fallback: int = 0
    partition_text_fallback_reasons: dict[str, int] = Field(default_factory=dict)
    pages_onnx_low_confidence_accepted: int = 0
    activated: Literal[False] = False
    indexed: Literal[False] = False
    retrieval_status: Literal[
        "not_ready; qualification, indexing and publication require a separate workflow"
    ] = "not_ready; qualification, indexing and publication require a separate workflow"
    # None when the mode writes no review pages (lite); ``export_document_review`` writes them.
    review_path: str | None


def _selected_pages(pages: str, page_count: int) -> tuple[int, ...]:
    if pages == "all":
        return tuple(range(page_count))
    selected: list[int] = []
    for part in pages.split(","):
        if re.fullmatch(r"[1-9][0-9]*(?:-[1-9][0-9]*)?", part) is None:
            raise ValueError("Pages must be 'all' or physical pages such as 1-3,5")
        bounds = tuple(map(int, part.split("-")))
        first, last = bounds[0], bounds[-1]
        if not 1 <= first <= last <= page_count:
            raise ValueError(f"Pages must exist in the {page_count}-page PDF")
        selected.extend(range(first - 1, last))
    if len(selected) != len(set(selected)):
        raise ValueError("Pages must not repeat or overlap")
    return tuple(sorted(selected))


def _source(
    sources: LocalDocumentStore,
    cache: ProcessingStore,
    *,
    pdf: bytes,
    filename: str,
    pages: str,
) -> tuple[DocumentSnapshot, tuple[int, ...], bool]:
    digest = sha256(pdf).hexdigest()
    producer = f"pdfspine/{pdfspine.__version__}; native-svg/text-dict-v2"
    fingerprint = stage_fingerprint("source", producer, (digest, filename))
    cached = cache.cached(fingerprint)
    if cached is not None:
        assert cached.artifact is not None
        try:
            snapshot = sources.load(cached.artifact.sha256)
        except (OSError, ValueError):
            # A page or text object lost on disk (ADR 0029): extract again from the PDF; the
            # same bytes are put back under the same digests, so the manifest id is unchanged.
            note_repair("source")
            cached = None
    if cached is not None:
        if (
            snapshot.manifest.source.sha256,
            snapshot.manifest.filename,
            snapshot.manifest.producer,
        ) != (digest, filename, producer):
            raise ValueError("Cached source differs from its input identity")
        return snapshot, _selected_pages(pages, len(snapshot.manifest.pages)), True
    try:
        with open_pdf(pdf) as document:
            count = document.page_count
            if count == 0:
                raise ValueError("PDF has no pages")
            selected = _selected_pages(pages, count)
            rect = document.load_page(0).rect
            bounds = (0.0, 0.0, float(rect.width), float(rect.height))
    except pdfspine.PdfError as error:
        raise ValueError("PDF cannot be opened by the source adapter") from error
    spec = DocumentSpec(filename, digest, count, 0, bounds)
    manifest_id = ingest_document(
        pdf, spec=spec, extractor=PdfspineDocumentAdapter(), store=sources
    )
    ref = AssetRef(manifest_id, "application/json", len(sources.read_content(manifest_id)))
    cache.cache(StageOutcome("source", fingerprint, StageState.SUCCEEDED, producer, ref))
    return sources.load(manifest_id), selected, False


def _page_counts(
    manifest: ProcessingManifest, metadata: dict[int, tuple[StageState, str | None]]
) -> tuple[int, int, int]:
    """(complete, budget-deferred, claim-blocked) selected pages of one ingest."""
    complete = deferred = blocked = 0
    for page in manifest.pages:
        outcomes = [
            (outcome.state, outcome.diagnostic)
            for outcome in (page.partition, *(s for item in page.objects for s in item.stages))
        ]
        if page.page_index in metadata:
            outcomes.append(metadata[page.page_index])
        diagnostics = " ".join(diagnostic or "" for _, diagnostic in outcomes)
        if _CLAIM_CODE in diagnostics:
            blocked += 1
        elif _BUDGET_CODE in diagnostics or any(
            state is StageState.DEFERRED for state, _ in outcomes
        ):
            deferred += 1
        elif page.partition.state is StageState.SUCCEEDED and all(
            state is not StageState.FAILED for state, _ in outcomes
        ):
            complete += 1
    return complete, deferred, blocked


def _row_tables(outputs: ProcessingStore, manifest: ProcessingManifest) -> tuple[int, ...]:
    """The row count of every Table qualified as verbatim rows (ADR 0027)."""
    counts: list[int] = []
    for page in manifest.pages:
        for item in page.objects:
            stages = {stage.stage: stage for stage in item.stages}
            ir = stages.get("ir")
            if (
                item.kind is ObjectKind.TABLE
                and ir is not None
                and ir.artifact is not None
                and ir.producer.endswith(":" + TABLE_ROWS_PRODUCER)
            ):
                rows = TypeAdapter(TableRowsIR).validate_json(outputs.assets.get(ir.artifact))
                counts.append(len(rows.rows))
    return tuple(counts)


def _tsr_tables(manifest: ProcessingManifest) -> tuple[int, Counter[str]]:
    """Tables given an inferred grid, and the fallback reasons of those that were not (ADR 0031)."""
    grids = 0
    fallbacks: Counter[str] = Counter()
    for page in manifest.pages:
        for item in page.objects:
            if item.kind is not ObjectKind.TABLE:
                continue
            stages = {stage.stage: stage for stage in item.stages}
            ir = stages.get("ir")
            structure = stages.get("table_structure")
            if (
                ir is not None
                and ir.state is StageState.SUCCEEDED
                and f":{TSR_PRODUCER}:" in ir.producer
            ):
                grids += 1
            elif structure is not None and (structure.diagnostic or "").startswith(TSR_FALLBACK):
                fallbacks[(structure.diagnostic or "").split(":")[1]] += 1
    return grids, fallbacks


def ingest_pdf(
    *,
    pdf: Path,
    pages: str = "all",
    output_dir: Path | None = None,
    stage: IngestionStage = "source",
    max_live_calls: int | Callable[[int], int] = 0,
    progress: Callable[[IngestProgress], None] | None = None,
    ingest_mode: IngestMode = "full",
    layout_policy: LayoutPolicy | None = None,
    unverified_tables_as_rows: bool | None = None,
    unverified_table_structure: UnverifiedTableStructure | None = None,
    layout_fallback: LayoutFallback | None = None,
) -> IngestionSummary:
    """Save complete PDF sources and selected downstream stages without activation.

    Model stages require explicit provider configuration. Their one shared budget
    covers layout, both semantic branches and page metadata; zero permits existing
    cache only. ``metadata`` runs page metadata over the source stage alone.
    ``progress`` hears about every finished page of the layout and metadata stages.
    ``max_live_calls`` may instead be a function of the number of selected pages, asked
    once the source stage knows it and before any model call (run-folder's ``"auto"``).
    ``ingest_mode`` picks the calls the semantic stages send (ADR 0025, ``ingest_mode.py``);
    ``layout_policy`` and ``unverified_tables_as_rows`` override that mode's preset for one
    switch each (ADR 0028, ADR 0027), ``None`` keeps the preset; so does
    ``unverified_table_structure`` (ADR 0031: ``"tsr"`` needs the SLANet-plus weights and
    ``pdfspine[onnx]``, and refuses to start without them). ``layout_fallback`` (ADR 0039)
    defaults to the settings' ``layout_fallback`` (``APP_LAYOUT_FALLBACK``).
    """
    plan = ingest_plan(
        ingest_mode,
        layout_policy=layout_policy,
        layout_fallback=layout_fallback
        if layout_fallback is not None
        else get_settings().layout_fallback,
        unverified_tables_as_rows=unverified_tables_as_rows,
        unverified_table_structure=unverified_table_structure,
    )
    options = _Options(
        stage=stage,
        max_live_calls=max_live_calls if isinstance(max_live_calls, int) else 0,
    )
    if not pdf.is_file():
        raise ValueError("--pdf must name an existing PDF file")
    data = pdf.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ValueError("Expected a PDF file beginning with %PDF-")
    parent = (
        (output_dir if output_dir is not None else get_settings().ingestion_root)
        .expanduser()
        .resolve()
    )
    document_root = parent / sha256(data).hexdigest()
    sources = LocalDocumentStore(document_root / "source", activate_on_publish=False)
    outputs = ProcessingStore(document_root / "processing")
    # The source stage's own cache lives in the source root: share that root's backend so the
    # writer lease and the connections are held once; closing ``sources`` releases both.
    source_cache = ProcessingStore(sources.root, backend=sources.backend)
    try:
        return _ingest_pdf(
            pdf=pdf,
            data=data,
            pages=pages,
            stage=stage,
            max_live_calls=max_live_calls,
            progress=progress,
            plan=plan,
            options=options,
            sources=sources,
            outputs=outputs,
            source_cache=source_cache,
        )
    finally:
        outputs.close()
        sources.close()


def _ingest_pdf(
    *,
    pdf: Path,
    data: bytes,
    pages: str,
    stage: IngestionStage,
    max_live_calls: int | Callable[[int], int],
    progress: Callable[[IngestProgress], None] | None,
    plan: IngestPlan,
    options: _Options,
    sources: LocalDocumentStore,
    outputs: ProcessingStore,
    source_cache: ProcessingStore,
) -> IngestionSummary:
    config = None if stage == "source" else load_llm_config()
    source, selected, cached = _source(
        sources, source_cache, pdf=data, filename=pdf.name, pages=pages
    )
    if config is not None and not isinstance(max_live_calls, int):
        options = _Options(
            stage=stage,
            max_live_calls=max_live_calls(len(selected)),
        )
    client = (
        None
        if config is None
        else JsonCompletionClient(
            config,
            cache_dir=outputs.root / "model-cache",
            max_live_calls=options.max_live_calls,
            timeout=180.0,
        )
    )
    objects = (
        SemanticObjectAdapter(sources, outputs, client, qualification_policy="none", plan=plan)
        if client is not None and stage == "semantics"
        else None
    )
    pipeline = ProcessingPipeline(
        sources,
        outputs,
        None
        if client is None or stage == "metadata"
        else make_partitioner(plan, client, sources, source),
        objects,
        normalize_layout=False,
        activate=False,
        producer="generic-pdf-processing-v1",
    )

    def reporter(name: Literal["layout", "metadata"]) -> Callable[[int, int], None] | None:
        if progress is None:
            return None

        def report(done: int, total: int) -> None:
            assert progress is not None
            progress(
                IngestProgress(
                    name,
                    done,
                    total,
                    0 if client is None else client.live_call_count,
                    0 if client is None else client.cache_hit_count,
                )
            )

        return report

    processing_id, manifest = pipeline.run(
        source.manifest_id, selected_page_indices=selected, on_page=reporter("layout")
    )
    metadata = None
    if stage in _METADATA_STAGES:
        metadata = annotate_page_metadata(
            sources,
            outputs,
            processing_id=processing_id,
            client=client,
            on_page=reporter("metadata"),
            deterministic=plan.page_metadata == "deterministic",
        )
        processing_id = metadata.annotated_processing_id
    partition_tally = (
        partition_counts(outputs, manifest) if plan.layout != "model" else EMPTY_PARTITION_COUNTS
    )
    complete, deferred, blocked = _page_counts(
        manifest,
        {}
        if metadata is None
        else {page.page_index: (page.state, page.diagnostic) for page in metadata.pages},
    )
    row_tables = _row_tables(outputs, manifest)
    tsr_grids, tsr_fallbacks = _tsr_tables(manifest)
    review = (
        _export_review(sources, outputs, source, processing_id, title=f"PDF 处理审阅: {pdf.name}")
        if plan.review_exports
        else None
    )
    return IngestionSummary(
        source_sha256=source.manifest.source.sha256,
        source_manifest_id=source.manifest_id,
        processing_id=processing_id,
        source_store=str(sources.root),
        processing_store=str(outputs.root),
        object_backend=outputs.object_backend,
        source_page_count=len(source.manifest.pages),
        selected_physical_pages=manifest.scope.physical_pages,
        stage=stage,
        source_cached=cached,
        layout_succeeded_pages=sum(
            page.partition.state is StageState.SUCCEEDED for page in manifest.pages
        ),
        object_count=sum(len(page.objects) for page in manifest.pages),
        failed_stage_count=sum(
            outcome.state is StageState.FAILED
            for page in manifest.pages
            for outcome in (
                page.partition,
                *(outcome for item in page.objects for outcome in item.stages),
            )
        ),
        semantic_status="attempted; inspect per-object stages; not qualified for QA or indexed"
        if stage == "semantics"
        else "deferred; no object semantics or index",
        metadata_status="deferred; page metadata has not run"
        if metadata is None
        else "attempted; inspect per-page metadata stages; values are verbatim page spans",
        metadata_page_states={} if metadata is None else metadata.page_states,
        display_title=None if metadata is None else metadata.display_title,
        text_layer_page_states=dict(
            Counter(
                "unassessed" if page.text_layer is None else str(page.text_layer.status)
                for page in source.manifest.pages
            )
        ),
        ocr_needed_pages=tuple(
            page.page_index + 1
            for page in source.manifest.pages
            if page.text_layer is not None and page.text_layer.needs_ocr
        ),
        live_call_count=0 if client is None else client.live_call_count,
        pages_complete=complete,
        pages_budget_deferred=deferred,
        pages_claim_blocked=blocked,
        calls_claim_blocked=0 if client is None else client.claim_blocked_count,
        claims_taken_over=0 if client is None else client.claims_taken_over,
        retries=0 if client is None else client.retry_count,
        transient_failures=0 if client is None else client.transient_failure_count,
        ingest_mode=plan.mode,
        skipped_calls=_skipped_calls(objects, metadata),
        table_row_transcriptions=len(row_tables),
        table_row_lines=sum(row_tables),
        table_tsr_grids=tsr_grids,
        table_tsr_fallbacks=sum(tsr_fallbacks.values()),
        table_tsr_fallback_reasons=dict(sorted(tsr_fallbacks.items())),
        pages_partitioned_deterministically=partition_tally.deterministic_pages,
        pages_partition_model_fallback=partition_tally.model_fallback_pages,
        partition_fallback_reasons=partition_tally.fallback_reasons,
        pages_partitioned_onnx=partition_tally.onnx_pages,
        pages_partition_text_fallback=partition_tally.text_fallback_pages,
        partition_text_fallback_reasons=partition_tally.text_fallback_reasons,
        pages_onnx_low_confidence_accepted=partition_tally.onnx_low_confidence_accepted_pages,
        review_path=None if review is None else str(review),
    )


def _skipped_calls(
    objects: SemanticObjectAdapter | None, metadata: PageMetadataSummary | None
) -> dict[str, int]:
    """Model calls this ingest left unsent by its mode, by category; empty in full mode."""
    counts = Counter[str]() if objects is None else Counter(objects.skipped_calls)
    if metadata is not None and metadata.deterministic_pages:
        counts["page_metadata"] += metadata.deterministic_pages
    return dict(sorted(counts.items()))


def _export_review(
    sources: LocalDocumentStore,
    outputs: ProcessingStore,
    source: DocumentSnapshot,
    processing_id: str,
    *,
    title: str,
) -> Path:
    export_review(sources, source)
    return export_processing_review(
        sources, outputs, processing_id, update_current=False, title=title
    )


def export_document_review(document_root: Path, processing_id: str | None = None) -> Path:
    """Write one ingested document's review pages on demand; returns the processing review.

    ``document_root`` is ``<ingestion root>/<source sha256>``. ``processing_id`` defaults to
    the published snapshot (``current-processing``), else the newest draft is not guessed:
    a document never published needs an explicit id. No model or network call; the pages
    are exactly what a full-mode ingest writes on every run.
    """
    sources = LocalDocumentStore(document_root / "source", activate_on_publish=False)
    outputs = ProcessingStore(document_root / "processing")
    try:
        if processing_id is None:
            if outputs.current_id() is None:
                raise ValueError("document has no published snapshot; pass processing_id")
            processing_id = outputs.load_current()[0]
        manifest = outputs.load(processing_id)
        source = sources.load(manifest.scope.source_manifest_id)
        return _export_review(
            sources,
            outputs,
            source,
            processing_id,
            title=f"PDF 处理审阅: {source.manifest.filename}",
        )
    finally:
        outputs.close()
        sources.close()
