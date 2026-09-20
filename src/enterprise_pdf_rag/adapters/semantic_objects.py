"""Persist independently inferred object branches and separately scoped qualifications."""

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.chart_publication import ChartPublicationReceipt
from enterprise_pdf_rag.adapters.chart_semantics import (
    ChartInference,
    DescriptionInference,
    ModelChartExtractor,
    ModelDescriptionGenerator,
    ModelOutputBindingError,
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
from enterprise_pdf_rag.adapters.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
)
from enterprise_pdf_rag.adapters.object_processing import ProcessingObjectAdapter
from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg
from enterprise_pdf_rag.adapters.pdfspine_tables import PdfspineTableAdapter
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.source_objects import source_table_description
from enterprise_pdf_rag.adapters.visual_semantics import VisualInference, VisualSemanticAdapter
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar
from enterprise_pdf_rag.figures.models import ChartIR, TextDescription
from enterprise_pdf_rag.processing.models import (
    LayoutObject,
    ObjectKind,
    ObjectProcessingRecord,
    PageInput,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.table_models import TableExtractionResult, TableIR
from enterprise_pdf_rag.processing.typed_ir import (
    DiagramIR,
    LiteralQualification,
    ObjectDescription,
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

    def diagnostic(self, stage: str, reason: str, *, failed: bool = False) -> StageOutcome:
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
            StageState.FAILED if failed else StageState.UNAVAILABLE,
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
    ) -> None:
        self.sources = sources
        self.outputs = outputs
        self.client = client
        self.qualification_policy = qualification_policy
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
            "semantic-object-v2:" + self.client.fingerprint + ":" + self.qualification_policy,
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
        try:
            result = VisualSemanticAdapter(self.client).infer(
                page=page, item=item, native_svg=native
            )
        except ValueError as error:
            return self._unavailable(writer, stages, _error(error))
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
            outcome = (
                writer.diagnostic(
                    name,
                    diagnostic or "No source-bound result was returned",
                    failed=True,
                )
                if value is None
                else writer.save(name, TypeAdapter[object](type(value)).dump_json(value))
            )
            stages.append(outcome)
            branch[name] = outcome
        if item.kind is not ObjectKind.DIAGRAM:
            stages.append(
                writer.diagnostic(
                    "qualification",
                    "Visual semantics are source-bound model inferences; an independent field/relationship verifier is not available for this object.",
                )
            )
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        return self._diagram(page, item, result, writer, stages, branch)

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
            self.sources.get(source.manifest.source), page=page, item=item
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
                        + str(error),
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
        return sum(claim.value is not None for claim in projected_description.claims)
