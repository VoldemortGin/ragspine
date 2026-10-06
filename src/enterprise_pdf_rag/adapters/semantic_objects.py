"""Persist independently inferred object branches and separately scoped qualifications."""

import json
import re
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters import pdfspine_tsr
from enterprise_pdf_rag.adapters.chart_publication import ChartPublicationReceipt
from enterprise_pdf_rag.adapters.chart_semantics import (
    ChartInference,
    DescriptionInference,
    ModelChartExtractor,
    ModelDescriptionGenerator,
    ModelOutputBindingError,
    describe_from_ir,
)
from enterprise_pdf_rag.adapters.diagram_publication import DiagramPublicationReceipt
from enterprise_pdf_rag.adapters.diagram_qualification import (
    DiagramQualificationError,
    qualify_diagram,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.donut_qualification import DonutQualification
from enterprise_pdf_rag.adapters.figure_label_qualification import qualify_source_labels
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure, prepare_figure
from enterprise_pdf_rag.adapters.formula_qualification import (
    FormulaPublicationReceipt,
    check_model_description,
    qualify_formula,
)
from enterprise_pdf_rag.adapters.ingest_mode import SKIPPED_CODE, IngestPlan, ingest_plan
from enterprise_pdf_rag.adapters.object_processing import ProcessingObjectAdapter
from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg
from enterprise_pdf_rag.adapters.pdfspine_tables import PdfspineTableAdapter
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.shared_pdf import opened_pdf, source_pdf
from enterprise_pdf_rag.adapters.source_objects import source_table_description
from enterprise_pdf_rag.adapters.visual_semantics import VisualInference, VisualSemanticAdapter
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
)
from ragspine.extraction.evidence.document.models import AssetRef, TextSidecar
from ragspine.extraction.evidence.figures.models import (
    ChartIR,
    Confidence,
    SourceAnchor,
    TextDescription,
    Verification,
)
from ragspine.extraction.evidence.objects.formulas.formula_models import (
    FormulaQualification,
    FormulaSourceObservation,
)
from ragspine.extraction.evidence.objects.tables.table_grid_proof import GRID_SCOPE
from ragspine.extraction.evidence.objects.tables.table_inferred_grid import (
    TSR_FALLBACK,
    TSR_SCOPE,
    InferredGridRejection,
)
from ragspine.extraction.evidence.objects.tables.table_models import TableExtractionResult, TableIR
from ragspine.extraction.evidence.objects.tables.table_rows import (
    TABLE_ROWS_METHOD,
    TABLE_ROWS_PRODUCER,
    TABLE_ROWS_SCOPE,
    TableRowsIR,
    check_table_rows,
    rows_text,
    table_rows,
)
from ragspine.extraction.evidence.objects.typed_ir import (
    DiagramIR,
    FormulaIR,
    LiteralQualification,
    ObjectDescription,
)
from ragspine.extraction.evidence.page.models import (
    LayoutObject,
    ObjectKind,
    ObjectProcessingRecord,
    PageInput,
    StageOutcome,
    StageState,
)


@dataclass(frozen=True, slots=True)
class _Writer:
    outputs: ProcessingStore
    page: PageInput
    item: LayoutObject
    producer: str

    def save(
        self, stage: str, payload: bytes, media_type: str = "application/json"
    ) -> StageOutcome:
        fingerprint = sha256(
            repr(
                (
                    "semantic-object-stage-v1",
                    stage,
                    self.producer,
                    self.page.source_manifest_id,
                    self.page.native_svg,
                    self.item,
                )
            ).encode()
        ).hexdigest()
        ref = self.outputs.assets.put(payload, media_type=media_type)
        outcome = StageOutcome(stage, fingerprint, StageState.SUCCEEDED, self.producer, ref)
        self.outputs.cache(outcome)
        return outcome

    def diagnostic(
        self, stage: str, reason: str, *, failed: bool = False, skipped: bool = False
    ) -> StageOutcome:
        """An unfinished stage; ``skipped`` marks one the ingest mode chose not to run."""
        fingerprint = sha256(
            repr(
                (
                    "semantic-object-stage-v1",
                    stage,
                    self.producer,
                    self.page.source_manifest_id,
                    self.item,
                )
            ).encode()
        ).hexdigest()
        return StageOutcome(
            stage,
            fingerprint,
            StageState.NOT_APPLICABLE
            if skipped
            else StageState.FAILED
            if failed
            else StageState.UNAVAILABLE,
            self.producer,
            diagnostic=reason,
        )


def _ref(stage: StageOutcome) -> AssetRef:
    if stage.artifact is None:
        raise ValueError("Successful semantic artifact is unavailable")
    return stage.artifact


def _error(error: ValueError) -> str:
    if isinstance(error, JsonCompletionError):
        return f"{error.code}; request_fingerprint={error.request_fingerprint}"
    return str(error)


class SemanticObjectAdapter:
    def __init__(
        self,
        sources: LocalDocumentStore,
        outputs: ProcessingStore,
        client: JsonCompletionClient,
        *,
        qualification_policy: Literal["none", "source-labels-only", "donut"] = "none",
        description_corrections: tuple[str, ...] = (),
        chart_corrections: tuple[str, ...] = (),
        plan: IngestPlan | None = None,
    ) -> None:
        self.sources = sources
        self.outputs = outputs
        self.client = client
        self.qualification_policy = qualification_policy
        # ADR 0025: which object calls this ingest sends; ``None`` is full, byte for byte.
        self.plan = ingest_plan("full") if plan is None else plan
        # ADR 0031: resolved up front, so a missing model or runtime stops the ingest with an
        # explicit message instead of every unruled table silently falling back to rows.
        self.table_structure = (
            pdfspine_tsr.table_structure_recognizer()
            if self.plan.unverified_table_structure == "tsr"
            else None
        )
        # Model calls left unsent by the plan, by ``ingest_mode.SKIPPED_CALL_KINDS`` category.
        self.skipped_calls: Counter[str] = Counter()
        if (
            len(set(description_corrections)) != len(description_corrections)
            or len(description_corrections) > 2
            or any(
                re.fullmatch(r"[0-9a-f]{64}", value) is None for value in description_corrections
            )
        ):
            raise ValueError(
                "At most two explicit original description request fingerprints are allowed"
            )
        self.description_corrections = description_corrections
        if len(chart_corrections) > 1 or any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None for value in chart_corrections
        ):
            raise ValueError("At most one explicit original chart request fingerprint is allowed")
        self.chart_corrections = chart_corrections

    def process(self, page: PageInput, item: LayoutObject) -> ObjectProcessingRecord:
        if item.kind in (ObjectKind.TEXT, ObjectKind.LIST, ObjectKind.GROUP):
            return ProcessingObjectAdapter(self.sources, self.outputs).process(page, item)
        writer = _Writer(
            self.outputs,
            page,
            item,
            "semantic-object-v2:"
            + self.client.fingerprint
            + ":"
            + self.qualification_policy
            + ("" if self.plan.object_variant is None else ":" + self.plan.object_variant),
        )
        if self.description_corrections:
            writer = _Writer(
                self.outputs,
                page,
                item,
                writer.producer
                + ":corrections:"
                + sha256(repr(tuple(sorted(self.description_corrections))).encode()).hexdigest(),
            )
        if self.chart_corrections:
            writer = _Writer(
                self.outputs,
                page,
                item,
                writer.producer
                + ":chart-corrections:"
                + sha256(repr(self.chart_corrections).encode()).hexdigest(),
            )
        native = self.sources.get(page.native_svg)
        crop = crop_native_svg(
            native.decode(), width=page.width, height=page.height, bbox=item.bbox
        ).encode()
        sidecar = TextSidecar(
            "object-source-text-v1",
            page.source_sha256,
            page.page_index,
            tuple(span for span in page.text.spans if span.span_id in item.source_span_ids),
        )
        stages = [
            writer.save("native_crop", crop, "image/svg+xml"),
            writer.save("source_text", TypeAdapter(TextSidecar).dump_json(sidecar)),
        ]
        if item.kind is ObjectKind.CHART:
            return self._chart(page, item, native, writer, stages)
        if item.kind is ObjectKind.TABLE:
            return self._table(page, item, writer, stages, crop)
        if item.kind is ObjectKind.IMAGE and not self.plan.image_semantics:
            return self._skipped_image(writer, stages)
        skip = (
            f"{SKIPPED_CODE}: this ingest mode sends no formula model call; the proof reads "
            "the PDF itself"
            if item.kind is ObjectKind.FORMULA and not self.plan.formula_semantics
            else None
        )
        try:
            result = VisualSemanticAdapter(self.client).infer(
                page=page, item=item, native_svg=native, skip_model=skip
            )
        except ValueError as error:
            return self._unavailable(writer, stages, _error(error))
        if skip is not None:
            self.skipped_calls["formula"] += 2
        stages.extend(
            (
                writer.save("svg", result.crop_svg, "image/svg+xml"),
                writer.save("model_render", result.model_png, "image/png"),
                writer.save("model_view", result.model_view_json),
            )
        )
        branch: dict[str, StageOutcome] = {}
        for name, value, raw, diagnostic in (
            ("ir", result.ir, result.ir_raw_json, result.ir_diagnostic),
            (
                "description",
                result.description,
                result.description_raw_json,
                result.description_diagnostic,
            ),
        ):
            if raw is not None:
                stages.append(writer.save(name + "_raw", raw))
            skipped = diagnostic is not None and diagnostic.startswith(SKIPPED_CODE)
            outcome = (
                writer.diagnostic(
                    name,
                    diagnostic or "No source-bound result was returned",
                    failed=not skipped,
                    skipped=skipped,
                )
                if value is None
                else writer.save(name, TypeAdapter[object](type(value)).dump_json(value))
            )
            stages.append(outcome)
            branch[name] = outcome
        if item.kind is ObjectKind.FORMULA:
            return self._formula(
                page,
                item,
                writer,
                stages,
                result.ir if isinstance(result.ir, FormulaIR) else None,
                result.description,
            )
        if item.kind is not ObjectKind.DIAGRAM:
            stages.append(
                writer.diagnostic(
                    "qualification",
                    "Visual semantics are source-bound model inferences; an independent field/relationship verifier is not available for this object.",
                )
            )
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        return self._diagram(page, item, result, writer, stages, branch)

    def _skipped_image(self, writer: _Writer, stages: list[StageOutcome]) -> ObjectProcessingRecord:
        """An Image the plan sends no call for: registered with its crop, never retrievable."""
        self.skipped_calls["image"] += 2
        reason = (
            f"{SKIPPED_CODE}: this ingest mode sends no image model call; Image objects are "
            "not retrievable"
        )
        stages.extend(
            writer.diagnostic(name, reason, skipped=True)
            for name in ("ir", "description", "qualification")
        )
        return ObjectProcessingRecord(writer.item.object_id, writer.item.kind, tuple(stages))

    def _diagram(
        self,
        page: PageInput,
        item: LayoutObject,
        result: VisualInference,
        writer: _Writer,
        stages: list[StageOutcome],
        branch: dict[str, StageOutcome],
    ) -> ObjectProcessingRecord:
        """Prove the model's structure against the same crop; an unproven diagram stays out.

        Like ``_table`` this runs whatever ``qualification_policy`` was selected: the proof
        is deterministic and reads no model.
        """
        if not isinstance(result.ir, DiagramIR) or result.description is None:
            stages.append(
                writer.diagnostic(
                    "qualification",
                    "Both actual source-bound branches are required; no description-only fallback is admitted.",
                )
            )
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        try:
            qualified = qualify_diagram(
                svg=result.crop_svg,
                spans=page.text.spans,
                ir=result.ir,
                source_manifest_id=page.source_manifest_id,
            )
        except DiagramQualificationError as error:
            stages.append(writer.diagnostic("qualification", str(error)))
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        svg_stage = next(stage for stage in stages if stage.stage == "svg")
        view_stage = next(stage for stage in stages if stage.stage == "model_view")
        ir_stage = writer.save("qualified_ir", TypeAdapter(DiagramIR).dump_json(qualified.ir))
        description_stage = writer.save(
            "qualified_description", TypeAdapter(ObjectDescription).dump_json(qualified.description)
        )
        receipt = DiagramPublicationReceipt(
            object_id=item.object_id,
            source_manifest_id=page.source_manifest_id,
            ir=_ref(ir_stage),
            description=_ref(description_stage),
            source_svg=_ref(svg_stage),
            raw_ir=_ref(branch["ir"]),
            raw_description=_ref(branch["description"]),
            view=_ref(view_stage),
            qualification=qualified.qualification,
        )
        stages.extend(
            (
                ir_stage,
                description_stage,
                writer.save("qualification", receipt.model_dump_json().encode()),
            )
        )
        return ObjectProcessingRecord(
            item.object_id,
            item.kind,
            tuple(stages),
            len(qualified.ir.nodes) + len(qualified.ir.edges),
        )

    def _table(
        self,
        page: PageInput,
        item: LayoutObject,
        writer: _Writer,
        stages: list[StageOutcome],
        crop: bytes,
    ) -> ObjectProcessingRecord:
        source = self.sources.load(page.source_manifest_id)
        result = PdfspineTableAdapter().extract(
            source_pdf(self.sources, source), page=page, item=item
        )
        svg = writer.save("svg", crop, "image/svg+xml")
        stages.extend(
            (
                svg,
                writer.save(
                    "table_detection",
                    TypeAdapter(TableExtractionResult).dump_json(result),
                ),
            )
        )
        # ADR 0031: a model-inferred (pending) grid first, when the plan asks for one.
        if result.table is None and self.table_structure is not None:
            return self._table_inferred(
                page, item, writer, stages, svg, result, source_pdf(self.sources, source)
            )
        # ADR 0027: a Table whose grid pdfspine cannot detect is indexed as its verbatim
        # printed rows instead of being left out (``IngestPlan.unverified_tables_as_rows``).
        if result.table is None and self.plan.unverified_tables_as_rows:
            return self._table_rows(page, item, writer, stages, svg, result)
        if result.table is None:
            stages.extend(
                (
                    writer.diagnostic("ir", "; ".join(result.diagnostics)),
                    writer.diagnostic(
                        "description",
                        "A table description cannot be generated before source cell/row/header relations have qualified.",
                    ),
                    writer.diagnostic(
                        "qualification",
                        "Typed native topology is observed; financial row/header relationships remain unverified.",
                    ),
                )
            )
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        ir = writer.save("ir", TypeAdapter(TableIR).dump_json(result.table))
        stages.append(ir)
        proved_grid = result.table.verification is Verification.VERIFIED
        try:
            transcription = source_table_description(page, item, result.table)
        except ValueError as error:
            # The inferred grid is kept for review; only a verbatim transcription qualifies.
            stages.extend(
                (
                    writer.diagnostic(
                        "description",
                        "A table description is only its verbatim cell transcription; "
                        + str(error),
                    ),
                    writer.diagnostic(
                        "qualification",
                        "Typed native topology is observed but its literal transcription did "
                        "not verify; financial row/header relationships remain unverified. "
                        + f"{error} grid={'verified' if proved_grid else 'pending'}",
                    ),
                )
            )
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        description = writer.save(
            "description", TypeAdapter(ObjectDescription).dump_json(transcription)
        )
        qualification = LiteralQualification(
            item.object_id,
            transcription.source,
            page.source_manifest_id,
            transcription.source_span_ids,
            _ref(ir),
            _ref(description),
            _ref(svg),
            grid_scope=GRID_SCOPE if proved_grid else None,
            ruling_digest=(
                result.table.grid_evidence.ruling_digest
                if result.table.grid_evidence is not None
                else None
            ),
        )
        stages.extend(
            (
                description,
                writer.save(
                    "qualification", TypeAdapter(LiteralQualification).dump_json(qualification)
                ),
            )
        )
        return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))

    def _table_inferred(
        self,
        page: PageInput,
        item: LayoutObject,
        writer: _Writer,
        stages: list[StageOutcome],
        svg: StageOutcome,
        result: TableExtractionResult,
        pdf: bytes,
    ) -> ObjectProcessingRecord:
        """No grid was detected: let the structure model propose one, kept PENDING (ADR 0031).

        Every cell is the region's own spans; the grid qualifies under its own scope and its
        stages carry the model's producer. A grid failing its self-check (or a transcription
        that does not hold) falls back to ADR 0027's verbatim rows, with the reason recorded.
        """
        recognizer = self.table_structure
        if recognizer is None:
            raise ValueError("No table structure model is configured")
        tsr_writer = _Writer(
            writer.outputs, page, item, writer.producer + ":" + recognizer.producer
        )
        owned = set(item.source_span_ids)
        anchor = SourceAnchor(page.source_sha256, page.source_sha256, page.page_index, item.bbox)
        with opened_pdf(pdf) as document:
            inferred: TableIR | InferredGridRejection = pdfspine_tsr.infer_table_grid(
                document.load_page(page.page_index),
                object_id=item.object_id,
                anchor=anchor,
                spans=tuple(span for span in page.text.spans if span.span_id in owned),
                recognizer=recognizer,
            )
        transcription: ObjectDescription | None = None
        if isinstance(inferred, TableIR):
            try:
                transcription = source_table_description(page, item, inferred)
            except ValueError as error:
                inferred = InferredGridRejection("transcription", str(error))
        if isinstance(inferred, InferredGridRejection) or transcription is None:
            reason = (
                inferred
                if isinstance(inferred, InferredGridRejection)
                else InferredGridRejection("transcription", "no transcription")
            )
            stages.append(
                tsr_writer.diagnostic(
                    "table_structure", f"{TSR_FALLBACK}:{reason.reason}: {reason.detail}"
                )
            )
            return self._table_rows(page, item, writer, stages, svg, result)
        ir = tsr_writer.save("ir", TypeAdapter(TableIR).dump_json(inferred))
        description = tsr_writer.save(
            "description", TypeAdapter(ObjectDescription).dump_json(transcription)
        )
        qualification = LiteralQualification(
            item.object_id,
            transcription.source,
            page.source_manifest_id,
            transcription.source_span_ids,
            _ref(ir),
            _ref(description),
            _ref(svg),
            scope=TSR_SCOPE,
        )
        stages.extend(
            (
                ir,
                description,
                tsr_writer.save(
                    "qualification", TypeAdapter(LiteralQualification).dump_json(qualification)
                ),
            )
        )
        return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))

    def _table_rows(
        self,
        page: PageInput,
        item: LayoutObject,
        writer: _Writer,
        stages: list[StageOutcome],
        svg: StageOutcome,
        result: TableExtractionResult,
    ) -> ObjectProcessingRecord:
        """No grid was detected: transcribe the region's printed rows verbatim (ADR 0027).

        The rows are geometry alone — no column, header or merge is claimed — so they
        qualify under their own scope and producer, never as a table grid. The three stages
        carry their own producer, so every other stage keeps its bytes and fingerprint.
        """
        rows_writer = _Writer(
            writer.outputs, page, item, writer.producer + ":" + TABLE_ROWS_PRODUCER
        )
        owned = set(item.source_span_ids)
        anchor = SourceAnchor(page.source_sha256, page.source_sha256, page.page_index, item.bbox)
        try:
            ir = table_rows(
                item.object_id,
                anchor,
                tuple(span for span in page.text.spans if span.span_id in owned),
            )
            check_table_rows(ir, page.text.spans, anchor=item.bbox)
        except ValueError as error:
            stages.extend(
                (
                    writer.diagnostic("ir", "; ".join(result.diagnostics)),
                    writer.diagnostic(
                        "description", "No verbatim row transcription: " + str(error)
                    ),
                    writer.diagnostic(
                        "qualification",
                        "No grid was detected and the region's rows did not transcribe: "
                        + str(error),
                    ),
                )
            )
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        ir_stage = rows_writer.save("ir", TypeAdapter(TableRowsIR).dump_json(ir))
        description = ObjectDescription(
            item.object_id,
            anchor,
            ir.source_span_ids,
            rows_text(ir),
            TABLE_ROWS_PRODUCER,
            Confidence(None, TABLE_ROWS_METHOD),
            Verification.VERIFIED,
        )
        description_stage = rows_writer.save(
            "description", TypeAdapter(ObjectDescription).dump_json(description)
        )
        qualification = LiteralQualification(
            item.object_id,
            anchor,
            page.source_manifest_id,
            ir.source_span_ids,
            _ref(ir_stage),
            _ref(description_stage),
            _ref(svg),
            scope=TABLE_ROWS_SCOPE,
        )
        stages.extend(
            (
                ir_stage,
                description_stage,
                rows_writer.save(
                    "qualification", TypeAdapter(LiteralQualification).dump_json(qualification)
                ),
            )
        )
        return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))

    def _formula(
        self,
        page: PageInput,
        item: LayoutObject,
        writer: _Writer,
        stages: list[StageOutcome],
        model_ir: FormulaIR | None,
        model_description: ObjectDescription | None,
    ) -> ObjectProcessingRecord:
        """Prove the formula from the pinned PDF itself; the two model branches stay lineage.

        Like ``_table`` this runs under whatever ``qualification_policy`` was selected, and
        unlike ``_diagram`` it needs neither model branch: no model byte reaches the
        qualified products, so a budget-exhausted object still qualifies.
        """
        source = self.sources.load(page.source_manifest_id)
        try:
            result = qualify_formula(
                source_pdf(self.sources, source),
                page=page,
                item=item,
                model_ir=model_ir,
            )
        except ValueError as error:
            stages.extend(
                (
                    writer.diagnostic("formula_observation", str(error), failed=True),
                    writer.diagnostic(
                        "qualification",
                        "Formula source could not be re-observed: " + str(error),
                    ),
                )
            )
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        observation = writer.save(
            "formula_observation",
            TypeAdapter(FormulaSourceObservation).dump_json(result.observation),
        )
        stages.append(observation)
        if result.ir is None or result.description is None or result.ir.proof_level is None:
            stages.append(
                writer.diagnostic(
                    "qualification",
                    "Formula qualification withheld: " + "; ".join(result.diagnostics),
                )
            )
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        by_name = {stage.stage: stage for stage in stages}
        lineage = tuple(
            _ref(by_name[name])
            for name in ("ir", "description", "model_view")
            if name in by_name and by_name[name].state is StageState.SUCCEEDED
        )
        ir = writer.save("qualified_ir", TypeAdapter(FormulaIR).dump_json(result.ir))
        described = writer.save(
            "qualified_description", TypeAdapter(ObjectDescription).dump_json(result.description)
        )
        receipt = FormulaQualification(
            item.object_id,
            result.ir.source,
            page.source_manifest_id,
            result.ir.source_span_ids,
            _ref(ir),
            _ref(described),
            _ref(by_name["svg"]),
            _ref(observation),
            result.ir.proof_level,
            len(result.ir.tokens),
            len(result.ir.structures),
            tuple(token.index for token in result.ir.tokens if token.script_proof == "derived"),
            result.agreement,
            lineage,
        )
        stages.extend(
            (
                ir,
                described,
                writer.save(
                    "qualification",
                    FormulaPublicationReceipt(qualification=receipt).model_dump_json().encode(),
                ),
                writer.save(
                    "qualification_exclusions",
                    json.dumps(
                        {
                            "model_literal_agreement": result.agreement,
                            "model_description_diagnostics": list(
                                check_model_description(model_description, result.ir)
                            ),
                            "diagnostics": list(result.diagnostics),
                        }
                    ).encode(),
                ),
            )
        )
        return ObjectProcessingRecord(
            item.object_id, item.kind, tuple(stages), len(result.ir.tokens)
        )

    def _chart(
        self,
        page: PageInput,
        item: LayoutObject,
        native: bytes,
        writer: _Writer,
        stages: list[StageOutcome],
    ) -> ObjectProcessingRecord:
        try:
            prepared = prepare_figure(
                page=page,
                native_svg=native,
                bbox=item.bbox,
                region_id=item.extraction_region_id or item.object_id,
                context_span_ids=item.context_span_ids,
            )
        except ValueError as error:
            return self._unavailable(writer, stages, _error(error))
        svg = writer.save("svg", prepared.svg.svg.encode(), "image/svg+xml")
        view = writer.save(
            "model_view",
            TypeAdapter[object](type(prepared.model_view)).dump_json(prepared.model_view),
        )
        stages.extend((svg, view, writer.save("model_render", prepared.rendered.png, "image/png")))
        chart: ChartIR | None = None
        description: TextDescription | None = None
        chart_stage: StageOutcome | None = None
        description_stage: StageOutcome | None = None
        try:
            inferred_chart = self._chart_inference(prepared, writer, stages)
            chart = inferred_chart.chart
            chart_stage = writer.save("ir", TypeAdapter(ChartIR).dump_json(chart))
            stages.extend(
                (
                    chart_stage,
                    writer.save("ir_raw", inferred_chart.completion.json_text.encode()),
                    writer.save(
                        "ir_diagnostics",
                        json.dumps(
                            {
                                "diagnostics": [
                                    *inferred_chart.completion.parsed.diagnostics,
                                    *inferred_chart.diagnostics,
                                ],
                                "request_fingerprint": inferred_chart.completion.request_fingerprint,
                                "output_digest": inferred_chart.completion.output_digest,
                                **(
                                    {"correction_of": inferred_chart.correction_of}
                                    if inferred_chart.correction_of is not None
                                    else {}
                                ),
                            }
                        ).encode(),
                    ),
                )
            )
        except ValueError as error:
            self._binding_failure(writer, stages, "ir", error)
            stages.append(writer.diagnostic("ir", _error(error), failed=True))
        if self.plan.chart_description == "from-ir":
            description, description_stage = self._derived_description(chart, writer, stages)
        else:
            description, description_stage = self._model_description(prepared, writer, stages)
        qualified_count = 0
        if self.qualification_policy == "none":
            stages.append(
                writer.diagnostic(
                    "qualification",
                    "Raw inference run: independent qualification/admission has not run; both branches remain pending.",
                )
            )
        elif (
            chart is None or description is None or chart_stage is None or description_stage is None
        ):
            stages.append(
                writer.diagnostic(
                    "qualification",
                    "Both actual source-bound branches are required; no description-only fallback is admitted.",
                )
            )
        else:
            try:
                qualified_count = self._qualify(
                    prepared,
                    chart,
                    description,
                    writer,
                    stages,
                    chart_stage,
                    description_stage,
                    svg,
                    view,
                )
            except ValueError as error:
                stages.append(writer.diagnostic("qualification", _error(error)))
        return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages), qualified_count)

    def _model_description(
        self, prepared: PreparedFigure, writer: _Writer, stages: list[StageOutcome]
    ) -> tuple[TextDescription | None, StageOutcome | None]:
        """The description branch's own model call, its raw output and its diagnostics."""
        description: TextDescription | None = None
        description_stage: StageOutcome | None = None
        try:
            inferred_description = self._description(prepared, writer, stages)
            description = inferred_description.description
            stages.extend(
                (
                    writer.save(
                        "description_raw",
                        inferred_description.completion.json_text.encode(),
                    ),
                    writer.save(
                        "description_diagnostics",
                        json.dumps(
                            {
                                "diagnostics": list(inferred_description.diagnostics),
                                "request_fingerprint": inferred_description.completion.request_fingerprint,
                                "output_digest": inferred_description.completion.output_digest,
                                **(
                                    {"correction_of": inferred_description.correction_of}
                                    if inferred_description.correction_of is not None
                                    else {}
                                ),
                            }
                        ).encode(),
                    ),
                )
            )
            if description is None:
                stages.append(
                    writer.diagnostic(
                        "description",
                        "No source-bound description claims survived validation; inspect description_raw and description_diagnostics.",
                    )
                )
            else:
                description_stage = writer.save(
                    "description", TypeAdapter(TextDescription).dump_json(description)
                )
                stages.append(description_stage)
        except ValueError as error:
            self._binding_failure(writer, stages, "description", error)
            stages.append(writer.diagnostic("description", _error(error), failed=True))
        return description, description_stage

    def _derived_description(
        self, chart: ChartIR | None, writer: _Writer, stages: list[StageOutcome]
    ) -> tuple[TextDescription | None, StageOutcome | None]:
        """Lite (ADR 0025): the description read off the chart's own IR, no model call."""
        self.skipped_calls["chart_description"] += 1
        description = None if chart is None else describe_from_ir(chart)
        if description is None:
            stages.append(
                writer.diagnostic(
                    "description",
                    f"{SKIPPED_CODE}: the description derives from the chart IR, which "
                    + ("is unavailable" if chart is None else "prints no label field")
                    + "; no model description is requested in this ingest mode.",
                )
            )
            return None, None
        stage = writer.save("description", TypeAdapter(TextDescription).dump_json(description))
        stages.append(stage)
        return description, stage

    def _description(
        self, prepared: PreparedFigure, writer: _Writer, stages: list[StageOutcome]
    ) -> DescriptionInference:
        try:
            return ModelDescriptionGenerator(self.client, prepared).infer(prepared.svg)
        except ModelOutputBindingError as error:
            if error.request_fingerprint not in self.description_corrections:
                raise
            self._binding_failure(writer, stages, "description_original", error)
            return ModelDescriptionGenerator(
                self.client, prepared, correction_of=error.request_fingerprint
            ).infer(prepared.svg)

    def _chart_inference(
        self, prepared: PreparedFigure, writer: _Writer, stages: list[StageOutcome]
    ) -> ChartInference:
        try:
            return ModelChartExtractor(self.client, prepared).infer(prepared.svg)
        except ModelOutputBindingError as error:
            if error.request_fingerprint not in self.chart_corrections:
                raise
            self._binding_failure(writer, stages, "ir_original", error)
            return ModelChartExtractor(
                self.client, prepared, correction_of=error.request_fingerprint
            ).infer(prepared.svg)

    @staticmethod
    def _binding_failure(
        writer: _Writer, stages: list[StageOutcome], name: str, error: ValueError
    ) -> None:
        if not isinstance(error, ModelOutputBindingError):
            return
        stages.extend(
            (
                writer.save(name + "_raw", error.json_text.encode()),
                writer.save(
                    name + "_diagnostics",
                    json.dumps(
                        {
                            "diagnostics": [
                                "model_svg_binding_mismatch; raw output is rejected without rewriting its source digest"
                            ],
                            "request_fingerprint": error.request_fingerprint,
                            "output_digest": error.output_digest,
                        }
                    ).encode(),
                ),
            )
        )

    @staticmethod
    def _unavailable(
        writer: _Writer, stages: list[StageOutcome], reason: str
    ) -> ObjectProcessingRecord:
        crop = next(stage for stage in stages if stage.stage == "native_crop")
        stages.extend(
            (
                writer.save("svg", writer.outputs.assets.get(_ref(crop)), "image/svg+xml"),
                writer.diagnostic("ir", reason, failed=True),
                writer.diagnostic(
                    "description", "The source view could not be prepared: " + reason
                ),
                writer.diagnostic(
                    "qualification",
                    "No complete source view and paired inference; no fallback",
                ),
            )
        )
        return ObjectProcessingRecord(writer.item.object_id, writer.item.kind, tuple(stages))

    def _qualify(
        self,
        prepared: PreparedFigure,
        chart: ChartIR,
        description: TextDescription,
        writer: _Writer,
        stages: list[StageOutcome],
        chart_stage: StageOutcome,
        description_stage: StageOutcome,
        svg: StageOutcome,
        view: StageOutcome,
    ) -> int:
        if self.qualification_policy == "source-labels-only":
            label_pair = qualify_source_labels(prepared.svg, chart, description)
            projected_chart = label_pair.chart
            projected_description = label_pair.description
            qualification = label_pair.receipt
            excluded = label_pair.excluded_claim_paths
        elif self.qualification_policy == "donut":
            pair = DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)
            projected_chart = pair.chart
            projected_description = pair.description
            qualification = pair.receipt
            excluded = pair.excluded_fields
        else:
            raise ValueError("No qualification policy selected")
        ir = writer.save("qualified_ir", TypeAdapter(ChartIR).dump_json(projected_chart))
        desc = writer.save(
            "qualified_description",
            TypeAdapter(TextDescription).dump_json(projected_description),
        )
        receipt = ChartPublicationReceipt(
            object_id=writer.item.object_id,
            source_manifest_id=writer.page.source_manifest_id,
            region_id=writer.item.extraction_region_id or writer.item.object_id,
            ir=_ref(ir),
            description=_ref(desc),
            source_svg=_ref(svg),
            raw_chart=_ref(chart_stage),
            raw_description=_ref(description_stage),
            view=_ref(view),
            qualification=qualification,
        )
        stages.extend(
            (
                ir,
                desc,
                writer.save("qualification", receipt.model_dump_json().encode()),
                writer.save(
                    "qualification_exclusions",
                    json.dumps(
                        {
                            "raw_chart_id": chart.artifact_id,
                            "raw_description_id": description.artifact_id,
                            "policy": self.qualification_policy,
                            "excluded_fields": list(excluded),
                        }
                    ).encode(),
                ),
            )
        )
        # One qualified number shows up once per projection: the donut policy carries it as a
        # numeric description claim, the verbatim-points policy as an explicit chart point.
        # Take the richer view rather than adding them, which would count the same fact twice.
        return max(
            sum(claim.value is not None for claim in projected_description.claims),
            sum(point.value.value is not None for point in projected_chart.points),
        )
