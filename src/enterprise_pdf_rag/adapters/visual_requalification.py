"""Re-prove a published snapshot's visual objects from the branches it already stores.

``semantic_objects`` proves a Diagram the moment its two model branches return. A
snapshot processed before that rule existed carries the branches (``svg`` / ``ir`` /
``description`` / ``model_view``) but only a "no independent verifier" ``qualification``
diagnostic, and re-running the whole semantics stage would re-enter the model path and
re-derive every other object. This module re-applies the same deterministic proof to
those stored bytes alone — no model, no network, no re-inference — and saves the result
as a new content-addressed draft. Nothing is indexed or published and no pointer moves.
"""

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Literal

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.chart_publication import (
    ChartPublicationReceipt,
    parse_chart_receipt,
)
from enterprise_pdf_rag.adapters.diagram_publication import DiagramPublicationReceipt
from enterprise_pdf_rag.adapters.diagram_qualification import (
    DiagramQualificationError,
    qualify_diagram,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.figure_label_qualification import qualify_source_labels
from enterprise_pdf_rag.adapters.figure_reasoning import (
    ContextualFigureModelView,
    FigureModelView,
    prepare_figure,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.documents.models import AssetRef, DocumentSnapshot, TextSpan
from enterprise_pdf_rag.figures.models import ChartIR, FigureError, TextDescription
from enterprise_pdf_rag.processing.models import (
    ObjectKind,
    ObjectProcessingRecord,
    PageInput,
    PageProcessingRecord,
    ProcessingScope,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.typed_ir import DiagramIR, ObjectDescription

REQUALIFICATION_PRODUCER = "visual-requalification-v1"
# The stored branches one Diagram proof reads; all four must already have succeeded.
# A Chart re-projection reads exactly the same four, for the same reason.
_DIAGRAM_INPUTS = ("svg", "ir", "description", "model_view")
_CHART_INPUTS = _DIAGRAM_INPUTS
# What a proof writes. A record already carrying them is left exactly as it is.
_PROVEN_STAGES = ("qualified_ir", "qualified_description", "qualification")
# The one chart scope this module must never touch: it carries the ADR 0008 native-sector
# and source-paint proof, which is strictly stronger than the verbatim projection here.
_GEOMETRY_PROVED_SCOPE = "explicit-distribution-shares"

Outcome = Literal["qualified", "withheld", "unchanged"]


@dataclass(frozen=True, slots=True)
class ObjectRequalification:
    """One visual object's verdict; ``diagnostic`` is the verbatim proof failure."""

    page_index: int
    object_id: str
    kind: ObjectKind
    outcome: Outcome
    diagnostic: str | None
    qualified_claim_count: int


@dataclass(frozen=True, slots=True)
class RequalificationSummary:
    processing_id: str
    draft_processing_id: str | None
    objects: tuple[ObjectRequalification, ...]


@dataclass(frozen=True, slots=True)
class _StageWriter:
    """Write one object's proof stages.

    ``semantic_objects._Writer`` caches every stage it writes because repeating a model
    call is expensive. This proof reads stored bytes and pure geometry only, so a re-run
    re-derives byte-identical artifacts (and therefore the same draft id) without the
    stage cache; staying out of it leaves the published store's cache untouched.
    """

    outputs: ProcessingStore
    object_id: str
    source_manifest_id: str
    inputs: tuple[AssetRef, ...]

    def _fingerprint(self, stage: str) -> str:
        return sha256(
            repr(
                (
                    "visual-requalification-stage-v1",
                    stage,
                    REQUALIFICATION_PRODUCER,
                    self.source_manifest_id,
                    self.object_id,
                    tuple(ref.sha256 for ref in self.inputs),
                )
            ).encode()
        ).hexdigest()

    def save(self, stage: str, payload: bytes) -> StageOutcome:
        ref = self.outputs.assets.put(payload, media_type="application/json")
        return StageOutcome(
            stage, self._fingerprint(stage), StageState.SUCCEEDED, REQUALIFICATION_PRODUCER, ref
        )

    def withheld(self, stage: str, reason: str) -> StageOutcome:
        return StageOutcome(
            stage,
            self._fingerprint(stage),
            StageState.UNAVAILABLE,
            REQUALIFICATION_PRODUCER,
            diagnostic=reason,
        )


def _artifact(stage: StageOutcome) -> AssetRef:
    if stage.artifact is None:
        raise ValueError("A succeeded requalification stage must carry its actual artifact")
    return stage.artifact


def _succeeded_refs(
    record: ObjectProcessingRecord, names: tuple[str, ...]
) -> dict[str, AssetRef] | None:
    """Every named stage's artifact, or ``None`` as soon as one did not succeed."""
    stages = {stage.stage: stage for stage in record.stages}
    refs: dict[str, AssetRef] = {}
    for name in names:
        stage = stages.get(name)
        if stage is None or stage.state is not StageState.SUCCEEDED or stage.artifact is None:
            return None
        refs[name] = stage.artifact
    return refs


def _requalify_diagram(
    outputs: ProcessingStore,
    page_index: int,
    record: ObjectProcessingRecord,
    *,
    spans: tuple[TextSpan, ...],
    source_manifest_id: str,
    dry_run: bool,
) -> tuple[ObjectProcessingRecord, ObjectRequalification]:
    """Prove one Diagram against its stored crop and the page's source occurrences.

    A dry run returns the record untouched, so nothing is written to the asset store.
    """

    def report(outcome: Outcome, diagnostic: str | None, claims: int) -> ObjectRequalification:
        return ObjectRequalification(
            page_index, record.object_id, record.kind, outcome, diagnostic, claims
        )

    if _succeeded_refs(record, ("qualified_ir",)) is not None:
        return record, report("unchanged", None, record.qualified_claim_count)
    inputs = _succeeded_refs(record, _DIAGRAM_INPUTS)
    if inputs is None:
        return record, report(
            "unchanged", "Stored diagram branches are incomplete; there is nothing to re-prove.", 0
        )
    writer = _StageWriter(
        outputs,
        record.object_id,
        source_manifest_id,
        tuple(inputs[name] for name in _DIAGRAM_INPUTS),
    )
    ir = TypeAdapter(DiagramIR).validate_json(outputs.assets.get(inputs["ir"]), strict=True)
    try:
        qualified = qualify_diagram(
            svg=outputs.assets.get(inputs["svg"]),
            spans=spans,
            ir=ir,
            source_manifest_id=source_manifest_id,
        )
    except DiagramQualificationError as error:
        if dry_run:
            return record, report("withheld", str(error), 0)
        kept = tuple(stage for stage in record.stages if stage.stage not in _PROVEN_STAGES)
        withheld = replace(
            record,
            stages=(*kept, writer.withheld("qualification", str(error))),
            qualified_claim_count=0,
        )
        return withheld, report("withheld", str(error), 0)
    claims = len(qualified.ir.nodes) + len(qualified.ir.edges)
    if dry_run:
        return record, report("qualified", None, claims)
    ir_stage = writer.save("qualified_ir", TypeAdapter(DiagramIR).dump_json(qualified.ir))
    description_stage = writer.save(
        "qualified_description", TypeAdapter(ObjectDescription).dump_json(qualified.description)
    )
    receipt = DiagramPublicationReceipt(
        object_id=record.object_id,
        source_manifest_id=source_manifest_id,
        ir=_artifact(ir_stage),
        description=_artifact(description_stage),
        source_svg=inputs["svg"],
        raw_ir=inputs["ir"],
        raw_description=inputs["description"],
        view=inputs["model_view"],
        qualification=qualified.qualification,
    )
    kept = tuple(stage for stage in record.stages if stage.stage not in _PROVEN_STAGES)
    proven = replace(
        record,
        stages=(
            *kept,
            ir_stage,
            description_stage,
            writer.save("qualification", receipt.model_dump_json().encode()),
        ),
        qualified_claim_count=claims,
    )
    return proven, report("qualified", None, claims)


def _geometry_proved(outputs: ProcessingStore, record: ObjectProcessingRecord) -> bool:
    """True for a chart already carrying the ADR 0008 sector + source-paint proof.

    That proof is strictly stronger than the verbatim projection this module applies, so
    such a member is left exactly as published. Anything unreadable is treated as proved,
    because re-projecting a receipt we cannot parse would silently weaken it.
    """
    refs = _succeeded_refs(record, ("qualification",))
    if refs is None:
        return False
    try:
        receipt = parse_chart_receipt(outputs.assets.get(refs["qualification"]))
    except ValueError:
        return True
    return receipt.qualification.semantic_scope == _GEOMETRY_PROVED_SCOPE


def _requalify_chart(
    outputs: ProcessingStore,
    page_index: int,
    record: ObjectProcessingRecord,
    *,
    sources: LocalDocumentStore,
    source: DocumentSnapshot,
    scope: ProcessingScope,
    dry_run: bool,
) -> tuple[ObjectProcessingRecord, ObjectRequalification]:
    """Re-project one Chart from its stored branches under the current label scope.

    Unlike the Diagram branch this also revisits a chart that already qualified: the
    ADR 0016 scope keeps the points that ``figure-source-labels-only-v1`` blanked, so a
    published labels-only member has facts to gain. A geometry-proved member is skipped.
    """

    def report(outcome: Outcome, diagnostic: str | None, claims: int) -> ObjectRequalification:
        return ObjectRequalification(
            page_index, record.object_id, record.kind, outcome, diagnostic, claims
        )

    if _geometry_proved(outputs, record):
        return record, report("unchanged", None, record.qualified_claim_count)
    inputs = _succeeded_refs(record, _CHART_INPUTS)
    if inputs is None:
        return record, report(
            "unchanged", "Stored chart branches are incomplete; there is nothing to re-prove.", 0
        )
    writer = _StageWriter(
        outputs,
        record.object_id,
        scope.source_manifest_id,
        tuple(inputs[name] for name in _CHART_INPUTS),
    )
    view: FigureModelView | ContextualFigureModelView = TypeAdapter(
        FigureModelView | ContextualFigureModelView
    ).validate_json(outputs.assets.get(inputs["model_view"]), strict=True, extra="forbid")
    base = view.base_view if isinstance(view, ContextualFigureModelView) else view
    page_record = source.manifest.pages[page_index]
    prepared = prepare_figure(
        page=PageInput(
            scope.source_manifest_id,
            scope.source_sha256,
            page_index,
            page_record.width,
            page_record.height,
            page_record.svg,
            read_text_sidecar(sources, source, page_index),
        ),
        native_svg=sources.get(page_record.svg),
        bbox=base.bbox,
        # The layout item's extraction region is not carried in the record; the object id
        # identifies the same region and never enters the SVG bytes, which the next check proves.
        region_id=record.object_id,
        context_span_ids=(
            tuple(observation.source_span_id for observation in view.page_context)
            if isinstance(view, ContextualFigureModelView)
            else ()
        ),
    )
    chart = TypeAdapter(ChartIR).validate_json(outputs.assets.get(inputs["ir"]), strict=True)
    description = TypeAdapter(TextDescription).validate_json(
        outputs.assets.get(inputs["description"]), strict=True
    )
    reason: str | None = None
    if prepared.svg.svg.encode() != outputs.assets.get(inputs["svg"]):
        reason = "Stored chart SVG does not derive from the pinned source page and model view"
    else:
        try:
            projection = qualify_source_labels(prepared.svg, chart, description)
        except FigureError as error:
            reason = str(error)
    if reason is not None:
        if dry_run:
            return record, report("withheld", reason, 0)
        kept = tuple(stage for stage in record.stages if stage.stage not in _PROVEN_STAGES)
        withheld = replace(
            record,
            stages=(*kept, writer.withheld("qualification", reason)),
            qualified_claim_count=0,
        )
        return withheld, report("withheld", reason, 0)
    claims = sum(point.value.value is not None for point in projection.chart.points)
    if dry_run:
        return record, report("qualified", None, claims)
    ir_stage = writer.save("qualified_ir", TypeAdapter(ChartIR).dump_json(projection.chart))
    description_stage = writer.save(
        "qualified_description", TypeAdapter(TextDescription).dump_json(projection.description)
    )
    receipt = ChartPublicationReceipt(
        object_id=record.object_id,
        source_manifest_id=scope.source_manifest_id,
        region_id=record.object_id,
        ir=_artifact(ir_stage),
        description=_artifact(description_stage),
        source_svg=inputs["svg"],
        raw_chart=inputs["ir"],
        raw_description=inputs["description"],
        view=inputs["model_view"],
        qualification=projection.receipt,
    )
    kept = tuple(stage for stage in record.stages if stage.stage not in _PROVEN_STAGES)
    proven = replace(
        record,
        stages=(
            *kept,
            ir_stage,
            description_stage,
            writer.save("qualification", receipt.model_dump_json().encode()),
        ),
        qualified_claim_count=claims,
    )
    return proven, report("qualified", None, claims)


def requalify_visual_objects(
    sources: LocalDocumentStore,
    outputs: ProcessingStore,
    *,
    processing_id: str,
    dry_run: bool = False,
) -> RequalificationSummary:
    """Re-prove one snapshot's visual objects and save the outcome as a new draft.

    Every other object and every page stage is copied verbatim, and the draft is saved
    under a new content-addressed id without moving ``current-processing``. ``dry_run``
    reports the verdicts and writes nothing at all. When no record changes, no draft is
    saved and ``draft_processing_id`` stays ``None``.
    """
    manifest = outputs.load(processing_id)
    source = sources.load(manifest.scope.source_manifest_id)
    reports: list[ObjectRequalification] = []
    pages: list[PageProcessingRecord] = []
    changed = False
    for page in manifest.pages:
        spans: tuple[TextSpan, ...] | None = None
        objects: list[ObjectProcessingRecord] = []
        for record in page.objects:
            # Kind dispatch for the deterministic visual proofs. ADR 0015 admits Diagram
            # (geometry + verbatim span labels); ADR 0016 re-projects a Chart's verbatim
            # points. The Formula branch proves its token IR the same way and plugs in here.
            revised = record
            verdict: ObjectRequalification | None = None
            if record.kind is ObjectKind.DIAGRAM:
                if spans is None:
                    spans = read_text_sidecar(sources, source, page.page_index).spans
                revised, verdict = _requalify_diagram(
                    outputs,
                    page.page_index,
                    record,
                    spans=spans,
                    source_manifest_id=manifest.scope.source_manifest_id,
                    dry_run=dry_run,
                )
            elif record.kind is ObjectKind.CHART:
                revised, verdict = _requalify_chart(
                    outputs,
                    page.page_index,
                    record,
                    sources=sources,
                    source=source,
                    scope=manifest.scope,
                    dry_run=dry_run,
                )
            if verdict is None:
                objects.append(record)
                continue
            changed = changed or revised != record
            objects.append(revised)
            reports.append(verdict)
        pages.append(replace(page, objects=tuple(objects)))
    if dry_run or not changed:
        return RequalificationSummary(processing_id, None, tuple(reports))
    draft = outputs.save_draft(
        replace(
            manifest,
            pages=tuple(pages),
            # The eligible member set changed, so the pinned plan no longer describes this
            # snapshot; ``index_draft`` rebuilds it, exactly as after the page metadata stage.
            retrieval=None,
            producer=f"{REQUALIFICATION_PRODUCER}:{processing_id}",
        ),
        sources=sources,
    )
    return RequalificationSummary(processing_id, draft, tuple(reports))
