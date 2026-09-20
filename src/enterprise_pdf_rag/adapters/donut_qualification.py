"""Independent source occurrences and geometry qualify a restricted share view."""

import re
from dataclasses import dataclass, replace
from decimal import Decimal
from hashlib import sha256
from math import hypot

from enterprise_pdf_rag.adapters.donut_geometry import (
    NativeSector,
    contains,
    native_donut_geometry,
)
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure
from enterprise_pdf_rag.adapters.source_paint import SourcePaintProof
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    ChartPoint,
    Evidence,
    ExecutionMode,
    FailureCode,
    FieldOccurrence,
    FigureError,
    FigureQualification,
    QualifiedFigurePair,
    SvgArtifact,
    SvgElement,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
)
from enterprise_pdf_rag.figures.validation import evidence_elements, validate_svg


def _fail(reason: str) -> FigureError:
    return FigureError(FailureCode.UNVERIFIED, reason)


def _center(element: SvgElement) -> tuple[float, float]:
    x0, y0, x1, y1 = element.anchor.bbox
    return ((x0 + x1) / 2, (y0 + y1) / 2)


def _verified(evidence: Evidence, *elements: SvgElement) -> Evidence:
    if evidence.verification is Verification.REJECTED:
        raise _fail("rejected_prior_evidence_cannot_be_qualified")
    return replace(
        evidence,
        verification=Verification.VERIFIED,
        element_ids=tuple(element.element_id for element in elements),
    )


@dataclass(frozen=True, slots=True)
class _SourcePoint:
    category: SvgElement
    literal: SvgElement
    number: SvgElement
    unit: SvgElement
    value: Decimal


def _full_spans(svg: SvgArtifact) -> tuple[SvgElement, ...]:
    longest: dict[str, SvgElement] = {}
    for element in svg.elements:
        if element.source_span_id is None:
            raise _fail("source_span_occurrences_required")
        current = longest.get(element.source_span_id)
        if current is None or len(current.text) < len(element.text):
            longest[element.source_span_id] = element
    return tuple(longest.values())


def _aligned_external(
    category: SvgElement,
    value: SvgElement,
    outer: tuple[float, float, float, float],
) -> bool:
    c = category.anchor.bbox
    v = value.anchor.bbox
    overlap = min(c[3], v[3]) - max(c[1], v[1])
    if overlap < 0.5 * min(c[3] - c[1], v[3] - v[1]):
        return False
    return (c[2] < outer[0] and c[2] < v[0]) or (c[0] > outer[2] and c[0] > v[2])


def _corridor(
    category: SvgElement,
    value: SvgElement,
    spans: tuple[SvgElement, ...],
    outer: tuple[float, float, float, float],
) -> bool:
    if not _aligned_external(category, value, outer):
        return False
    c, v = category.anchor.bbox, value.anchor.bbox
    if c[2] < outer[0] and c[2] < v[0]:
        x0, x1 = c[2], v[0]
    elif c[0] > outer[2] and c[0] > v[2]:
        x0, x1 = v[2], c[0]
    else:
        return False
    y0, y1 = max(c[1], v[1]), min(c[3], v[3])
    return not any(
        element.source_span_id not in {category.source_span_id, value.source_span_id}
        and max(element.anchor.bbox[0], x0) < min(element.anchor.bbox[2], x1)
        and max(element.anchor.bbox[1], y0) < min(element.anchor.bbox[3], y1)
        for element in spans
    )


class DonutQualification:
    """Trust saved source observations, never model marks, values or status flags."""

    def __init__(
        self,
        prepared: PreparedFigure,
        *,
        source_paint: "SourcePaintProof | None" = None,
    ) -> None:
        self._prepared = prepared
        self._source_paint = source_paint

    def _source(
        self, svg: SvgArtifact
    ) -> tuple[
        SvgElement,
        SvgElement,
        SvgElement,
        tuple[_SourcePoint, ...],
        tuple[NativeSector, ...],
        tuple[str, ...],
    ]:
        if svg.verification is Verification.REJECTED:
            raise _fail("rejected_source_cannot_be_qualified")
        if self._prepared.page_context:
            raise _fail("page_context_semantics_not_qualified")
        if (
            svg != self._prepared.svg
            or svg.source.rotation != 0
            or svg.source.coordinate_frame != "page-top-left-points"
            or svg.source.transform != (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            or self._prepared.view.svg_digest != svg.digest
            or self._prepared.view.source_sha256 != svg.source.document_sha256
            or self._prepared.view.page_index != svg.source.page_index
            or self._prepared.view.bbox != svg.source.bbox
            or self._prepared.view.crop_svg_digest
            != sha256(self._prepared.crop_svg.encode()).hexdigest()
            or self._prepared.view.render_digest != self._prepared.rendered.digest
            or self._prepared.view.renderer_fingerprint
            != self._prepared.rendered.renderer_fingerprint
            or not svg.svg.startswith(self._prepared.crop_svg[:-6] + "<metadata>")
        ):
            raise _fail("source_view_binding_or_coordinate_frame_mismatch")
        validate_svg(svg)
        proof = self._source_paint
        if proof is not None and (
            proof.source_sha256 != svg.source.document_sha256
            or proof.source_revision != svg.source.source_revision
            or proof.page_index != svg.source.page_index
            or proof.native_svg_digest != self._prepared.view.native_svg_digest
            or proof.crop_svg_digest != self._prepared.view.crop_svg_digest
            or proof.bbox != svg.source.bbox
            or proof.source_text_digest
            != sha256(repr(self._prepared.paint_text_spans).encode()).hexdigest()
            or proof.coverage != "complete_source_paint"
        ):
            raise _fail("source_paint_proof_binding_mismatch")
        try:
            geometry = native_donut_geometry(
                self._prepared.crop_svg,
                svg.source.bbox,
                self._prepared.view.native_svg_digest,
                text_spans=self._prepared.paint_text_spans,
                excluded_span_ids=self._prepared.excluded_span_ids,
                proven_glyph_refs=tuple(glyph.native_path_ref for glyph in proof.glyphs)
                if proof
                else (),
                transparent_refs=tuple(
                    vector.native_path_ref
                    for vector in proof.vectors
                    if vector.role == "transparent"
                )
                if proof
                else (),
                source_proof_id=proof.proof_id if proof else None,
            )
        except ValueError as error:
            raise _fail(str(error)) from None
        sectors = geometry.sectors
        outer = (
            min(s.bbox[0] for s in sectors),
            min(s.bbox[1] for s in sectors),
            max(s.bbox[2] for s in sectors),
            max(s.bbox[3] for s in sectors),
        )
        center = ((outer[0] + outer[2]) / 2, (outer[1] + outer[3]) / 2)
        inner = min(
            hypot(p[0] - center[0], p[1] - center[1]) for sector in sectors for p in sector.points
        )
        spans = _full_spans(svg)
        central = tuple(
            e
            for e in spans
            if all(
                hypot(x - center[0], y - center[1]) < inner - 1
                for x, y in (
                    (e.anchor.bbox[0], e.anchor.bbox[1]),
                    (e.anchor.bbox[2], e.anchor.bbox[3]),
                )
            )
        )
        periods = tuple(e for e in central if re.fullmatch(r"[12]H[0-9]{2}", e.text))
        metrics = tuple(e for e in central if e not in periods)
        titles = tuple(
            e for e in spans if e.text == "Distribution Mix" and e.anchor.bbox[3] < outer[1]
        )
        if len(periods) != 1 or len(metrics) != 1 or len(titles) != 1:
            raise _fail("distribution_title_metric_or_period_scope_ambiguous")
        literals = tuple(e for e in spans if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%", e.text))
        if len(literals) != 2:
            raise _fail("two_explicit_percent_labels_required")
        external_categories = tuple(
            element
            for element in spans
            if element not in (*literals, *central, *titles)
            and any(_aligned_external(element, value, outer) for value in literals)
        )
        if len(external_categories) != 2:
            raise _fail("category_to_numeric_label_relation_ambiguous")
        values = []
        used_sectors = []
        used_categories = []
        for literal in literals:
            box = literal.anchor.bbox
            candidates = tuple(
                sector
                for sector in sectors
                if all(
                    contains(sector.points, p, margin=1.0)
                    for p in (
                        _center(literal),
                        (box[0], box[1]),
                        (box[2], box[1]),
                        (box[0], box[3]),
                        (box[2], box[3]),
                    )
                )
            )
            if len(candidates) != 1:
                raise _fail("numeric_label_not_safely_inside_one_native_sector")
            categories = tuple(
                e for e in external_categories if _corridor(e, literal, spans, outer)
            )
            if len(categories) != 1:
                raise _fail("category_to_numeric_label_relation_ambiguous")
            fragments = tuple(e for e in svg.elements if e.source_span_id == literal.source_span_id)
            number = next((e for e in fragments if e.text == literal.text[:-1]), None)
            unit = next((e for e in fragments if e.text == "%"), None)
            if number is None or unit is None:
                raise _fail("numeric_literal_substring_provenance_missing")
            values.append(_SourcePoint(categories[0], literal, number, unit, Decimal(number.text)))
            used_sectors.append(candidates[0].source_ref)
            used_categories.append(categories[0].source_span_id)
        if len(set(used_sectors)) != 2 or len(set(used_categories)) != 2:
            raise _fail("source_sector_label_mapping_not_one_to_one")
        if proof is not None:
            expected_spans = {
                element.source_span_id
                for element in (
                    *titles,
                    *metrics,
                    *periods,
                    *literals,
                    *external_categories,
                )
            }
            for glyph in proof.glyphs:
                if glyph.source_span_id in expected_spans:
                    if not (
                        svg.source.bbox[0]
                        <= glyph.bounds[0]
                        < glyph.bounds[2]
                        <= svg.source.bbox[2]
                        and svg.source.bbox[1]
                        <= glyph.bounds[1]
                        < glyph.bounds[3]
                        <= svg.source.bbox[3]
                    ):
                        raise _fail("qualified_field_glyph_crosses_crop_boundary")
                elif glyph.source_span_id not in self._prepared.excluded_span_ids:
                    raise _fail("unexplained_source_text_inside_chart")
                elif any(
                    max(glyph.bounds[0], box[0]) < min(glyph.bounds[2], box[2])
                    and max(glyph.bounds[1], box[1]) < min(glyph.bounds[3], box[3])
                    for box in (
                        outer,
                        *(
                            element.anchor.bbox
                            for element in (
                                *titles,
                                *metrics,
                                *periods,
                                *literals,
                                *external_categories,
                            )
                        ),
                    )
                ):
                    raise _fail("neighbor_text_overlaps_qualified_chart")
        return (
            titles[0],
            metrics[0],
            periods[0],
            tuple(values),
            sectors,
            geometry.review_refs,
        )

    @staticmethod
    def _field(svg: SvgArtifact, field: TextField, source: SvgElement) -> TextField:
        refs = evidence_elements(svg, field.evidence)
        if (
            field.text != source.text
            or any(e.source_span_id != source.source_span_id for e in refs)
            or not any(e.text == field.text for e in refs)
        ):
            raise _fail("field_source_occurrence_mismatch")
        return replace(field, evidence=_verified(field.evidence, source))

    def qualify_pair(
        self, svg: SvgArtifact, chart: ChartIR, description: TextDescription
    ) -> QualifiedFigurePair:
        if Verification.REJECTED in (chart.verification, description.verification):
            raise _fail("rejected_prior_branch_cannot_be_qualified")
        if (
            chart.binding != svg.binding
            or description.binding != svg.binding
            or chart.execution_mode is not ExecutionMode.PRODUCTION
            or description.execution_mode is not ExecutionMode.PRODUCTION
        ):
            raise _fail("branch_binding_or_execution_mode_mismatch")
        title, metric, period, points, sectors, paint_reviews = self._source(svg)
        if (
            chart.grammar != "donut"
            or chart.axes
            or chart.title is None
            or chart.period is None
            or len(chart.points) != 2
        ):
            raise _fail("scoped_donut_grammar_or_context_missing")
        qualified_points = []
        source_by_category = {p.category.text: p for p in points}
        for point in chart.points:
            source = source_by_category.get(point.category.text)
            if (
                source is None
                or point.value.kind is not ValueKind.EXPLICIT
                or point.value.value != source.value
                or point.value.value.as_tuple().exponent != source.value.as_tuple().exponent
            ):
                raise _fail("explicit_value_category_pair_or_precision_mismatch")
            references = evidence_elements(svg, point.value.evidence)
            if any(
                e.source_span_id != source.literal.source_span_id for e in references
            ) or not any(e.text in {source.literal.text, source.number.text} for e in references):
                raise _fail("numeric_source_occurrence_mismatch")
            qualified_points.append(
                ChartPoint(
                    point.point_id,
                    self._field(svg, point.series, metric),
                    self._field(svg, point.category, source.category),
                    self._field(svg, point.unit, source.unit),
                    replace(
                        point.value,
                        evidence=_verified(point.value.evidence, source.number),
                    ),
                )
            )
        if len({p.category.text for p in qualified_points}) != 2:
            raise _fail("chart_categories_not_one_to_one")
        qualified_chart = replace(
            chart,
            points=tuple(qualified_points),
            title=self._field(svg, chart.title, title),
            period=self._field(svg, chart.period, period),
            marks=(),
            verification=Verification.VERIFIED,
            producer=chart.producer
            if chart.producer.startswith("qualified-chart-v1:")
            else "qualified-chart-v1:" + chart.producer,
        )
        # This projection consumes ONLY the independent description's claims and
        # the independently reconstructed source relations, never ChartIR fields.
        claims = []
        covered = []
        for claim in description.claims:
            refs = evidence_elements(svg, claim.evidence)
            if claim.value is None:
                if claim.text != title.text or any(
                    e.source_span_id != title.source_span_id for e in refs
                ):
                    raise _fail("unqualified_nonnumeric_description_claim")
                claims.append(replace(claim, evidence=_verified(claim.evidence, title)))
                continue
            source = source_by_category.get(str(claim.category))
            if (
                source is None
                or (claim.series, claim.period, claim.unit, claim.value)
                != (metric.text, period.text, "%", source.value)
                or claim.value.as_tuple().exponent != source.value.as_tuple().exponent
            ):
                raise _fail("description_fields_disagree_with_source_relation")
            expected = (metric, source.category, source.number, source.unit, period)
            if {e.source_span_id for e in refs} != {e.source_span_id for e in expected}:
                raise _fail("description_source_occurrence_mismatch")
            raw = f"During {claim.period}, {claim.series} for {claim.category}: {claim.value} {claim.unit}."
            projected = f"During {claim.period}, {claim.series} distribution share for {claim.category}: {claim.value} {claim.unit}."
            if claim.text not in {raw, projected}:
                raise _fail("description_contains_unqualified_text")
            claims.append(
                replace(claim, text=projected, evidence=_verified(claim.evidence, *expected))
            )
            covered.append(claim.category)
        if len(covered) != 2 or set(covered) != set(source_by_category):
            raise _fail("description_share_claim_coverage_incomplete")
        qualified_description = replace(
            description,
            claims=tuple(claims),
            verification=Verification.VERIFIED,
            producer=description.producer
            if description.producer.startswith("qualified-description-v1:")
            else "qualified-description-v1:" + description.producer,
        )
        fields = [
            FieldOccurrence("title", (title.element_id,)),
            FieldOccurrence("period", (period.element_id,)),
        ]
        for point in qualified_points:
            fields.extend(
                FieldOccurrence(f"points.{point.point_id}.{name}", field.evidence.element_ids)
                for name, field in (
                    ("series", point.series),
                    ("category", point.category),
                    ("unit", point.unit),
                    ("value", point.value),
                )
            )
        receipt = FigureQualification(
            svg.binding,
            svg.source,
            tuple(fields),
            (
                "source-replay-font-evenodd-annular-direct-label-v3; "
                if self._source_paint
                else "native-MCLZ-annular-direct-label-v2; "
            )
            + "curve tolerance=0.025pt; radial tolerance=0.15pt; complete complementary annulus; label margin=1pt; clear horizontal corridor; no area-derived values",
            ExecutionMode.PRODUCTION,
            (
                f"native-svg:{self._prepared.view.native_svg_digest}",
                *(s.source_ref for s in sectors),
                *paint_reviews,
            ),
            "explicit-distribution-shares",
        )
        return QualifiedFigurePair(
            qualified_chart,
            qualified_description,
            receipt,
            chart.artifact_id,
            description.artifact_id,
            tuple(f"marks.{mark.mark_id}" for mark in chart.marks),
        )
