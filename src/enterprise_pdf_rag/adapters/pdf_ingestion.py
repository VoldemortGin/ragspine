"""Generic PDF entry: compose existing source and selected-page stages as a draft."""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pdfspine
from pydantic import Field, model_validator

from enterprise_pdf_rag.adapters.aia_ingestion import export_review
from enterprise_pdf_rag.adapters.aia_processing import (
    ProcessingPipeline,
    stage_fingerprint,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.adapters.page_metadata_extraction import annotate_page_metadata
from enterprise_pdf_rag.adapters.page_partition import ModelPagePartitioner
from enterprise_pdf_rag.adapters.pdf_password import open_pdf
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.processing_export import export_processing_review
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.semantic_objects import SemanticObjectAdapter
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.common.evidence.providers.providers import load_llm_config
from ragspine.extraction.evidence.document.models import AssetRef, DocumentSnapshot, DocumentSpec
from ragspine.extraction.evidence.document.service import ingest_document
from ragspine.extraction.evidence.page.models import (
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
    activated: Literal[False] = False
    indexed: Literal[False] = False
    retrieval_status: Literal[
        "not_ready; qualification, indexing and publication require a separate workflow"
    ] = "not_ready; qualification, indexing and publication require a separate workflow"
    review_path: str


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
    sources: LocalDocumentStore, *, pdf: bytes, filename: str, pages: str
) -> tuple[DocumentSnapshot, tuple[int, ...], bool]:
    digest = sha256(pdf).hexdigest()
    producer = f"pdfspine/{pdfspine.__version__}; native-svg/text-dict-v2"
    fingerprint = stage_fingerprint("source", producer, (digest, filename))
    cache = ProcessingStore(sources.root)
    cached = cache.cached(fingerprint)
    if cached is not None:
        assert cached.artifact is not None
        snapshot = sources.load(cached.artifact.sha256)
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


def ingest_pdf(
    *,
    pdf: Path,
    pages: str = "all",
    output_dir: Path | None = None,
    stage: IngestionStage = "source",
    max_live_calls: int = 0,
    progress: Callable[[IngestProgress], None] | None = None,
) -> IngestionSummary:
    """Save complete PDF sources and selected downstream stages without activation.

    Model stages require explicit provider configuration. Their one shared budget
    covers layout, both semantic branches and page metadata; zero permits existing
    cache only. ``metadata`` runs page metadata over the source stage alone.
    ``progress`` hears about every finished page of the layout and metadata stages.
    """
    options = _Options(stage=stage, max_live_calls=max_live_calls)
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
    client = (
        None
        if stage == "source"
        else JsonCompletionClient(
            load_llm_config(),
            cache_dir=outputs.root / "model-cache",
            max_live_calls=options.max_live_calls,
            timeout=180.0,
        )
    )
    source, selected, cached = _source(sources, pdf=data, filename=pdf.name, pages=pages)
    pipeline = ProcessingPipeline(
        sources,
        outputs,
        None if client is None or stage == "metadata" else ModelPagePartitioner(client, sources),
        SemanticObjectAdapter(sources, outputs, client, qualification_policy="none")
        if client is not None and stage == "semantics"
        else None,
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
        )
        processing_id = metadata.annotated_processing_id
    complete, deferred, blocked = _page_counts(
        manifest,
        {}
        if metadata is None
        else {page.page_index: (page.state, page.diagnostic) for page in metadata.pages},
    )
    export_review(sources, source)
    review = export_processing_review(
        sources,
        outputs,
        processing_id,
        update_current=False,
        title=f"PDF 处理审阅: {pdf.name}",
    )
    return IngestionSummary(
        source_sha256=source.manifest.source.sha256,
        source_manifest_id=source.manifest_id,
        processing_id=processing_id,
        source_store=str(sources.root),
        processing_store=str(outputs.root),
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
        review_path=str(review),
    )
