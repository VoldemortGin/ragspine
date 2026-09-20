"""Independent typed and natural-language branches for non-chart visuals."""

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html import escape

from enterprise_pdf_rag.adapters.figure_reasoning import render_svg_png
from enterprise_pdf_rag.adapters.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
    JsonCompletionResult,
)
from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg
from enterprise_pdf_rag.adapters.visual_semantic_schemas import (
    DiagramObservationsDTO,
    FormulaObservationsDTO,
    ImageObservationsDTO,
    VisualDescriptionDTO,
    VisualEvidenceDTO,
)
from enterprise_pdf_rag.documents.models import Bounds, TextSpan
from enterprise_pdf_rag.figures.models import (
    Confidence,
    EvidenceKind,
    SourceAnchor,
    SvgArtifact,
    SvgElement,
    Verification,
    content_id,
)
from enterprise_pdf_rag.processing.geometry import contains
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput
from enterprise_pdf_rag.processing.typed_ir import (
    DiagramEdge,
    DiagramIR,
    DiagramNode,
    FormulaIR,
    ImageIR,
    ObjectDescription,
    ObservedText,
)

type VisualIR = DiagramIR | ImageIR | FormulaIR
type VisualDTO = DiagramObservationsDTO | ImageObservationsDTO | FormulaObservationsDTO


@dataclass(frozen=True, slots=True)
class _PreparedVisual:
    svg: SvgArtifact
    crop_svg: bytes
    model_png: bytes
    model_view_json: bytes
    full_span_ids: tuple[str, ...]
    excluded_partial_span_ids: tuple[str, ...]
    unowned_full_span_ids: tuple[str, ...]
    renderer_fingerprint: str


@dataclass(frozen=True, slots=True)
class VisualInference:
    kind: ObjectKind
    source: SourceAnchor
    ir: VisualIR | None
    description: ObjectDescription | None
    crop_svg: bytes
    model_png: bytes
    model_view_json: bytes
    ir_raw_json: bytes | None
    description_raw_json: bytes | None
    ir_diagnostic: str | None
    description_diagnostic: str | None
    ir_confidence: Confidence | None
    producer: str
    description_index_eligible: bool = False


def _inside(inner: Bounds, outer: Bounds) -> bool:
    return contains(outer, inner)


def _overlaps(first: Bounds, second: Bounds) -> bool:
    return max(first[0], second[0]) < min(first[2], second[2]) and max(first[1], second[1]) < min(
        first[3], second[3]
    )


def _source_fragment(span: TextSpan) -> tuple[int, int] | None:
    start = len(span.text) - len(span.text.lstrip())
    end = len(span.text.rstrip())
    return None if start == end else (start, end)


def _confidence(value: str | None) -> Confidence:
    if value is None:
        return Confidence(None, "model did not declare confidence; uncalibrated")
    try:
        score = Decimal(value)
    except InvalidOperation:
        return Confidence(
            None,
            f"model-declared ordinal confidence={value}; uncalibrated and not independently verified",
        )
    if not score.is_finite() or not Decimal(0) <= score <= Decimal(1):
        return Confidence(
            None,
            f"model-declared confidence={value}; invalid numeric scale and not independently verified",
        )
    return Confidence(score, "model-self-assessment; uncalibrated and unverified")


def _prepare(*, page: PageInput, item: LayoutObject, native_svg: bytes) -> _PreparedVisual:
    if (
        sha256(native_svg).hexdigest() != page.native_svg.sha256
        or len(native_svg) != page.native_svg.byte_length
    ):
        raise ValueError("Saved native SVG does not match PageInput")
    if page.text.coordinate_frame != "page-top-left-points":
        raise ValueError("Unsupported source text coordinate frame")
    observed = {span.span_id: span for span in page.text.spans}
    if (
        len(set(item.source_span_ids)) != len(item.source_span_ids)
        or not set(item.source_span_ids) <= observed.keys()
    ):
        raise ValueError("Visual object references unknown source occurrences")
    if any(not _inside(observed[span_id].bbox, item.bbox) for span_id in item.source_span_ids):
        raise ValueError("Visual object owns a source occurrence outside its bbox")

    crop = crop_native_svg(
        native_svg.decode("utf-8"),
        width=page.width,
        height=page.height,
        bbox=item.bbox,
    )
    source = SourceAnchor(
        page.source_sha256,
        page.source_sha256,
        page.page_index,
        item.bbox,
        "page-top-left-points",
    )
    elements: list[SvgElement] = []
    metadata: list[str] = []
    for span_id in item.source_span_ids:
        span = observed[span_id]
        offsets = _source_fragment(span)
        if offsets is None:
            continue
        start, end = offsets
        element_id = "obs-" + sha256(f"{span.span_id}:{start}:{end}".encode()).hexdigest()[:16]
        text = span.text[start:end]
        anchor = SourceAnchor(
            source.source_revision,
            source.document_sha256,
            source.page_index,
            span.bbox,
            source.coordinate_frame,
        )
        elements.append(
            SvgElement(
                element_id,
                text,
                anchor,
                EvidenceKind.SOURCE_TEXT_OBSERVATION,
                span.span_id,
                (start, end),
            )
        )
        metadata.append(
            f'<observation xmlns="urn:enterprise-pdf-rag:source-observation-v1" id="{element_id}" source-span-id="{escape(span.span_id, quote=True)}" start="{start}" end="{end}">{escape(text)}</observation>'
        )
    structured = (
        crop[:-6] + "<metadata>" + "".join(metadata) + "</metadata></svg>" if metadata else crop
    )
    svg = SvgArtifact(
        content_id(
            "visual-source-v1",
            (page.source_sha256, page.page_index, item.bbox, item.object_id),
        ),
        source,
        structured,
        tuple(elements),
        warnings=(
            "Visual completeness and semantic relationships remain unverified",
            "Source observations are metadata, not native glyph correspondence",
        ),
    )
    rendered = render_svg_png(
        structured.encode(),
        width=min(960, max(1, round((item.bbox[2] - item.bbox[0]) * 2))),
    )
    all_full = tuple(span.span_id for span in page.text.spans if _inside(span.bbox, item.bbox))
    partial = tuple(
        span.span_id
        for span in page.text.spans
        if not _inside(span.bbox, item.bbox) and _overlaps(span.bbox, item.bbox)
    )
    unowned = tuple(span_id for span_id in all_full if span_id not in item.source_span_ids)
    view = {
        "schema_version": "visual-model-view-v1",
        "object_id": item.object_id,
        "kind": item.kind.value,
        "source_manifest_id": page.source_manifest_id,
        "source_sha256": page.source_sha256,
        "page_index": page.page_index,
        "bbox": item.bbox,
        "native_svg_digest": page.native_svg.sha256,
        "crop_svg_digest": sha256(crop.encode()).hexdigest(),
        "structured_svg_digest": svg.digest,
        "render_digest": rendered.digest,
        "renderer_fingerprint": rendered.renderer_fingerprint,
        "full_span_ids": item.source_span_ids,
        "excluded_partial_span_ids": partial,
        "unowned_full_span_ids": unowned,
    }
    return _PreparedVisual(
        svg,
        crop.encode(),
        rendered.png,
        json.dumps(view, sort_keys=True, separators=(",", ":")).encode(),
        item.source_span_ids,
        partial,
        unowned,
        rendered.renderer_fingerprint,
    )


def _prompt(prepared: _PreparedVisual) -> str:
    return json.dumps(
        {
            "svg_digest": prepared.svg.digest,
            "physical_page": prepared.svg.source.page_index + 1,
            "coordinate_frame": prepared.svg.source.coordinate_frame,
            "region_bbox": prepared.svg.source.bbox,
            "observations": [
                {
                    "id": element.element_id,
                    "text": element.text,
                    "bbox": element.anchor.bbox,
                    "source_span_id": element.source_span_id,
                    "unicode_range": element.text_range,
                }
                for element in prepared.svg.elements
            ],
            "excluded_partial_span_ids": prepared.excluded_partial_span_ids,
            "unowned_full_span_ids": prepared.unowned_full_span_ids,
            "render_digest": sha256(prepared.model_png).hexdigest(),
            "renderer_fingerprint": prepared.renderer_fingerprint,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _elements(prepared: _PreparedVisual, evidence: VisualEvidenceDTO) -> tuple[SvgElement, ...]:
    if len(evidence.element_ids) != len(set(evidence.element_ids)):
        raise JsonCompletionError("duplicate_visual_evidence")
    lookup = {element.element_id: element for element in prepared.svg.elements}
    if any(element_id not in lookup for element_id in evidence.element_ids):
        raise JsonCompletionError("unbound_visual_evidence")
    elements = tuple(lookup[element_id] for element_id in evidence.element_ids)
    if any(element.source_span_id not in prepared.full_span_ids for element in elements):
        raise JsonCompletionError("visual_evidence_outside_object")
    return elements


def _source_ids(elements: tuple[SvgElement, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            element.source_span_id for element in elements if element.source_span_id is not None
        )
    )


def _observed(elements: tuple[SvgElement, ...]) -> tuple[ObservedText, ...]:
    return tuple(
        ObservedText(element.source_span_id or "", element.text, element.anchor)
        for element in elements
    )


def _normal(value: str) -> str:
    return " ".join(value.split())


def _label_matches(label: str | None, elements: tuple[SvgElement, ...]) -> bool:
    if label is None:
        return not elements
    observed = tuple(_normal(element.text) for element in elements)
    return bool(elements) and _normal(label) in (*observed, " ".join(observed))


class VisualSemanticAdapter:
    """Run two bounded branches over one immutable SVG-derived view."""

    def __init__(self, client: JsonCompletionClient) -> None:
        self._client = client
        self.fingerprint = "model-visual-semantics-v1:" + client.fingerprint

    def infer(self, *, page: PageInput, item: LayoutObject, native_svg: bytes) -> VisualInference:
        if item.kind not in (
            ObjectKind.IMAGE,
            ObjectKind.DIAGRAM,
            ObjectKind.FORMULA,
        ):
            raise ValueError("Visual semantics only accepts Image, Diagram or Formula")
        prepared = _prepare(page=page, item=item, native_svg=native_svg)
        ir, ir_raw, ir_diagnostic, confidence = self._infer_ir(prepared=prepared, item=item)
        description, description_raw, description_diagnostic = self._infer_description(
            prepared=prepared, item=item
        )
        return VisualInference(
            item.kind,
            prepared.svg.source,
            ir,
            description,
            prepared.crop_svg,
            prepared.model_png,
            prepared.model_view_json,
            ir_raw,
            description_raw,
            ir_diagnostic,
            description_diagnostic,
            confidence,
            self.fingerprint,
        )

    def _infer_ir(
        self, *, prepared: _PreparedVisual, item: LayoutObject
    ) -> tuple[VisualIR | None, bytes | None, str | None, Confidence | None]:
        instructions = {
            ObjectKind.IMAGE: (
                "Return image-observations-v1. Record only visible objects and exact observed label IDs. Visual objects without readable labels may use empty evidence. Do not identify people, infer business meaning, or claim verification."
            ),
            ObjectKind.DIAGRAM: (
                "Return diagram-observations-v1. Nodes need tight original-page bboxes. Labels must copy cited observations exactly; use null with empty evidence when unavailable. Edges are visual hypotheses between returned node IDs and stay unverified."
            ),
            ObjectKind.FORMULA: (
                "Return formula-observations-v1. Cite ordered exact source observation IDs for the original literal. A normalized LaTeX form is inferred or unavailable; never reconstruct an unreadable source expression."
            ),
        }[item.kind]
        models: dict[ObjectKind, type[VisualDTO]] = {
            ObjectKind.IMAGE: ImageObservationsDTO,
            ObjectKind.DIAGRAM: DiagramObservationsDTO,
            ObjectKind.FORMULA: FormulaObservationsDTO,
        }
        try:
            result = self._client.complete_json(
                task=f"visual-ir-{item.kind.value.lower()}-v1",
                prompt=(
                    "Analyze this exact SVG-derived visual region independently of any natural-language description. "
                    "Copy svg_digest exactly. Source observation IDs are text evidence, not native glyph mapping. "
                    "Confidence is model-declared and cannot verify a claim. "
                    + instructions
                    + "\n"
                    + _prompt(prepared)
                ),
                image_png=prepared.model_png,
                response_model=models[item.kind],
                max_output_tokens=3000,
            )
        except JsonCompletionError as error:
            return None, None, error.code, None
        raw = result.json_text.encode()
        try:
            if result.parsed.svg_digest != prepared.svg.digest:
                raise JsonCompletionError("model_svg_binding_mismatch")
            confidence = _confidence(result.parsed.confidence)
            ir = self._map_ir(prepared=prepared, item=item, result=result)
        except (JsonCompletionError, ValueError) as error:
            code = error.code if isinstance(error, JsonCompletionError) else "invalid_visual_ir"
            return None, raw, code, None
        return ir, raw, None, confidence

    def _map_ir(
        self,
        *,
        prepared: _PreparedVisual,
        item: LayoutObject,
        result: JsonCompletionResult[VisualDTO],
    ) -> VisualIR:
        dto = result.parsed
        if isinstance(dto, ImageObservationsDTO):
            for visible in dto.visible_objects:
                _elements(prepared, visible.evidence)
            label_evidence = VisualEvidenceDTO(
                element_ids=dto.observed_label_element_ids, confidence=None
            )
            labels = _observed(_elements(prepared, label_evidence))
            return ImageIR(
                item.object_id,
                prepared.svg.source,
                tuple(visible.text for visible in dto.visible_objects),
                labels,
                (
                    *dto.diagnostics,
                    "Visible objects are model hypotheses; observed labels are exact source occurrences",
                ),
                Verification.PENDING,
            )
        if isinstance(dto, DiagramObservationsDTO):
            node_ids = tuple(node.node_id for node in dto.nodes)
            if len(node_ids) != len(set(node_ids)):
                raise JsonCompletionError("duplicate_diagram_node")
            nodes: list[DiagramNode] = []
            for node in dto.nodes:
                elements = _elements(prepared, node.evidence)
                if not _label_matches(node.label, elements) or not _inside(node.bbox, item.bbox):
                    raise JsonCompletionError("invalid_diagram_node")
                nodes.append(
                    DiagramNode(
                        node.node_id,
                        "" if node.label is None else node.label,
                        node.bbox,
                        _source_ids(elements),
                    )
                )
            edges: list[DiagramEdge] = []
            for edge in dto.edges:
                elements = _elements(prepared, edge.evidence)
                if (
                    edge.source_node_id not in node_ids
                    or edge.target_node_id not in node_ids
                    or not _label_matches(edge.label, elements)
                ):
                    raise JsonCompletionError("invalid_diagram_edge")
                edges.append(
                    DiagramEdge(
                        edge.source_node_id,
                        edge.target_node_id,
                        edge.label,
                        edge.relationship,
                        Verification.PENDING,
                        _source_ids(elements),
                    )
                )
            return DiagramIR(
                item.object_id,
                prepared.svg.source,
                tuple(nodes),
                tuple(edges),
                (
                    *dto.diagnostics,
                    "Node and edge structure is model-inferred and not independently verified",
                ),
                Verification.PENDING,
            )
        if not isinstance(dto, FormulaObservationsDTO):
            raise JsonCompletionError("visual_ir_kind_mismatch")
        elements = _elements(
            prepared,
            VisualEvidenceDTO(
                element_ids=dto.source_literal_element_ids, confidence=dto.confidence
            ),
        )
        if (dto.normalization_state == "unavailable") != (dto.latex is None):
            raise JsonCompletionError("invalid_formula_normalization_state")
        literal = "".join(element.text for element in elements) or None
        return FormulaIR(
            item.object_id,
            prepared.svg.source,
            literal,
            dto.latex,
            _source_ids(elements),
            (
                *dto.diagnostics,
                f"Normalized form state={dto.normalization_state}; normalized form is inferred, never source literal",
            ),
            Verification.PENDING,
        )

    def _infer_description(
        self, *, prepared: _PreparedVisual, item: LayoutObject
    ) -> tuple[ObjectDescription | None, bytes | None, str | None]:
        try:
            result = self._client.complete_json(
                task=f"visual-description-{item.kind.value.lower()}-v1",
                prompt=(
                    "Describe this exact SVG-derived visual region directly from the render and source observations. "
                    "You receive no typed extraction. Return visual-description-v1 and copy svg_digest exactly. "
                    "State only visible content; do not infer financial relationships, identity, intent, causality, or verification. "
                    "Cite exact observation IDs for every copied label. A textless visual may have empty evidence, remains pending, and is not index eligible.\n"
                    + _prompt(prepared)
                ),
                image_png=prepared.model_png,
                response_model=VisualDescriptionDTO,
                max_output_tokens=1800,
            )
        except JsonCompletionError as error:
            return None, None, error.code
        raw = result.json_text.encode()
        dto = result.parsed
        try:
            if dto.svg_digest != prepared.svg.digest:
                raise JsonCompletionError("model_svg_binding_mismatch")
            elements = _elements(prepared, dto.evidence)
            if dto.text.lstrip().startswith(
                ("{", "[", "<", "DiagramIR(", "ImageIR(", "FormulaIR(")
            ):
                raise JsonCompletionError("invalid_visual_description")
            description = ObjectDescription(
                item.object_id,
                prepared.svg.source,
                _source_ids(elements),
                dto.text,
                f"model-visual-description-v1:{self._client.fingerprint}:{result.request_fingerprint}:{result.output_digest}",
                _confidence(dto.evidence.confidence),
                Verification.PENDING,
            )
        except (JsonCompletionError, ValueError) as error:
            code = (
                error.code
                if isinstance(error, JsonCompletionError)
                else "invalid_visual_description"
            )
            return None, raw, code
        return description, raw, None
