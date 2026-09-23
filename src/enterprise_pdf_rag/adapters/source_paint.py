"""Prove SVG glyph roles from pdfspine text operations and embedded outlines.

This module never treats a font-like transform or a text bounding box as proof
that arbitrary SVG paint is text. The recorded font program supplies the shape.
"""

import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from hashlib import sha256
from importlib import import_module
from importlib.metadata import version
from io import BytesIO
from math import hypot, isfinite
from typing import Literal, Protocol, cast, runtime_checkable
from xml.etree import ElementTree

import pdfspine
from pydantic import BaseModel, ConfigDict, TypeAdapter

from enterprise_pdf_rag.adapters.donut_geometry import (
    Matrix,
    Point,
    _bounds,
    _compose,
    _inside,
    _intersects,
    _matrix,
    _path_controls,
    _path_reference,
    _transform,
)
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure, prepare_figure
from enterprise_pdf_rag.adapters.pdfspine_document import _PageText, _spans
from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg
from enterprise_pdf_rag.adapters.source_profile import (
    SourceProfileReceipt,
    read_source_profile,
)
from ragspine.extraction.evidence.document.models import AssetRef, Bounds, TextSidecar, TextSpan
from ragspine.extraction.evidence.page.models import PageInput

type Command = tuple[str, tuple[float, ...]]
type Glyph = tuple[str, int, int | None, Point, tuple[float, ...], Matrix]
type FontPoint = tuple[int | float, int | float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_GLYPHS = TypeAdapter(tuple[Glyph, ...], config=ConfigDict(strict=True))
_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_MATRIX = TypeAdapter[Matrix](Matrix, config=ConfigDict(strict=True))


def _sdk_attribute(value: object, name: str) -> object:
    return getattr(value, name)


@runtime_checkable
class _ReplayList(Protocol):
    def run(self, device: object, matrix: None, area: None) -> None: ...


@runtime_checkable
class _ReplayPage(Protocol):
    def get_displaylist(self, *, annots: bool) -> _ReplayList: ...


@runtime_checkable
class _FontGlyph(Protocol):
    def draw(self, pen: "_OutlinePen") -> None: ...


@runtime_checkable
class _FontReader(Protocol):
    def getBestCmap(self) -> Mapping[int, str] | None: ...
    def getGlyphOrder(self) -> list[str]: ...
    def getGlyphSet(self) -> Mapping[str, _FontGlyph]: ...
    def __getitem__(self, key: str) -> object: ...
    def close(self) -> None: ...


def _font_reader(data: bytes) -> _FontReader:
    # FontTools has no py.typed. Keep its untyped objects behind this adapter;
    # every value used as source evidence is validated before it leaves it.
    factory = import_module("fontTools.ttLib").TTFont
    reader = factory(BytesIO(data))
    for name in ("getBestCmap", "getGlyphOrder", "getGlyphSet", "close"):
        if not callable(getattr(reader, name, None)):
            raise ValueError("unsupported_font_reader")
    return cast(_FontReader, reader)


class _ReplayEvent(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    kind: str
    sequence: int
    matrix: Matrix
    bounds: Bounds | None
    font_buffer: bytes | None
    font_format: str | None
    payload: dict[str, object]


def _source_events(page: pdfspine.Page) -> tuple[_ReplayEvent, ...]:
    events: list[_ReplayEvent] = []

    def capture(event: object) -> None:
        values = {
            key: getattr(event, key)
            for key in (
                "kind",
                "sequence",
                "matrix",
                "bounds",
                "font_buffer",
                "font_format",
            )
        }
        payload = _sdk_attribute(event, "payload")
        if not isinstance(payload, Mapping):
            raise ValueError("unsupported_replay_payload")
        values["payload"] = dict(payload)
        events.append(_ReplayEvent.model_validate(values))

    # These runtime APIs are omitted by pdfspine 0.10's shipped stub. Their
    # output is copied into the strict adapter record above, never domain SDKs.
    factory = _sdk_attribute(pdfspine, "ReplayDevice")
    replay_page = cast(_ReplayPage, page)
    if not callable(factory) or not isinstance(replay_page, _ReplayPage):
        raise ValueError("pdfspine_replay_capability_missing")
    device = cast(Callable[[Callable[[object], None]], object], factory)(capture)
    replay_page.get_displaylist(annots=False).run(device, None, None)
    return tuple(events)


@dataclass(frozen=True, slots=True)
class GlyphPaintProof:
    character: str
    font_sha256: str
    font_glyph_name: str
    source_sequence: int
    source_glyph_index: int
    source_span_id: str | None
    native_path_ref: str
    matrix: Matrix
    bounds: Bounds
    clips: tuple[Bounds, ...] = ()


@dataclass(frozen=True, slots=True)
class VectorPaintProof:
    native_path_ref: str
    source_sequence: int
    paint_order: int
    role: Literal["fill", "transparent"]
    bounds: Bounds
    clips: tuple[Bounds, ...]
    fill_rule: Literal["evenodd", "nonzero"]


@dataclass(frozen=True, slots=True)
class SourcePaintProof:
    source_sha256: str
    page_index: int
    native_svg_digest: str
    bbox: Bounds
    glyphs: tuple[GlyphPaintProof, ...]
    producer: str
    coverage: Literal["complete_source_paint"] = "complete_source_paint"
    vectors: tuple[VectorPaintProof, ...] = ()
    crop_svg_digest: str = ""
    source_revision: str = ""
    source_text_digest: str = ""
    trace_digest: str = ""
    schema_version: Literal["source-paint-proof-v2"] = "source-paint-proof-v2"

    @property
    def proof_id(self) -> str:
        return "source-paint-proof-v2:" + sha256(repr(self).encode()).hexdigest()


def _rounded(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal(".001"), rounding=ROUND_HALF_UP))


def _commands(path: str) -> tuple[Command, ...]:
    if _TOKEN.sub("", path).strip(" ,\t\n\r"):
        raise ValueError("unsupported_native_glyph_path")
    tokens = _TOKEN.findall(path)
    commands: list[Command] = []
    index = 0
    start: tuple[float, ...] | None = None
    while index < len(tokens):
        kind = tokens[index]
        count = {"M": 2, "L": 2, "Q": 4, "C": 6, "Z": 0}.get(kind)
        if count is None or index + count >= len(tokens):
            raise ValueError("unsupported_native_glyph_path")
        values = tuple(float(v) for v in tokens[index + 1 : index + count + 1])
        index += count + 1
        if kind == "M":
            start = values
        if kind == "Z" and commands and commands[-1] == ("L", start):
            commands.pop()
        commands.append((kind, values))
    return tuple(commands)


class _OutlinePen:
    """A bounded TrueType pen; compound/variable glyphs are not accepted."""

    def __init__(self, units_per_em: int) -> None:
        self._units = units_per_em
        self.commands: list[Command] = []

    def _emit(self, kind: str, *points: FontPoint) -> None:
        self.commands.append(
            (kind, tuple(_rounded(v / self._units) for point in points for v in point))
        )

    def moveTo(self, point: FontPoint) -> None:
        self._emit("M", point)

    def lineTo(self, point: FontPoint) -> None:
        self._emit("L", point)

    def qCurveTo(self, *points: FontPoint | None) -> None:
        if not points or any(point is None for point in points):
            raise ValueError("unsupported_implied_glyph_contour")
        concrete = tuple(point for point in points if point is not None)
        for index, control in enumerate(concrete[:-1]):
            following = concrete[index + 1]
            end = (
                following
                if index == len(concrete) - 2
                else ((control[0] + following[0]) / 2, (control[1] + following[1]) / 2)
            )
            self._emit("Q", control, end)

    def curveTo(self, *points: FontPoint) -> None:
        if len(points) != 3:
            raise ValueError("unsupported_glyph_cubic")
        self._emit("C", *points)

    def closePath(self) -> None:
        self._emit("Z")

    def endPath(self) -> None:
        raise ValueError("unclosed_glyph_contour")

    def addComponent(self, glyph_name: str, transform: Matrix) -> None:
        raise ValueError("unsupported_composite_glyph")


@dataclass(frozen=True, slots=True)
class _NativePath:
    commands: tuple[Command, ...]
    matrix: Matrix
    fill: str
    opacity: float
    reference: str
    bounds: Bounds
    clips: tuple[Bounds, ...]
    stroke: str
    stroke_opacity: float
    stroke_width: float
    order: int
    fill_rule: Literal["evenodd", "nonzero"]


def _rectangle(commands: tuple[Command, ...], matrix: Matrix) -> Bounds:
    if (
        tuple(command[0] for command in commands) != ("M", "L", "L", "L", "Z")
        or matrix[1] != 0
        or matrix[2] != 0
    ):
        raise ValueError("unsupported_source_clip")
    points = tuple(_transform(matrix, (value[0], value[1])) for _, value in commands[:-1])
    if (
        len(set(points)) != 4
        or len({p[0] for p in points}) != 2
        or len({p[1] for p in points}) != 2
    ):
        raise ValueError("unsupported_source_clip")
    return _bounds(points)


def _native_paths(svg: str) -> tuple[_NativePath, ...]:
    result: list[_NativePath] = []
    root = ElementTree.fromstring(svg)
    definitions = {node.get("id"): node for node in root.iter() if node.get("id")}

    def walk(node: ElementTree.Element, matrix: Matrix, clips: tuple[Bounds, ...] = ()) -> None:
        tag = node.tag.rsplit("}", 1)[-1]
        allowed = {
            "svg": {"width", "height", "viewBox", "version", "overflow"},
            "g": {"transform", "clip-path"},
            "path": {
                "d",
                "fill",
                "fill-opacity",
                "fill-rule",
                "transform",
                "stroke",
                "stroke-opacity",
                "stroke-width",
            },
        }
        if tag not in allowed or set(node.attrib) - allowed[tag]:
            raise ValueError("unsupported_native_source_paint")
        if own := node.get("transform"):
            matrix = _compose(matrix, _matrix(own))
        if clip := node.get("clip-path"):
            match = re.fullmatch(r"url\(#([^)]+)\)", clip)
            definition = definitions.get(match.group(1)) if match else None
            if definition is None or set(definition.attrib) != {"id"} or len(definition) != 1:
                raise ValueError("unsupported_source_clip")
            child = definition[0]
            if child.tag.rsplit("}", 1)[-1] != "path" or set(child.attrib) - {
                "d",
                "clip-rule",
            }:
                raise ValueError("unsupported_source_clip")
            clips = (*clips, _rectangle(_commands(child.get("d", "")), matrix))
        if tag == "path":
            source = node.get("d", "")
            bounds = _bounds(tuple(_transform(matrix, p) for p in _path_controls(source)))
            reference = _path_reference(node, matrix, clips)
            opacity, stroke_opacity, stroke_width = (
                float(node.get("fill-opacity", "1")),
                float(node.get("stroke-opacity", "1")),
                float(node.get("stroke-width", "1")),
            )
            if not all(isfinite(value) for value in (opacity, stroke_opacity, stroke_width)):
                raise ValueError("nonfinite_native_source_paint")
            result.append(
                _NativePath(
                    _commands(source),
                    matrix,
                    node.get("fill", "#000000"),
                    opacity,
                    reference,
                    bounds,
                    clips,
                    node.get("stroke", "none"),
                    stroke_opacity,
                    stroke_width,
                    len(result),
                    TypeAdapter(Literal["evenodd", "nonzero"]).validate_python(
                        node.get("fill-rule", "nonzero"), strict=True
                    ),
                )
            )
        for child in node:
            if child.tag.rsplit("}", 1)[-1] not in {"defs", "metadata"}:
                walk(child, matrix, clips)

    walk(root, _IDENTITY)
    return tuple(result)


def _span_for(origin: Point, spans: tuple[TextSpan, ...]) -> TextSpan | None:
    candidates = tuple(
        span
        for span in spans
        if abs(span.origin[1] - origin[1]) < 0.002
        and span.bbox[0] - 0.002 <= origin[0] < span.bbox[2] - 0.002
    )
    if len(candidates) > 1:
        raise ValueError("ambiguous_source_glyph_occurrence")
    return candidates[0] if candidates else None


type _PathItem = (
    tuple[Literal["l"], Point, Point]
    | tuple[Literal["c"], Point, Point, Point, Point]
    | tuple[Literal["re"], Bounds]
)
_PATH = TypeAdapter(tuple[_PathItem, ...], config=ConfigDict(strict=True))


def _source_commands(payload: dict[str, object]) -> tuple[Command, ...]:
    parts: list[str] = []
    current: Point | None = None

    def emit(kind: str, *points: Point) -> None:
        parts.append(kind + " ".join(str(_rounded(value)) for point in points for value in point))

    for item in _PATH.validate_python(payload["path"]):
        if item[0] == "re":
            x0, y0, x1, y1 = item[1]
            emit("M", (x0, y0))
            for point in ((x1, y0), (x1, y1), (x0, y1)):
                emit("L", point)
            parts.append("Z")
            current = None
        else:
            if current != item[1]:
                emit("M", item[1])
            if item[0] == "l":
                emit("L", item[2])
                current = item[2]
            else:
                emit("C", item[2], item[3], item[4])
                current = item[4]
    if payload.get("close") is True and parts and parts[-1] != "Z":
        parts.append("Z")
    return _commands("".join(parts))


def _command_bounds(commands: tuple[Command, ...], matrix: Matrix) -> Bounds:
    return _bounds(
        tuple(
            _transform(matrix, (values[index], values[index + 1]))
            for _, values in commands
            for index in range(0, len(values), 2)
        )
    )


def _expand(bounds: Bounds, padding: float) -> Bounds:
    if not isfinite(padding) or padding < 0:
        raise ValueError("unsupported_source_stroke_width")
    return (
        bounds[0] - padding,
        bounds[1] - padding,
        bounds[2] + padding,
        bounds[3] + padding,
    )


def _paint_bounds(path: _NativePath) -> Bounds:
    return (
        _expand(
            path.bounds,
            path.stroke_width * max(hypot(*path.matrix[:2]), hypot(*path.matrix[2:4])) / 2,
        )
        if path.stroke != "none"
        else path.bounds
    )


def _vector_roles(
    events: tuple[_ReplayEvent, ...],
    paths: tuple[_NativePath, ...],
    roi: Bounds,
) -> tuple[tuple[VectorPaintProof, ...], dict[int, tuple[Bounds, ...]]]:
    clips: tuple[Bounds, ...] = ()
    stack: list[tuple[Bounds, ...]] = []
    recorded_clips: dict[int, tuple[Bounds, ...]] = {}
    roles: list[VectorPaintProof] = []
    for event in events:
        if event.kind == "save":
            stack.append(clips)
        elif event.kind == "restore":
            if not stack:
                raise ValueError("unbalanced_source_clip_stack")
            clips = stack.pop()
        elif event.kind == "clip":
            clips = (*clips, _rectangle(_source_commands(event.payload), event.matrix))
        elif event.kind in {"fill", "stroke"}:
            commands = _source_commands(event.payload)
            bounds = _command_bounds(commands, event.matrix)
            if event.kind == "stroke":
                bounds = _expand(
                    bounds,
                    TypeAdapter(float).validate_python(event.payload["width"], strict=True) / 2,
                )
            if not _intersects(bounds, roi):
                continue
            alpha = TypeAdapter(int).validate_python(event.payload["alpha"], strict=True)
            color = f"#{TypeAdapter(int).validate_python(event.payload['color'], strict=True):06x}"
            fill_rule = (
                "evenodd"
                if event.kind == "fill"
                and TypeAdapter(bool).validate_python(event.payload["even_odd"], strict=True)
                else "nonzero"
            )
            if alpha not in {0, 255} or (event.kind == "stroke" and alpha != 0):
                raise ValueError("unsupported_visible_source_stroke_or_alpha")
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
                    )
                )
            )
            if len(matched) != 1:
                raise ValueError("source_vector_does_not_match_native_svg")
            path = matched[0]
            roles.append(
                VectorPaintProof(
                    path.reference,
                    event.sequence,
                    path.order,
                    "transparent" if alpha == 0 else "fill",
                    path.bounds,
                    clips,
                    path.fill_rule,
                )
            )
        elif event.kind not in {"begin", "end", "text"} and (
            event.bounds is None or _intersects(event.bounds, roi)
        ):
            raise ValueError("unsupported_source_paint_operation")
        recorded_clips[event.sequence] = clips
    if stack:
        raise ValueError("unbalanced_source_clip_stack")
    return tuple(roles), recorded_clips


def _check_glyph_visibility(
    paths: tuple[_NativePath, ...],
    roles: tuple[GlyphPaintProof, ...],
    vectors: tuple[VectorPaintProof, ...],
) -> None:
    by_ref = {path.reference: path for path in paths}
    for role in roles:
        if any(not _inside(role.bounds, clip) for clip in role.clips):
            raise ValueError("source_glyph_clipped")
        path = by_ref[role.native_path_ref]
        if any(
            vector.role != "transparent"
            and vector.paint_order > path.order
            and _intersects(vector.bounds, role.bounds)
            for vector in vectors
        ):
            raise ValueError("source_glyph_occluded_by_later_paint")
        if any(
            other.source_span_id != role.source_span_id
            and by_ref[other.native_path_ref].order > path.order
            and _intersects(other.bounds, role.bounds)
            for other in roles
        ):
            raise ValueError("source_glyph_occluded_by_other_text")


def _matched_glyph_roles(
    prepared: PreparedFigure,
    events: tuple[_ReplayEvent, ...],
    native_paths: tuple[_NativePath, ...],
    event_clips: dict[int, tuple[Bounds, ...]],
) -> tuple[GlyphPaintProof, ...]:
    roles = []
    texts: dict[str, list[str]] = defaultdict(list)
    used: set[str] = set()
    for event in events:
        if event.kind != "text":
            continue
        payload = event.payload
        for index, glyph in enumerate(_GLYPHS.validate_python(payload["glyphs"])):
            character, _, mapper_gid, origin, _, trm = glyph
            transformed = _transform(event.matrix, origin)
            span = _span_for(transformed, prepared.paint_text_spans)
            if span is None:
                continue
            texts[span.span_id].append(character)
            if not _intersects(span.bbox, prepared.svg.source.bbox):
                continue
            if len(character) != 1 or payload["alpha"] != 255 or payload["render_mode"] != 0:
                raise ValueError("unsupported_source_text_mode")
            font_bytes = event.font_buffer
            if event.font_format != "TrueType" or font_bytes is None:
                raise ValueError("embedded_truetype_outline_required")
            font = _font_reader(font_bytes)
            try:
                cmap = TypeAdapter(dict[int, str]).validate_python(
                    dict(font.getBestCmap() or {}), strict=True
                )
                order = TypeAdapter(list[str]).validate_python(font.getGlyphOrder(), strict=True)
                name = cmap.get(ord(character))
                if name is None and mapper_gid is not None and 0 < mapper_gid < len(order):
                    name = order[mapper_gid]
                if name is None:
                    raise ValueError("font_glyph_resolution_failed")
                pen = _OutlinePen(
                    TypeAdapter(int).validate_python(
                        _sdk_attribute(font["head"], "unitsPerEm"), strict=True
                    )
                )
                font.getGlyphSet()[name].draw(pen)
            finally:
                font.close()
            if not pen.commands:
                if not character.isspace():
                    raise ValueError("visible_source_character_without_outline")
                continue
            matrix = _compose(
                event.matrix,
                _MATRIX.validate_python(tuple(_rounded(v) for v in trm)),
            )
            color = f"#{TypeAdapter(int).validate_python(payload['fill_color'], strict=True):06x}"
            matched = tuple(
                path
                for path in native_paths
                if path.commands == tuple(pen.commands)
                and path.matrix == matrix
                and path.fill == color
                and path.opacity == 1
                and path.stroke == "none"
                and path.clips == event_clips[event.sequence]
            )
            if len(matched) != 1 or matched[0].reference in used:
                raise ValueError("source_glyph_outline_does_not_match_native_svg")
            path = matched[0]
            used.add(path.reference)
            if _intersects(path.bounds, prepared.svg.source.bbox):
                roles.append(
                    GlyphPaintProof(
                        character,
                        sha256(font_bytes).hexdigest(),
                        name,
                        event.sequence,
                        index,
                        span.span_id,
                        path.reference,
                        matrix,
                        path.bounds,
                        path.clips,
                    )
                )
    for span in prepared.paint_text_spans:
        if (
            _intersects(span.bbox, prepared.svg.source.bbox)
            and "".join(texts[span.span_id]).strip() != span.text.strip()
        ):
            raise ValueError("source_span_text_differs_from_recorded_glyphs")
    return tuple(roles)


def _source_clip_map(events: tuple[_ReplayEvent, ...]) -> dict[int, tuple[Bounds, ...]]:
    clips: tuple[Bounds, ...] = ()
    stack: list[tuple[Bounds, ...]] = []
    result: dict[int, tuple[Bounds, ...]] = {}
    for event in events:
        if event.kind == "save":
            stack.append(clips)
        elif event.kind == "restore":
            if not stack:
                raise ValueError("unbalanced_source_clip_stack")
            clips = stack.pop()
        elif event.kind == "clip":
            clips = (*clips, _rectangle(_source_commands(event.payload), event.matrix))
        result[event.sequence] = clips
    if stack:
        raise ValueError("unbalanced_source_clip_stack")
    return result


@dataclass(frozen=True, slots=True)
class _SourcePaintTrace:
    source_hash: str
    native_paths: tuple[_NativePath, ...]
    events: tuple[_ReplayEvent, ...]
    glyphs: tuple[GlyphPaintProof, ...]
    profile: SourceProfileReceipt
    source_spans: tuple[TextSpan, ...]


def _read_source_trace(pdf: bytes, prepared: PreparedFigure) -> _SourcePaintTrace:
    """Reconstruct pinned source observations and prove glyphs before chart grammar."""
    source_hash = sha256(pdf).hexdigest()
    if not pdf.startswith(b"%PDF-"):
        raise ValueError("source_pdf_header_required")
    if source_hash != prepared.svg.source.document_sha256:
        raise ValueError("source_pdf_digest_mismatch")
    with pdfspine.open(stream=pdf, filetype="pdf") as document:
        page = document[prepared.svg.source.page_index]
        profile = read_source_profile(
            page, source_sha256=source_hash, page_index=prepared.svg.source.page_index
        )
        if page.rotation != 0 or page.annot_xrefs():
            raise ValueError("source_rotation_or_annotation_visibility_unsupported")
        native = page.get_svg_image(text_as_path=False)
        if sha256(native.encode()).hexdigest() != prepared.view.native_svg_digest:
            raise ValueError("source_native_export_mismatch")
        expected_crop = crop_native_svg(
            native,
            width=page.rect.width,
            height=page.rect.height,
            bbox=prepared.svg.source.bbox,
        )
        if (
            expected_crop != prepared.crop_svg
            or sha256(expected_crop.encode()).hexdigest() != prepared.view.crop_svg_digest
        ):
            raise ValueError("source_crop_derivation_mismatch")
        native_paths = _native_paths(native)
        source_spans = _spans(
            _PageText.model_validate(page.get_text("dict")),
            source_digest=source_hash,
            page_index=prepared.svg.source.page_index,
        )
        rebuilt = prepare_figure(
            page=PageInput(
                prepared.view.source_manifest_id,
                source_hash,
                prepared.svg.source.page_index,
                page.rect.width,
                page.rect.height,
                AssetRef(
                    prepared.view.native_svg_digest,
                    "image/svg+xml",
                    len(native.encode()),
                ),
                TextSidecar(
                    "source-text-v1",
                    source_hash,
                    prepared.svg.source.page_index,
                    source_spans,
                ),
            ),
            native_svg=native.encode(),
            bbox=prepared.svg.source.bbox,
            region_id="proof-rebuild-placeholder",
            context_span_ids=tuple(context.source_span_id for context in prepared.page_context),
        )
        if (
            rebuilt.svg.source,
            rebuilt.svg.svg,
            rebuilt.svg.elements,
            rebuilt.paint_text_spans,
            rebuilt.excluded_span_ids,
        ) != (
            prepared.svg.source,
            prepared.svg.svg,
            prepared.svg.elements,
            prepared.paint_text_spans,
            prepared.excluded_span_ids,
        ):
            raise ValueError("source_text_or_metadata_differs_from_pinned_pdf")
        events = _source_events(page)
    roles = _matched_glyph_roles(prepared, events, native_paths, _source_clip_map(events))
    return _SourcePaintTrace(source_hash, native_paths, events, roles, profile, source_spans)


@dataclass(frozen=True, slots=True)
class SourcePaintRevalidation:
    proof: SourcePaintProof
    profile: SourceProfileReceipt
    validator: str


@lru_cache(maxsize=8)
def _build_source_paint_proof(
    pdf: bytes, prepared: PreparedFigure, producer: str, validator: str
) -> SourcePaintRevalidation:
    """Read one pinned page; prove glyph shapes without changing existing assets."""
    trace = _read_source_trace(pdf, prepared)
    source_hash, native_paths, events, roles = (
        trace.source_hash,
        trace.native_paths,
        trace.events,
        trace.glyphs,
    )
    vectors, _ = _vector_roles(events, native_paths, prepared.svg.source.bbox)
    _check_glyph_visibility(native_paths, tuple(roles), vectors)
    proven_refs = tuple(role.native_path_ref for role in roles) + tuple(
        role.native_path_ref for role in vectors
    )
    target_refs = {
        path.reference
        for path in native_paths
        if _intersects(_paint_bounds(path), prepared.svg.source.bbox)
    }
    if len(set(proven_refs)) != len(proven_refs) or set(proven_refs) != target_refs:
        raise ValueError("source_paint_closure_incomplete")
    proof = SourcePaintProof(
        source_hash,
        prepared.svg.source.page_index,
        prepared.view.native_svg_digest,
        prepared.svg.source.bbox,
        tuple(roles),
        producer,
        vectors=vectors,
        crop_svg_digest=prepared.view.crop_svg_digest,
        source_revision=prepared.svg.source.source_revision,
        source_text_digest=sha256(repr(prepared.paint_text_spans).encode()).hexdigest(),
        trace_digest=sha256(
            repr(
                tuple(
                    (
                        event.kind,
                        event.sequence,
                        event.matrix,
                        event.bounds,
                        event.payload,
                        sha256(event.font_buffer).hexdigest() if event.font_buffer else None,
                    )
                    for event in events
                )
            ).encode()
        ).hexdigest(),
    )
    return SourcePaintRevalidation(proof, trace.profile, validator)


def _producer(prepared: PreparedFigure, sdk_version: str) -> str:
    return f"pdfspine/{sdk_version};fonttools/{version('fonttools')};source-replay-font-closure-v2;{prepared.view.renderer_fingerprint}"


def _validator_identity() -> str:
    return f"pdfspine/{pdfspine.__version__};trusted-profile-v1;legacy-0.10.0-to-0.11.0-v1"


def build_source_paint_proof(pdf: bytes, *, prepared: PreparedFigure) -> SourcePaintProof:
    """Rebuild from source, with a bounded process cache over immutable inputs.

    The key contains the actual PDF bytes, complete prepared SVG/view/sidecar and
    rule/SDK/font/renderer identities. A proof JSON or caller status is not a key.
    """
    return _build_source_paint_proof(
        pdf, prepared, _producer(prepared, pdfspine.__version__), _validator_identity()
    ).proof


def revalidate_source_paint_proof(
    pdf: bytes, *, prepared: PreparedFigure, proof: SourcePaintProof
) -> SourcePaintRevalidation:
    producer = _producer(prepared, pdfspine.__version__)
    if pdfspine.__version__ == "0.11.0" and proof.producer == _producer(prepared, "0.10.0"):
        producer = proof.producer
    verified = _build_source_paint_proof(pdf, prepared, producer, _validator_identity())
    if verified.proof != proof:
        raise ValueError("source_paint_proof_revalidation_mismatch")
    return verified


def verify_source_paint_proof(
    pdf: bytes, *, prepared: PreparedFigure, proof: SourcePaintProof
) -> SourcePaintProof:
    return revalidate_source_paint_proof(pdf, prepared=prepared, proof=proof).proof
