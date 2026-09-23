"""Source proof for displayed bar labels, retaining unknown stroke-style fidelity."""

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
from typing import Literal

import pdfspine
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.donut_geometry import (
    Matrix,
    _compose,
    _inside,
    _intersects,
    _transform,
)
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure, prepare_figure
from enterprise_pdf_rag.adapters.source_paint import (
    Command,
    GlyphPaintProof,
    _command_bounds,
    _NativePath,
    _read_source_trace,
    _ReplayEvent,
    _source_clip_map,
    _source_commands,
)
from enterprise_pdf_rag.adapters.source_profile import (
    SourceProfileReceipt,
)
from enterprise_pdf_rag.adapters.stroke_visibility import (
    StrokeEnvelope,
    similarity_scale,
    stroke_envelope,
)
from ragspine.extraction.evidence.document.models import Bounds, TextSidecar
from ragspine.extraction.evidence.figures.chart_qa.displayed_models import PageContextCitation
from ragspine.extraction.evidence.figures.models import Confidence, Verification
from ragspine.extraction.evidence.page.models import PageInput


@dataclass(frozen=True, slots=True)
class BarVectorPaint:
    native_path_ref: str
    source_sequence: int
    paint_order: int
    kind: Literal["fill", "stroke"]
    commands: tuple[Command, ...]
    bounds: Bounds
    clips: tuple[Bounds, ...]
    color: str
    alpha: int
    fill_rule: Literal["evenodd", "nonzero"]
    visibility_region: Bounds
    width: float | None = None
    stroke_ctm: Matrix | None = None
    envelope_rule: Literal["pdf-solid-stroke-envelope-v1"] = "pdf-solid-stroke-envelope-v1"

    @property
    def envelope(self) -> StrokeEnvelope | None:
        if self.kind != "stroke":
            return None
        if self.width is None or self.stroke_ctm is None:
            raise ValueError("stroke_source_parameters_missing")
        return stroke_envelope(
            self.commands,
            width=self.width,
            stroke_ctm=self.stroke_ctm,
            region=self.visibility_region,
        )

    @property
    def style_status(self) -> str:
        return self.envelope.style_status if self.envelope else "not_stroked"

    def intersects(self, bounds: Bounds) -> bool:
        if self.alpha == 0:
            return False
        return (
            self.envelope.intersects(bounds) if self.envelope else _intersects(self.bounds, bounds)
        )


@dataclass(frozen=True, slots=True)
class SourcePaintBarProof:
    source_sha256: str
    page_index: int
    native_svg_digest: str
    bbox: Bounds
    glyphs: tuple[GlyphPaintProof, ...]
    vectors: tuple[BarVectorPaint, ...]
    profile: SourceProfileReceipt
    crop_svg_digest: str
    source_revision: str
    source_text_digest: str
    source_page_spans_digest: str
    trace_digest: str
    producer: str
    schema_version: Literal["source-paint-bar-proof-v3"] = "source-paint-bar-proof-v3"
    coverage: Literal["complete_source_paint_bounded_strokes"] = (
        "complete_source_paint_bounded_strokes"
    )
    stroke_fidelity: Literal["pending"] = "pending"

    @property
    def proof_id(self) -> str:
        return "source-paint-bar-proof-v3:" + sha256(repr(self).encode()).hexdigest()


def _transformed(commands: tuple[Command, ...], matrix: Matrix) -> tuple[Command, ...]:
    return tuple(
        (
            operation,
            tuple(
                value
                for index in range(0, len(values), 2)
                for value in _transform(matrix, (values[index], values[index + 1]))
            ),
        )
        for operation, values in commands
    )


def _vector_roles(
    events: tuple[_ReplayEvent, ...], paths: tuple[_NativePath, ...], roi: Bounds
) -> tuple[BarVectorPaint, ...]:
    event_clips = _source_clip_map(events)
    roles = []
    for event in events:
        if event.kind not in {"fill", "stroke"}:
            if event.kind not in {"begin", "end", "save", "restore", "clip", "text"}:
                raise ValueError("unsupported_source_paint_operation")
            continue
        commands = _source_commands(event.payload)
        bounds = _command_bounds(commands, event.matrix)
        if event.kind == "fill" and not _intersects(bounds, roi):
            continue
        alpha = TypeAdapter(int).validate_python(event.payload["alpha"], strict=True)
        if alpha not in {0, 255}:
            raise ValueError("unsupported_source_paint_alpha")
        color = f"#{TypeAdapter(int).validate_python(event.payload['color'], strict=True):06x}"
        clips = event_clips[event.sequence]
        fill_rule: Literal["evenodd", "nonzero"] = "nonzero"
        width = None
        ctm: Matrix | None = None
        transformed = _transformed(commands, event.matrix)
        if event.kind == "fill":
            if TypeAdapter(bool).validate_python(event.payload["even_odd"], strict=True):
                fill_rule = "evenodd"
        else:
            width = TypeAdapter(float).validate_python(event.payload["width"], strict=True)
            ctm = TypeAdapter(Matrix).validate_python(event.payload["ctm"], strict=True)
            stroke_envelope(
                transformed,
                width=width,
                stroke_ctm=_compose(event.matrix, ctm),
                dashes=TypeAdapter(str).validate_python(event.payload["dashes"], strict=True),
                region=roi,
            )
        matched = tuple(
            path
            for path in paths
            if path.commands == commands
            and path.matrix == event.matrix
            and path.clips == clips
            and (
                (
                    event.kind == "fill"
                    and path.fill == color
                    and path.opacity == alpha / 255
                    and path.stroke == "none"
                    and path.fill_rule == fill_rule
                )
                or (
                    event.kind == "stroke"
                    and path.fill == "none"
                    and path.stroke == color
                    and path.stroke_opacity == alpha / 255
                    and width is not None
                    and ctm is not None
                    and path.stroke_width == width * similarity_scale(ctm)
                )
            )
        )
        if len(matched) != 1:
            raise ValueError("source_bar_vector_does_not_match_native_svg")
        path = matched[0]
        roles.append(
            BarVectorPaint(
                path.reference,
                event.sequence,
                path.order,
                "fill" if event.kind == "fill" else "stroke",
                transformed,
                path.bounds,
                clips,
                color,
                alpha,
                fill_rule,
                roi,
                width,
                _compose(event.matrix, ctm) if ctm is not None else None,
            )
        )
    return tuple(roles)


def _glyph_visibility(
    glyphs: tuple[GlyphPaintProof, ...],
    vectors: tuple[BarVectorPaint, ...],
    paths: tuple[_NativePath, ...],
) -> None:
    by_ref = {path.reference: path for path in paths}
    for glyph in glyphs:
        if any(not _inside(glyph.bounds, clip) for clip in glyph.clips):
            raise ValueError("source_glyph_clipped")
        path = by_ref[glyph.native_path_ref]
        if any(
            vector.paint_order > path.order and vector.intersects(glyph.bounds)
            for vector in vectors
        ):
            raise ValueError("source_glyph_occluded_by_later_paint")
        if any(
            other.source_span_id != glyph.source_span_id
            and by_ref[other.native_path_ref].order > path.order
            and _intersects(other.bounds, glyph.bounds)
            for other in glyphs
        ):
            raise ValueError("source_glyph_occluded_by_other_text")


@lru_cache(maxsize=8)
def _build_bar_source_paint_proof(
    pdf: bytes, prepared: PreparedFigure, producer: str
) -> SourcePaintBarProof:
    if sha256(pdf).hexdigest() != prepared.svg.source.document_sha256:
        raise ValueError("source_pdf_digest_mismatch")
    trace = _read_source_trace(pdf, prepared)
    vectors = _vector_roles(trace.events, trace.native_paths, prepared.svg.source.bbox)
    _glyph_visibility(trace.glyphs, vectors, trace.native_paths)
    proven_refs = tuple(glyph.native_path_ref for glyph in trace.glyphs) + tuple(
        vector.native_path_ref for vector in vectors
    )
    target_refs = {
        path.reference
        for path in trace.native_paths
        if path.stroke != "none" or _intersects(path.bounds, prepared.svg.source.bbox)
    }
    if len(set(proven_refs)) != len(proven_refs) or set(proven_refs) != target_refs:
        raise ValueError("source_paint_closure_incomplete")
    return SourcePaintBarProof(
        trace.source_hash,
        prepared.svg.source.page_index,
        prepared.view.native_svg_digest,
        prepared.svg.source.bbox,
        trace.glyphs,
        vectors,
        trace.profile,
        prepared.view.crop_svg_digest,
        prepared.svg.source.source_revision,
        sha256(repr(prepared.paint_text_spans).encode()).hexdigest(),
        sha256(repr(trace.source_spans).encode()).hexdigest(),
        sha256(repr(trace.events).encode()).hexdigest(),
        producer,
    )


def build_bar_source_paint_proof(pdf: bytes, *, prepared: PreparedFigure) -> SourcePaintBarProof:
    producer = f"pdfspine/{pdfspine.__version__};fonttools/{version('fonttools')};source-bar-visibility-v3;trusted-profile-v1;{prepared.view.renderer_fingerprint}"
    return _build_bar_source_paint_proof(pdf, prepared, producer)


def verify_bar_source_paint_proof(
    pdf: bytes, *, prepared: PreparedFigure, proof: SourcePaintBarProof
) -> SourcePaintBarProof:
    rebuilt = build_bar_source_paint_proof(pdf, prepared=prepared)
    if rebuilt != proof:
        raise ValueError("bar_source_paint_revalidation_mismatch")
    return rebuilt


def prove_page_context(
    pdf: bytes,
    *,
    page: PageInput,
    native_svg: bytes,
    source_text: bytes,
    source_span_id: str,
) -> tuple[PageContextCitation, SourcePaintBarProof]:
    if TypeAdapter(TextSidecar).validate_json(source_text, strict=True) != page.text:
        raise ValueError("page_context_sidecar_mismatch")
    spans = tuple(span for span in page.text.spans if span.span_id == source_span_id)
    if len(spans) != 1:
        raise ValueError("page_context_occurrence_missing")
    span = spans[0]
    prepared = prepare_figure(
        page=page,
        native_svg=native_svg,
        bbox=span.bbox,
        region_id="page-context:" + source_span_id,
    )
    proof = build_bar_source_paint_proof(pdf, prepared=prepared)
    if proof.source_page_spans_digest != sha256(repr(page.text.spans).encode()).hexdigest():
        raise ValueError("page_context_full_sidecar_differs_from_pdf")
    if not proof.glyphs or any(glyph.source_span_id != span.span_id for glyph in proof.glyphs):
        raise ValueError("page_context_visible_occurrence_ambiguous")
    return PageContextCitation(
        page.source_manifest_id,
        sha256(source_text).hexdigest(),
        span.span_id,
        span.text,
        prepared.svg.source,
        (0, len(span.text)),
        Verification.VERIFIED,
        Confidence(None, "separate page-context source glyph/clip/alpha/occlusion proof"),
    ), proof
