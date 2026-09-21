"""Publish one offline page that prints several charts side by side under their own headings.

Page metadata (ADR 0013) is page-wide: every member of a page carries every region the page
names, so a slide of three country charts hands all three the same three countries. No other
offline fixture prints more than one chart on a page, so nothing offline could show what the
page geometry knows — which heading stands over which chart. This builds that page.

Everything here is a real release: a real source store, real layout objects, real page
metadata verified verbatim against the page's own spans, and chart members qualified under
``figure-source-labels-only-v1`` — the one chart policy that needs neither a PDF nor a
source-paint proof, so three of them cost three crops. ``scan_catalog`` re-derives every one
of them at mount, so a fixture that could not be published cannot be tested with either.

The three charts print the *same* two labels on purpose: without the column binding their
index texts are indistinguishable, which is exactly the ambiguity the binding removes.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.chart_publication import ChartPublicationReceipt
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.figure_label_qualification import (
    FIGURE_LABEL_SCOPE,
    qualify_source_labels,
)
from enterprise_pdf_rag.adapters.figure_reasoning import prepare_figure
from enterprise_pdf_rag.adapters.object_processing import ProcessingObjectAdapter
from enterprise_pdf_rag.adapters.page_metadata_extraction import PAGE_METADATA_STAGE
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.documents.models import (
    AssetRef,
    Bounds,
    DocumentManifest,
    PageRecord,
    RegionRecord,
    TextSidecar,
    TextSpan,
)
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    Confidence,
    DescriptionClaim,
    Evidence,
    ExecutionMode,
    TextDescription,
    TextField,
    Verification,
)
from enterprise_pdf_rag.processing.document_metadata import summarize_document
from enterprise_pdf_rag.processing.index_text import PageIndexContext
from enterprise_pdf_rag.processing.models import (
    CanonicalPage,
    LayoutObject,
    ObjectKind,
    ObjectProcessingRecord,
    PageInput,
    PagePartition,
    PageProcessingRecord,
    ProcessingManifest,
    ProcessingScope,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.page_metadata import (
    CandidateValue,
    PageMetadata,
    PageMetadataCandidate,
    PageType,
    verify_page_metadata,
)
from enterprise_pdf_rag.processing.service import canonical_page
from tests.enterprise_pdf_rag.processing.test_persistent_retrieval import RecordingEmbedding

PAGE_WIDTH, PAGE_HEIGHT = 720.0, 200.0
COVER_TITLE = "Meridian Interim Report 2026"
PAGE_TITLE = "VONB by market 1H26"
# The page's banner: one span as wide as the page's content, over all three columns.
BANNER = "ASEAN"
BANNER_BBOX = (28.0, 30.0, 690.0, 48.0)
# One heading per column, each standing directly above its own chart.
HEADINGS = ("Meridian Thailand", "Meridian Singapore", "Meridian Malaysia")
# The charts, left to right; the headings above them share their x extent.
COLUMNS: tuple[Bounds, ...] = (
    (40.0, 90.0, 260.0, 180.0),
    (280.0, 90.0, 500.0, 180.0),
    (520.0, 90.0, 700.0, 180.0),
)
HEADING_BOXES: tuple[Bounds, ...] = (
    (44.0, 60.0, 130.0, 74.0),
    (286.0, 60.0, 380.0, 74.0),
    (526.0, 60.0, 612.0, 74.0),
)
# What every column prints inside itself, and therefore claims. Identical in all three, so
# the only thing that can tell the charts apart is the heading each one stands under.
CHART_LABELS = ("VONB", "New business")
# The layout region that owns the page's title, banner and headings.
BAND = (20.0, 5.0, 700.0, 80.0)
_AUTHORED = Confidence(None, "authored fixture")


@dataclass(frozen=True, slots=True)
class ColumnPage:
    """Where the published release lives, and what its one chart page prints."""

    root: Path
    document_id: str
    page_index: int
    banner: str
    headings: tuple[str, ...]
    page_title: str
    display_title: str
    chart_body: str

    @property
    def header(self) -> str:
        """The ADR 0013 contextual header every member of the chart page carries."""
        return f"{self.display_title} | {self.page_title}"

    @property
    def page_regions(self) -> tuple[str, ...]:
        """Every region value the page itself carries, in page order."""
        return (self.banner, *self.headings)


def _svg(boxes: Sequence[Bounds]) -> bytes:
    """A page of filled rectangles: geometry only, because a render refuses live text."""
    paths = "".join(
        f'<path fill="#d31145" d="M{box[0]} {box[1]}L{box[2]} {box[1]}'
        f'L{box[2]} {box[3]}L{box[0]} {box[3]}Z"/>'
        for box in boxes
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_WIDTH:g}"'
        f' height="{PAGE_HEIGHT:g}" viewBox="0 0 {PAGE_WIDTH:g} {PAGE_HEIGHT:g}">'
        f"{paths}</svg>"
    ).encode()


def _sidecar(source_sha256: str, page_index: int, spans: Sequence[TextSpan]) -> TextSidecar:
    return TextSidecar("source-text-v1", source_sha256, page_index, tuple(spans))


def _chart_spans(column: int) -> tuple[TextSpan, ...]:
    """The two labels printed inside one column, well inside its own rectangle."""
    left, top = COLUMNS[column][0], COLUMNS[column][1]
    return (
        TextSpan(f"c{column}-label", CHART_LABELS[0], (left + 16, top + 10, left + 56, top + 22)),
        TextSpan(f"c{column}-series", CHART_LABELS[1], (left + 16, top + 30, left + 110, top + 42)),
    )


def _page_spans() -> tuple[TextSpan, ...]:
    """Title, banner and headings first, then each column's own labels in page order."""
    band = (
        TextSpan("title", PAGE_TITLE, (28.0, 8.0, 260.0, 24.0)),
        TextSpan("banner", BANNER, BANNER_BBOX),
        *(
            TextSpan(f"head-{index}", text, HEADING_BOXES[index])
            for index, text in enumerate(HEADINGS)
        ),
    )
    return band + tuple(span for column in range(len(COLUMNS)) for span in _chart_spans(column))


def _stage(object_id: str, stage: str, artifact: AssetRef) -> StageOutcome:
    fingerprint = sha256(f"authored-column-page:{object_id}:{stage}".encode()).hexdigest()
    return StageOutcome(stage, fingerprint, StageState.SUCCEEDED, "authored", artifact)


def _chart_record(
    assets: LocalDocumentStore, page: PageInput, column: int
) -> ObjectProcessingRecord:
    """One chart member: crop the column, claim the two labels it prints, qualify them."""
    prepared = prepare_figure(
        page=page,
        native_svg=_svg(COLUMNS),
        bbox=COLUMNS[column],
        region_id=f"column-{column}",
    )
    elements = {element.text: element for element in prepared.svg.elements}

    def evidence(label: str) -> Evidence:
        return Evidence((elements[label].element_id,), Verification.PENDING, _AUTHORED)

    raw_chart = ChartIR(
        prepared.svg.binding,
        "bar",
        (),
        (),
        "authored-chart-model",
        Verification.PENDING,
        ExecutionMode.PRODUCTION,
        TextField(CHART_LABELS[0], evidence(CHART_LABELS[0])),
    )
    raw_description = TextDescription(
        prepared.svg.binding,
        tuple(DescriptionClaim(label, evidence(label)) for label in CHART_LABELS),
        "authored-description-model",
        Verification.PENDING,
        ExecutionMode.PRODUCTION,
    )
    labels = qualify_source_labels(
        prepared.svg, raw_chart, raw_description, scope=FIGURE_LABEL_SCOPE
    )
    raw_chart_ref = assets.put(
        TypeAdapter(ChartIR).dump_json(raw_chart), media_type="application/json"
    )
    raw_description_ref = assets.put(
        TypeAdapter(TextDescription).dump_json(raw_description), media_type="application/json"
    )
    view_ref = assets.put(
        TypeAdapter[object](type(prepared.model_view)).dump_json(prepared.model_view),
        media_type="application/json",
    )
    chart_ref = assets.put(
        TypeAdapter(ChartIR).dump_json(labels.chart), media_type="application/json"
    )
    description_ref = assets.put(
        TypeAdapter(TextDescription).dump_json(labels.description), media_type="application/json"
    )
    svg_ref = assets.put(prepared.svg.svg.encode(), media_type="image/svg+xml")
    object_id = f"chart-{column}"
    receipt_ref = assets.put(
        ChartPublicationReceipt.model_validate(
            {
                "object_id": object_id,
                "source_manifest_id": page.source_manifest_id,
                "region_id": f"column-{column}",
                "ir": chart_ref,
                "description": description_ref,
                "source_svg": svg_ref,
                "raw_chart": raw_chart_ref,
                "raw_description": raw_description_ref,
                "view": view_ref,
                "qualification": labels.receipt,
            }
        )
        .model_dump_json()
        .encode(),
        media_type="application/json",
    )
    return ObjectProcessingRecord(
        object_id,
        ObjectKind.CHART,
        (
            _stage(object_id, "qualified_ir", chart_ref),
            _stage(object_id, "qualified_description", description_ref),
            _stage(object_id, "qualification", receipt_ref),
            _stage(object_id, "svg", svg_ref),
            _stage(object_id, "ir", raw_chart_ref),
            _stage(object_id, "description", raw_description_ref),
            _stage(object_id, "model_view", view_ref),
        ),
    )


def _metadata_stage(
    outputs: ProcessingStore, canonical: StageOutcome, payload: bytes
) -> StageOutcome:
    ref = outputs.assets.put(payload, media_type="application/json")
    fingerprint = sha256(repr(("authored-page-metadata", canonical.artifact)).encode()).hexdigest()
    return StageOutcome(PAGE_METADATA_STAGE, fingerprint, StageState.SUCCEEDED, "authored", ref)


def publish_column_page(root: Path, *, charts: int = 3) -> ColumnPage:
    """Publish a two-page document whose second page prints ``charts`` charts side by side.

    ``charts`` is how many of the three headed columns actually carry a chart: three read
    as columns, one is never ambiguous, and two leave the third heading standing over
    nothing — the two layouts a binding must refuse.
    """
    pdf_bytes = f"authored-column-page-source-{charts}".encode()
    document_id = sha256(pdf_bytes).hexdigest()
    sources = LocalDocumentStore(root / document_id / "source")
    outputs = ProcessingStore(root / document_id / "processing")
    pdf = sources.put(pdf_bytes, media_type="application/pdf")
    cover_span = TextSpan("cover-title", COVER_TITLE, (28.0, 40.0, 420.0, 60.0))
    cover_sidecar = _sidecar(pdf.sha256, 0, (cover_span,))
    page_sidecar = _sidecar(pdf.sha256, 1, _page_spans())
    cover_svg_ref = sources.put(_svg(()), media_type="image/svg+xml")
    page_svg_ref = sources.put(_svg(COLUMNS), media_type="image/svg+xml")
    cover_text_ref = sources.put(
        TypeAdapter(TextSidecar).dump_json(cover_sidecar), media_type="application/json"
    )
    page_text_ref = sources.put(
        TypeAdapter(TextSidecar).dump_json(page_sidecar), media_type="application/json"
    )
    manifest_id = sources.publish(
        DocumentManifest(
            "source-ingestion-v1",
            "meridian-columns.pdf",
            pdf,
            "authored-fixture",
            (
                PageRecord(0, PAGE_WIDTH, PAGE_HEIGHT, 0, cover_svg_ref, cover_text_ref, 1, ()),
                PageRecord(
                    1,
                    PAGE_WIDTH,
                    PAGE_HEIGHT,
                    0,
                    page_svg_ref,
                    page_text_ref,
                    len(page_sidecar.spans),
                    (),
                ),
            ),
            RegionRecord(
                1,
                (0.0, 0.0, PAGE_WIDTH, PAGE_HEIGHT),
                page_svg_ref,
                page_svg_ref,
                page_text_ref,
                (),
            ),
        )
    )
    cover = PageInput(
        manifest_id, pdf.sha256, 0, PAGE_WIDTH, PAGE_HEIGHT, cover_svg_ref, cover_sidecar
    )
    page = PageInput(
        manifest_id, pdf.sha256, 1, PAGE_WIDTH, PAGE_HEIGHT, page_svg_ref, page_sidecar
    )
    band = LayoutObject(
        "band",
        ObjectKind.TEXT,
        BAND,
        ("title", "banner", *(f"head-{index}" for index in range(len(HEADINGS)))),
        "literal transcription only",
        _AUTHORED,
    )
    charts_objects = tuple(
        LayoutObject(
            f"chart-{column}",
            ObjectKind.CHART,
            COLUMNS[column],
            tuple(span.span_id for span in _chart_spans(column)),
            "authored source",
            _AUTHORED,
        )
        for column in range(charts)
    )
    # A column without a chart still prints its labels; nothing owns them.
    unassigned = tuple(
        span.span_id for column in range(charts, len(COLUMNS)) for span in _chart_spans(column)
    )
    records = [ProcessingObjectAdapter(sources, outputs).process(page, band)]
    records.extend(_chart_record(outputs.assets, page, column) for column in range(charts))
    cover_metadata = verify_page_metadata(
        PageMetadataCandidate(
            PageType.COVER, "en", CandidateValue(COVER_TITLE, "cover-title"), None, (), ()
        ),
        cover_sidecar.spans,
        source_sha256=pdf.sha256,
        page_index=0,
    )
    page_metadata = verify_page_metadata(
        PageMetadataCandidate(
            PageType.CHART,
            "en",
            CandidateValue(PAGE_TITLE, "title"),
            None,
            (CandidateValue("1H26", "title"),),
            (
                CandidateValue(BANNER, "banner"),
                *(CandidateValue(text, f"head-{index}") for index, text in enumerate(HEADINGS)),
            ),
        ),
        page_sidecar.spans,
        source_sha256=pdf.sha256,
        page_index=1,
    )
    assert not cover_metadata.diagnostics and not page_metadata.diagnostics
    document_metadata = summarize_document((cover_metadata, page_metadata))
    assert document_metadata is not None and document_metadata.display_title is not None
    assert page_metadata.title is not None
    context = PageIndexContext(document_metadata.display_title.text, page_metadata.title.text, None)
    scope = ProcessingScope(manifest_id, pdf.sha256, 2, (0, 1))
    publication = ProcessingRetrieval(sources, outputs, RecordingEmbedding()).build(
        scope, tuple((1, record) for record in records), {1: context}
    )
    pages: list[PageProcessingRecord] = []
    for index, (source_page, objects, owned, metadata) in enumerate(
        (
            (cover, (), ("cover-title",), cover_metadata),
            (page, (band, *charts_objects), unassigned, page_metadata),
        )
    ):
        canonical = _stage(
            f"page-{index}",
            "canonical",
            outputs.assets.put(
                TypeAdapter(CanonicalPage).dump_json(canonical_page(source_page)),
                media_type="application/json",
            ),
        )
        partition = _stage(
            f"page-{index}",
            "partition",
            outputs.assets.put(
                TypeAdapter(PagePartition).dump_json(
                    PagePartition(
                        "layout-v2",
                        manifest_id,
                        pdf.sha256,
                        index,
                        "authored",
                        objects,
                        owned,
                    )
                ),
                media_type="application/json",
            ),
        )
        pages.append(
            PageProcessingRecord(
                index,
                canonical,
                partition,
                () if index == 0 else tuple(records),
                metadata=_metadata_stage(
                    outputs, canonical, TypeAdapter(PageMetadata).dump_json(metadata)
                ),
            )
        )
    outputs.publish(
        ProcessingManifest(
            "processing-v1",
            scope,
            "authored",
            tuple(pages),
            publication,
            document_metadata,
        ),
        sources=sources,
    )
    return ColumnPage(
        root,
        document_id,
        1,
        BANNER,
        HEADINGS,
        PAGE_TITLE,
        document_metadata.display_title.text,
        " ".join(CHART_LABELS),
    )
