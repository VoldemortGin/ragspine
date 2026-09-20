"""Quote the source fields a formula proof reads: span matrices, characters and drawn paths.

The released drawing API reports PDF bottom-left coordinates while text spans use
top-left page coordinates, so every path is normalized here exactly like
``pdfspine_figure`` does. Nothing is inferred: a span must still be the one the pinned
text sidecar recorded, and an unsupported drawing primitive fails closed.
"""

from hashlib import sha256
from math import isfinite

import pdfspine
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from enterprise_pdf_rag.documents.models import Bounds
from enterprise_pdf_rag.processing.formula_models import (
    FormulaSourceObservation,
    Matrix,
    ObservedChar,
    ObservedPath,
    ObservedRun,
    Point,
)
from enterprise_pdf_rag.processing.formula_rules import PATH_INSIDE_TOLERANCE
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput

_RECORDS = TypeAdapter(list[dict[str, object]])
_DRAWINGS = TypeAdapter(list[dict[str, object]])
_POINT = TypeAdapter(tuple[float, float])
_BOUNDS = TypeAdapter(tuple[float, float, float, float])


class _RawChar(BaseModel):
    """One ``rawdict`` character: the smallest unit a token substring can quote."""

    model_config = ConfigDict(extra="ignore")
    c: str
    bbox: Bounds


class _RawSpan(BaseModel):
    """A ``rawdict`` span has no ``text`` key; its text is its characters in order."""

    model_config = ConfigDict(extra="ignore")
    bbox: Bounds
    origin: Point
    size: float
    font: str
    flags: int
    ctm: Matrix
    text_matrix: Matrix
    dir: Point | None = None
    chars: list[_RawChar]


class _RawPage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    blocks: list[dict[str, object]]


class _Drawing(BaseModel):
    """The nine documented ``get_cdrawings()`` keys, same projection as ``pdfspine_figure``."""

    model_config = ConfigDict(extra="forbid")
    type: str
    rect: Bounds
    color: tuple[float, ...] | None
    fill: tuple[float, ...] | None
    width: float
    dashes: str
    close_path: bool = Field(alias="closePath")
    even_odd: bool
    items: tuple[tuple[object, ...], ...]


def _top_left(bounds: Bounds, height: float) -> Bounds:
    x0, y0, x1, y1 = bounds
    return min(x0, x1), height - max(y0, y1), max(x0, x1), height - min(y0, y1)


def _point(value: object, height: float) -> Point:
    x, y = _POINT.validate_python(value)
    return x, height - y


def _contains(outer: Bounds, inner: Bounds, *, tolerance: float = 0.5) -> bool:
    return (
        outer[0] - tolerance <= inner[0]
        and outer[1] - tolerance <= inner[1]
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _item(entry: tuple[object, ...], height: float) -> tuple[str, tuple[Point, ...]]:
    """Flip one drawing primitive into page top-left points; anything else is refused."""
    if len(entry) == 3 and entry[0] == "l":
        return "l", (_point(entry[1], height), _point(entry[2], height))
    if len(entry) == 2 and entry[0] == "re":
        x0, y0, x1, y1 = _top_left(_BOUNDS.validate_python(entry[1]), height)
        return "re", ((x0, y0), (x1, y1))
    if len(entry) == 5 and entry[0] == "c":
        return "c", tuple(_point(value, height) for value in entry[1:])
    raise ValueError("Unsupported drawing item")


def _validate_input(pdf: bytes, *, page: PageInput, item: LayoutObject) -> None:
    if sha256(pdf).hexdigest() != page.source_sha256:
        raise ValueError("PDF bytes do not match the PageInput source SHA-256")
    if page.page_index < 0:
        raise ValueError("Formula source page index must be nonnegative")
    if item.kind is not ObjectKind.FORMULA:
        raise ValueError("Formula adapter requires a Formula layout object")
    x0, y0, x1, y1 = item.bbox
    if not all(isfinite(value) for value in item.bbox) or not (
        0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height
    ):
        raise ValueError("Formula layout bbox is outside PageInput geometry")
    observed = {span.span_id for span in page.text.spans}
    if (
        len(set(item.source_span_ids)) != len(item.source_span_ids)
        or not set(item.source_span_ids) <= observed
    ):
        raise ValueError("Formula layout references unknown source occurrences")


def observe_formula(pdf: bytes, *, page: PageInput, item: LayoutObject) -> FormulaSourceObservation:
    """Re-open the pinned PDF and quote, for the object's own spans, what the proof reads.

    Raises ``ValueError`` when the PDF, the page geometry or any owned source occurrence
    differs from the pinned sidecar, so a drifted source can never be proven.
    """
    _validate_input(pdf, page=page, item=item)
    document = pdfspine.open(stream=pdf, filetype="pdf")
    try:
        if page.page_index >= document.page_count:
            raise ValueError("Formula source page is absent from the PDF")
        source_page = document.load_page(page.page_index)
        if source_page.rotation != 0 or tuple(source_page.rect) != (
            0.0,
            0.0,
            page.width,
            page.height,
        ):
            raise ValueError("Formula page geometry differs from PageInput")
        stored = {span.span_id: span for span in page.text.spans}
        wanted = set(item.source_span_ids)
        runs: list[ObservedRun] = []
        raw = _RawPage.model_validate(source_page.get_text("rawdict"))
        for block_index, block in enumerate(raw.blocks):
            if block.get("type") != 0:
                continue
            for line_index, line in enumerate(_RECORDS.validate_python(block.get("lines"))):
                for span_index, payload in enumerate(_RECORDS.validate_python(line.get("spans"))):
                    occurrence = (
                        f"{page.source_sha256}:{page.page_index}:"
                        f"{block_index}:{line_index}:{span_index}"
                    )
                    span_id = f"span-v1-{sha256(occurrence.encode()).hexdigest()}"
                    if span_id not in wanted:
                        continue
                    span = _RawSpan.model_validate(payload)
                    text = "".join(char.c for char in span.chars)
                    known = stored[span_id]
                    if (text, span.bbox, span.origin, span.font, span.size) != (
                        known.text,
                        known.bbox,
                        known.origin,
                        known.font,
                        known.size,
                    ):
                        raise ValueError(
                            "Formula source occurrence differs from the pinned text sidecar"
                        )
                    direction = (
                        span.dir
                        if span.dir is not None
                        else _POINT.validate_python(line.get("dir", (1.0, 0.0)))
                    )
                    runs.append(
                        ObservedRun(
                            span_id,
                            text,
                            span.bbox,
                            span.origin,
                            span.size,
                            span.font,
                            direction,
                            span.ctm,
                            span.text_matrix,
                            span.flags,
                            tuple(ObservedChar(char.c, char.bbox) for char in span.chars),
                        )
                    )
        if {run.span_id for run in runs} != wanted:
            raise ValueError("Formula object owns a source occurrence the PDF does not print")
        paths: list[ObservedPath] = []
        for index, value in enumerate(_DRAWINGS.validate_python(source_page.get_cdrawings())):
            drawing = _Drawing.model_validate(value)
            if not _contains(
                item.bbox,
                _top_left(drawing.rect, page.height),
                tolerance=PATH_INSIDE_TOLERANCE,
            ):
                continue
            paths.append(
                ObservedPath(
                    index,
                    drawing.type,
                    drawing.width,
                    drawing.close_path,
                    tuple(_item(entry, page.height) for entry in drawing.items),
                )
            )
        return FormulaSourceObservation(
            "formula-source-observation-v1",
            f"pdfspine/{pdfspine.__version__}",
            page.source_sha256,
            page.page_index,
            page.height,
            item.bbox,
            tuple(runs),
            tuple(paths),
        )
    finally:
        document.close()
