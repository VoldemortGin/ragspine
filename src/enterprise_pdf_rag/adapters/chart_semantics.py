"""Two independent inferences from one immutable SVG-derived model view."""

import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from enterprise_pdf_rag.adapters.chart_semantic_schemas import (
    ChartObservationsDTO,
    EvidenceDTO,
    FigureDescriptionDTO,
    NumericDTO,
    TextFieldDTO,
)
from enterprise_pdf_rag.adapters.description_mapping import map_description
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure
from enterprise_pdf_rag.figures.models import (
    ChartAxis,
    ChartIR,
    ChartMark,
    ChartPoint,
    Confidence,
    Evidence,
    ExecutionMode,
    NumericObservation,
    SvgArtifact,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
)
from enterprise_pdf_rag.figures.validation import evidence_elements, validate_svg
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
    JsonCompletionResult,
)


@dataclass(frozen=True, slots=True)
class ChartInference:
    chart: ChartIR
    completion: JsonCompletionResult[ChartObservationsDTO]
    view_id: str
    diagnostics: tuple[str, ...] = ()
    correction_of: str | None = None


@dataclass(frozen=True, slots=True)
class DescriptionInference:
    description: TextDescription | None
    completion: JsonCompletionResult[FigureDescriptionDTO]
    view_id: str
    diagnostics: tuple[str, ...] = ()
    correction_of: str | None = None


class ModelOutputBindingError(JsonCompletionError):
    """Retain rejected raw output for review without adopting its claimed source."""

    def __init__(
        self,
        completion: JsonCompletionResult[ChartObservationsDTO]
        | JsonCompletionResult[FigureDescriptionDTO],
        *,
        correction_of: str | None = None,
    ) -> None:
        super().__init__(
            "model_svg_binding_mismatch",
            completion.request_fingerprint,
            diagnostics=completion.diagnostics,
        )
        self.json_text = completion.json_text
        self.output_digest = completion.output_digest
        self.correction_of = correction_of


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise JsonCompletionError("invalid_decimal_observation") from None
    if not result.is_finite():
        raise JsonCompletionError("nonfinite_decimal_observation")
    return result


def _evidence(svg: SvgArtifact, dto: EvidenceDTO, field_path: str) -> Evidence:
    if len(set(dto.element_ids)) != len(dto.element_ids):
        raise JsonCompletionError(f"{field_path}:duplicate_source_evidence")
    available = {element.element_id for element in svg.elements}
    if any(element_id not in available for element_id in dto.element_ids):
        raise JsonCompletionError(f"{field_path}:missing_source_evidence")
    ordinal = dto.confidence if dto.confidence in {"high", "medium", "low", "unknown"} else None
    score = None
    if ordinal is None:
        try:
            candidate = _decimal(dto.confidence)
        except JsonCompletionError:
            candidate = None
        if candidate is not None and Decimal(0) <= candidate <= Decimal(1):
            score = candidate
    confidence = (
        Confidence(
            None,
            f"model-declared ordinal={ordinal}; uncalibrated, not a probability or verification",
        )
        if ordinal is not None
        else Confidence(
            score,
            "model-self-assessment; not calibrated or independently verified"
            if score is not None or dto.confidence is None
            else f"model-declared label={dto.confidence!r}; uncalibrated, not a probability or verification",
        )
    )
    evidence = Evidence(
        dto.element_ids,
        Verification.PENDING,
        confidence,
    )
    evidence_elements(svg, evidence)
    return evidence


def _field(svg: SvgArtifact, dto: TextFieldDTO, field_path: str) -> TextField:
    evidence = _evidence(svg, dto.evidence, field_path)
    if not dto.text.strip():
        raise JsonCompletionError(f"{field_path}:text_unavailable")
    return TextField(dto.text, evidence)


def _lexical_decimal(literal: str, unit: str) -> Decimal | None:
    match = re.fullmatch(
        r"(?P<prefix>[$€£¥]?)(?P<number>[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)(?P<suffix>[%kKmMbB]?)",
        literal,
    )
    if match is None:
        return None
    literal_unit = match["prefix"] + match["suffix"]
    if literal_unit and literal_unit != unit.strip():
        return None
    return Decimal(match["number"].replace(",", ""))


def _numeric(
    svg: SvgArtifact,
    dto: NumericDTO,
    field_path: str,
    diagnostics: list[str],
    unit: str,
) -> NumericObservation:
    evidence = _evidence(svg, dto.evidence, field_path)
    literals = (
        dto.value or "",
        *(element.text for element in evidence_elements(svg, evidence)),
    )
    if any(re.search(r"[<>≤≥]\s*[+-]?\d|\(\s*[+-]?\d", literal) for literal in literals):
        diagnostics.append(
            f"{field_path}:unsupported_comparison_or_accounting_literal; raw model literal retained"
        )
        return NumericObservation(None, ValueKind.UNAVAILABLE, evidence)
    try:
        number = _decimal(dto.value)
    except JsonCompletionError:
        number = _lexical_decimal(dto.value or "", unit)
        if number is not None:
            diagnostics.append(
                f"{field_path}:lexical_numeric_normalization; raw literal and declared unit retained"
            )
    if (number is None) != (dto.kind == "unavailable"):
        diagnostics.append(
            f"{field_path}:unsupported_numeric_literal_or_kind; raw model literal retained"
        )
        return NumericObservation(None, ValueKind.UNAVAILABLE, evidence)
    return NumericObservation(number, ValueKind(dto.kind), evidence)


def _optional_field(
    svg: SvgArtifact, dto: TextFieldDTO | None, field_path: str, diagnostics: list[str]
) -> TextField | None:
    if dto is None:
        return None
    try:
        return _field(svg, dto, field_path)
    except JsonCompletionError as error:
        diagnostics.append(error.code)
        return None


def map_chart(
    svg: SvgArtifact, dto: ChartObservationsDTO, *, producer: str
) -> tuple[ChartIR, tuple[str, ...]]:
    """Keep valid pending members and locate omissions in the immutable raw DTO."""
    if dto.svg_digest != svg.digest:
        raise JsonCompletionError("model_svg_binding_mismatch")
    validate_svg(svg)
    diagnostics: list[str] = []
    axes = []
    for axis in dto.axes:
        try:
            axes.append(
                ChartAxis(
                    axis.axis_id,
                    _field(svg, axis.label, f"axes.{axis.axis_id}.label"),
                    _field(svg, axis.unit, f"axes.{axis.axis_id}.unit"),
                    axis.scale,
                )
            )
        except JsonCompletionError as error:
            diagnostics.append(error.code)
    points = []
    point_ids = tuple(point.point_id for point in dto.points)
    for point in dto.points:
        path = f"points.{point.point_id}"
        if point_ids.count(point.point_id) != 1:
            diagnostics.append(f"{path}:duplicate_point_identity")
            continue
        try:
            series = _field(svg, point.series, f"{path}.series")
            category = _field(svg, point.category, f"{path}.category")
            unit = _field(svg, point.unit, f"{path}.unit")
            value = _numeric(svg, point.value, f"{path}.value", diagnostics, unit.text)
            points.append(ChartPoint(point.point_id, series, category, unit, value))
        except JsonCompletionError as error:
            diagnostics.append(error.code)
    marks = []
    mapped_ids = {point.point_id for point in points}
    for mark in dto.marks:
        path = f"marks.{mark.mark_id}"
        if any(point_id not in mapped_ids for point_id in mark.point_ids):
            diagnostics.append(f"{path}:unmapped_point_relation")
            continue
        try:
            marks.append(
                ChartMark(
                    mark.mark_id,
                    mark.kind,
                    mark.bbox,
                    mark.color,
                    mark.point_ids,
                    _evidence(svg, mark.evidence, path),
                )
            )
        except JsonCompletionError as error:
            diagnostics.append(error.code)
    chart = ChartIR(
        svg.binding,
        dto.grammar,
        tuple(axes),
        tuple(points),
        producer,
        Verification.PENDING,
        ExecutionMode.PRODUCTION,
        _optional_field(svg, dto.title, "title", diagnostics),
        _optional_field(svg, dto.period, "period", diagnostics),
        tuple(marks),
    )
    return chart, tuple(diagnostics)


def _prompt(prepared: PreparedFigure) -> str:
    payload: dict[str, object] = {
        "svg_digest": prepared.svg.digest,
        "view_id": prepared.model_view_id,
        "physical_page": prepared.svg.source.page_index + 1,
        "coordinate_frame": prepared.svg.source.coordinate_frame,
        "region_bbox": prepared.svg.source.bbox,
        "render_size": [prepared.rendered.width, prepared.rendered.height],
        "observations": [
            {
                "id": element.element_id,
                "text": element.text,
                "bbox": element.anchor.bbox,
                "evidence_kind": element.evidence_kind.value,
                "source_span_id": element.source_span_id,
                "unicode_range": element.text_range,
            }
            for element in prepared.svg.elements
        ],
        "excluded_partial_span_ids": prepared.excluded_span_ids,
    }
    if prepared.page_context:
        payload["page_context"] = [
            {"id": context.observation_id, **asdict(context)} for context in prepared.page_context
        ]
        payload["page_context_policy"] = (
            "Page-scope qualifiers are context, not crop-local marks or numeric occurrences. Applying a qualifier to a metric remains an unverified relation. Context IDs cannot be used as local field evidence; record required contextual relationships and unsupported financial normalization in diagnostics."
        )
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _check_view(svg: SvgArtifact, prepared: PreparedFigure) -> None:
    if (
        svg != prepared.svg
        or prepared.view.svg_digest != svg.digest
        or prepared.view.render_digest != prepared.rendered.digest
    ):
        raise JsonCompletionError("model_view_binding_mismatch")
    if any(
        (
            context.source.document_sha256,
            context.source.source_revision,
            context.source.page_index,
            context.source.coordinate_frame,
        )
        != (
            svg.source.document_sha256,
            svg.source.source_revision,
            svg.source.page_index,
            svg.source.coordinate_frame,
        )
        or context.source_span_id in {element.source_span_id for element in svg.elements}
        for context in prepared.page_context
    ):
        raise JsonCompletionError("page_context_source_binding_mismatch")
    validate_svg(svg)


def _correction_prompt[T: (ChartObservationsDTO, FigureDescriptionDTO)](
    client: JsonCompletionClient,
    prepared: PreparedFigure,
    *,
    task: str,
    prompt: str,
    response_model: type[T],
    max_output_tokens: int,
    correction_of: str | None,
) -> tuple[str, str]:
    if correction_of is None:
        return task, prompt
    prior = client.complete_json(
        task=task,
        prompt=prompt,
        image_png=prepared.rendered.png,
        response_model=response_model,
        max_output_tokens=max_output_tokens,
        cache_only=True,
        allow_failed_retry=False,
    )
    if prior.request_fingerprint != correction_of:
        raise JsonCompletionError("correction_reference_mismatch", prior.request_fingerprint)
    if prior.parsed.svg_digest == prepared.svg.digest:
        raise JsonCompletionError("correction_not_needed", prior.request_fingerprint)
    corrected_task = (
        "chart-svg-binding-correction-v1"
        if response_model is ChartObservationsDTO
        else "description-svg-binding-correction-v1"
    )
    prompt += "\n" + json.dumps(
        {
            "correction_of": correction_of,
            "correction_policy": "The previous response copied the source digest incorrectly. Re-read this same source view independently. Copy the exact expected_svg_digest below without truncation. Do not copy unsupported claims from the earlier response. All original evidence and schema constraints still apply.",
            "expected_svg_digest": prepared.svg.digest,
            "view_id": prepared.model_view_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return corrected_task, prompt


class ModelChartExtractor:
    """Preserve structured grammar hypotheses with every field still pending."""

    def __init__(
        self,
        client: JsonCompletionClient,
        prepared: PreparedFigure,
        *,
        correction_of: str | None = None,
    ) -> None:
        if correction_of is not None and re.fullmatch(r"[0-9a-f]{64}", correction_of) is None:
            raise ValueError("Correction requires an immutable original request fingerprint")
        self._client = client
        self._prepared = prepared
        self._correction_of = correction_of

    def extract(self, svg: SvgArtifact) -> ChartIR:
        return self.infer(svg).chart

    def infer(self, svg: SvgArtifact) -> ChartInference:
        _check_view(svg, self._prepared)
        prompt = (
            "Extract chart grammar observations from this SVG render and PDF source text observations independently. Return chart-observations-v1. Copy svg_digest exactly. All coordinates are original page points, not pixels. Text observation IDs are evidence, NOT native glyph mapping. Use explicit values only where numeric labels exist; do not estimate missing bar heights as exact. For a donut use the metric (e.g. VONB) as series, segment name as category, '%' as unit. Cite exact substring observations for numeric values and units when available. Include title, period, axes, and marks with point_ids/color/bbox where visible; these are hypotheses, never verified. Do not include neighboring clipped labels. Unknown grammar or missing relations require diagnostics, not invented values.\n"
            + _prompt(self._prepared)
        )
        task, prompt = _correction_prompt(
            self._client,
            self._prepared,
            task="chart-branch-v1",
            prompt=prompt,
            response_model=ChartObservationsDTO,
            max_output_tokens=4096,
            correction_of=self._correction_of,
        )
        result = self._client.complete_json(
            task=task,
            prompt=prompt,
            image_png=self._prepared.rendered.png,
            response_model=ChartObservationsDTO,
            max_output_tokens=4096,
            allow_failed_retry=self._correction_of is None,
            bound_svg_digest=svg.digest if self._correction_of is not None else None,
        )
        dto = result.parsed
        if dto.svg_digest != svg.digest:
            raise ModelOutputBindingError(result, correction_of=self._correction_of)
        chart, mapping_diagnostics = map_chart(
            svg,
            dto,
            producer=f"model-chart-v1:{self._client.fingerprint}:{result.request_fingerprint}:{result.output_digest}",
        )
        return ChartInference(
            chart,
            result,
            self._prepared.model_view_id,
            mapping_diagnostics,
            self._correction_of,
        )


class ModelDescriptionGenerator:
    """Read the same source view directly; no ChartIR is accepted as an input."""

    def __init__(
        self,
        client: JsonCompletionClient,
        prepared: PreparedFigure,
        *,
        correction_of: str | None = None,
    ) -> None:
        if correction_of is not None and re.fullmatch(r"[0-9a-f]{64}", correction_of) is None:
            raise ValueError("Correction requires an immutable original request fingerprint")
        self._client = client
        self._prepared = prepared
        self._correction_of = correction_of

    def generate(self, svg: SvgArtifact) -> TextDescription:
        inferred = self.infer(svg)
        if inferred.description is None:
            raise JsonCompletionError(
                "description_unavailable", inferred.completion.request_fingerprint
            )
        return inferred.description

    def infer(self, svg: SvgArtifact) -> DescriptionInference:
        _check_view(svg, self._prepared)
        prompt = (
            "Describe this chart directly from the SVG render and its PDF source observations. You have no structured chart extraction as input. Return figure-description-v1 and copy svg_digest exactly. A numeric claim must separately name series(metric), category, unit, value(decimal string), and period if present. Use one plain factual sentence per explicit value in this exact conservative form: 'During {period}, {series} for {category}: {value} {unit}.' Omit 'During {period}, ' when no period is visible. For donuts the metric such as VONB is series, segment such as Agency is category, and unit is %. Cite exact IDs for every named field, number and period; prefer numeric/unit substring IDs over the combined percent label. Include no trends, estimates, assertions of verification, or unsupported comparisons. Non-numeric claims may only transcribe an exact label. Missing values/relations must appear in diagnostics; do not make up a claim.\n"
            + _prompt(self._prepared)
        )
        task, prompt = _correction_prompt(
            self._client,
            self._prepared,
            task="description-branch-v1",
            prompt=prompt,
            response_model=FigureDescriptionDTO,
            max_output_tokens=3000,
            correction_of=self._correction_of,
        )
        result = self._client.complete_json(
            task=task,
            prompt=prompt,
            image_png=self._prepared.rendered.png,
            response_model=FigureDescriptionDTO,
            max_output_tokens=3000,
            allow_failed_retry=self._correction_of is None,
        )
        dto = result.parsed
        if dto.svg_digest != svg.digest:
            raise ModelOutputBindingError(result, correction_of=self._correction_of)
        mapped = map_description(
            svg,
            dto,
            producer=f"model-description-v1:{self._client.fingerprint}:{result.request_fingerprint}:{result.output_digest}",
        )
        return DescriptionInference(
            mapped.description,
            result,
            self._prepared.model_view_id,
            (*dto.diagnostics, *mapped.diagnostics),
            self._correction_of,
        )
