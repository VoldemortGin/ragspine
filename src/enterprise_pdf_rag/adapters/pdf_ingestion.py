"""Generic PDF entry: compose existing source and selected-page stages as a draft."""

import re
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
from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient
from enterprise_pdf_rag.adapters.page_partition import ModelPagePartitioner
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.processing_export import export_processing_review
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.providers import load_llm_config
from enterprise_pdf_rag.adapters.semantic_objects import SemanticObjectAdapter
from enterprise_pdf_rag.core.settings import get_settings
from enterprise_pdf_rag.documents.models import AssetRef, DocumentSnapshot, DocumentSpec
from enterprise_pdf_rag.documents.service import ingest_document
from enterprise_pdf_rag.processing.models import StageOutcome, StageState

type IngestionStage = Literal["source", "layout", "semantics"]


class _Options(BoundaryModel):
    stage: IngestionStage
    max_live_calls: int = Field(ge=0, le=200)

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
    live_call_count: int
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
    producer = f"pdfspine/{pdfspine.__version__}; native-svg/text-dict-v1"
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
        with pdfspine.open(stream=pdf, filetype="pdf") as document:
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


def ingest_pdf(
    *,
    pdf: Path,
    pages: str = "all",
    output_dir: Path | None = None,
    stage: IngestionStage = "source",
    max_live_calls: int = 0,
) -> IngestionSummary:
    """Save complete PDF sources and selected downstream stages without activation.

    Model stages require explicit provider configuration. Their one shared budget
    covers layout and both semantic branches; zero permits existing cache only.
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
        None if client is None else ModelPagePartitioner(client, sources),
        SemanticObjectAdapter(sources, outputs, client, qualification_policy="none")
        if client is not None and stage == "semantics"
        else None,
        normalize_layout=False,
        activate=False,
        producer="generic-pdf-processing-v1",
    )
    processing_id, manifest = pipeline.run(source.manifest_id, selected_page_indices=selected)
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
        live_call_count=0 if client is None else client.live_call_count,
        review_path=str(review),
    )
