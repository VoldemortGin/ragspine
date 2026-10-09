"""SLANet-plus table structure for a region with no ruled grid (ADR 0031): local ONNX, no LLM.

The model reads a rendered crop of the Table region and returns cells with row / column slots
and spans (pdfspine's packaged SLANet-plus pipeline, Apache-2.0). It never supplies text:
``table_inferred_grid.inferred_table`` fills every cell from the page's own spans by centre
and keeps the grid ``PENDING``. ``recheck_inferred_table`` runs the same model again at index /
resolve time and requires the identical IR, exactly as ``check_grid_evidence`` re-proves a
ruled grid.

pdfspine 0.11.0 exposes the SLANet-plus session only through ``pdfspine._onnx`` (its public
``find_tables(backend="onnx")`` also runs the layout detector, which this region does not
need). The private calls are confined to ``_recognize_crop`` and pinned with pdfspine itself.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from hashlib import sha256
from importlib import import_module
from importlib.util import find_spec
from io import BytesIO
from math import ceil, floor
from pathlib import Path
from types import ModuleType
from typing import Protocol, runtime_checkable

import pdfspine

from ragspine.common.evidence.configs import get_settings
from ragspine.extraction.evidence.document.models import Bounds, TextSpan
from ragspine.extraction.evidence.figures.models import SourceAnchor
from ragspine.extraction.evidence.objects.tables.table_inferred_grid import (
    TSR_PRODUCER,
    InferredCell,
    InferredGridRejection,
    InferredStructure,
    inferred_table,
    structure_producer,
)
from ragspine.extraction.evidence.objects.tables.table_models import TableIR
from ragspine.extraction.tables.structure import (
    CellBox,
    TableRegion,
    TableStructure,
    Word,
)

# pdfspine's own ONNX defaults (``OnnxOptions``): render resolution and the white margin the
# model is shown around the crop, in pixels. Part of ``table-structure-tsr-v1``.
RENDER_DPI = 144
CROP_PADDING_PX = 10
MODEL_FILE = "slanet-plus.onnx"
MODELS_ENV = "PDFSPINE_ONNX_MODELS"
MODEL_URL = "https://www.modelscope.cn/models/RapidAI/RapidTable/resolve/v2.0.0/slanet-plus.onnx"


class TableStructureUnavailable(ValueError):
    """The TSR policy was asked for but its model or runtime is not installed."""


def _pil() -> ModuleType:
    """Pillow, an optional dependency (``pdfspine[onnx]``) of this one policy only."""
    return import_module("PIL.Image")


def _model_path(explicit: Path | None) -> Path:
    """Explicit path, else beside the ONNX layout weights (``APP_ONNX_LAYOUT_MODEL``, a file or
    its directory: one weights directory serves ADR 0030 and this), else ``PDFSPINE_ONNX_MODELS``,
    else the default weights directory when the file is there (ADR 0039).
    """
    if explicit is not None:
        return explicit.expanduser()
    layout = (get_settings().onnx_layout_model or "").strip()
    if layout:
        configured = Path(layout).expanduser()
        return (configured if configured.is_dir() else configured.parent) / MODEL_FILE
    root = os.environ.get(MODELS_ENV)
    if not root:
        # ADR 0039: 什么都没配置时, notebook 的 onnx-check 格把权重下载到的默认目录.
        from enterprise_pdf_rag.adapters import onnx_partition

        default = onnx_partition.DEFAULT_ONNX_MODELS_DIR / MODEL_FILE
        if default.is_file():
            return default
        raise TableStructureUnavailable(
            f"表格结构识别 (unverified_table_structure='tsr') 需要本地 SLANet-plus 模型, 但未设置 "
            f"APP_ONNX_LAYOUT_MODEL 或 {MODELS_ENV}。请从 {MODEL_URL} 下载 {MODEL_FILE}"
            " (Apache-2.0) , 与 ONNX 版面权重放在同一个目录 (Databricks 上放到 Volume 路径) ,"
            f" 再把 APP_ONNX_LAYOUT_MODEL 指向该目录 (或其中的版面权重文件) , 或把环境变量"
            f" {MODELS_ENV} 设为该目录; 或把 unverified_table_structure 改回 'rows'。"
        )
    return Path(root).expanduser() / MODEL_FILE


@dataclass(frozen=True, slots=True)
class SlanetPlusRecognizer:
    """``TableStructureRecognizer`` over pdfspine's SLANet-plus session; coordinates only."""

    model_path: Path
    model_sha256: str
    name: str = "slanet-plus"

    @property
    def producer(self) -> str:
        """Rule version, parser version and the weights' digest: what a re-check must match."""
        return f"{TSR_PRODUCER}:pdfspine/{pdfspine.__version__}:{self.model_sha256[:12]}"

    def recognize(self, region: TableRegion) -> TableStructure | None:
        """Cells of the crop ``region.render(RENDER_DPI)`` shows, mapped into ``region.bbox``."""
        if region.render is None:
            raise ValueError("SLANet-plus needs the region's rendered crop")
        return _recognize_crop(self.model_path, region.render(RENDER_DPI), region.bbox)


def _recognize_crop(
    model_path: Path, png: bytes, bbox: tuple[float, float, float, float]
) -> TableStructure | None:
    pil = _pil()
    # Private in pdfspine 0.11.0 (no type stubs): its table-model-only path, pinned with it.
    onnx = import_module("pdfspine._onnx")
    final_cells = import_module("pdfspine._tatr")._final_cells
    crop = pil.open(BytesIO(png)).convert("RGB")
    padded = pil.new(
        "RGB",
        (crop.width + 2 * CROP_PADDING_PX, crop.height + 2 * CROP_PADDING_PX),
        (255, 255, 255),
    )
    padded.paste(crop, (CROP_PADDING_PX, CROP_PADDING_PX))
    options = onnx.OnnxOptions(table_model=os.fspath(model_path), ocr_if_no_text=False)
    tokens, boxes, scores = onnx._get_runtime(options).recognize_table(padded, options)
    raw, reasons = final_cells(onnx._structure_to_cells(tokens, boxes, scores, _strict=True))
    if reasons or not raw:
        return None
    scale_x = (bbox[2] - bbox[0]) / crop.width
    scale_y = (bbox[3] - bbox[1]) / crop.height
    cells = tuple(
        sorted(
            (
                CellBox(
                    row=min(cell["row_nums"]),
                    col=min(cell["column_nums"]),
                    row_span=len(cell["row_nums"]),
                    col_span=len(cell["column_nums"]),
                    bbox=(
                        bbox[0] + (cell["bbox"][0] - CROP_PADDING_PX) * scale_x,
                        bbox[1] + (cell["bbox"][1] - CROP_PADDING_PX) * scale_y,
                        bbox[0] + (cell["bbox"][2] - CROP_PADDING_PX) * scale_x,
                        bbox[1] + (cell["bbox"][3] - CROP_PADDING_PX) * scale_y,
                    ),
                )
                for cell in raw
            ),
            key=lambda cell: (cell.row, cell.col),
        )
    )
    return TableStructure(
        n_rows=max(cell.row + cell.row_span for cell in cells),
        n_cols=max(cell.col + cell.col_span for cell in cells),
        cells=cells,
    )


@cache
def _digest(path: Path, size: int, mtime_ns: int) -> str:
    del size, mtime_ns  # cache keys: a replaced file is hashed again
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _available_model(model_path: Path | None) -> Path:
    """The weights file, after the file and runtime checks; ``TableStructureUnavailable`` else."""
    path = _model_path(model_path)
    if not path.is_file():
        raise TableStructureUnavailable(
            f"表格结构识别模型缺失: {path} 不存在。请从 {MODEL_URL} 下载 {MODEL_FILE} 放到该位置"
            f" (与 ONNX 版面权重同一目录, 或把 {MODELS_ENV} 指向存放它的目录) ;"
            " 或把 unverified_table_structure 改回 'rows'。"
        )
    missing = [name for name in ("onnxruntime", "numpy", "PIL") if find_spec(name) is None]
    if missing:
        raise TableStructureUnavailable(
            "表格结构识别需要本地 ONNX 运行时: 请安装 `pip install 'pdfspine[onnx]'`"
            f" (onnxruntime、numpy、Pillow) ; 当前缺少 {', '.join(missing)}。"
            "或把 unverified_table_structure 改回 'rows'。"
        )
    return path


def table_structure_unavailable() -> str | None:
    """``None`` when ``"tsr"`` can run now, else the reason; the same checks as the ingest's
    preflight, without hashing the weights or loading the model (the notebook's ``"auto"``)."""
    try:
        _available_model(None)
    except TableStructureUnavailable as error:
        return str(error)
    return None


def table_structure_recognizer(model_path: Path | None = None) -> SlanetPlusRecognizer:
    """The configured recognizer, or ``TableStructureUnavailable`` saying what to install."""
    path = _available_model(model_path)
    stat = path.stat()
    return SlanetPlusRecognizer(path, _digest(path.resolve(), stat.st_size, stat.st_mtime_ns))


@runtime_checkable
class ProducingRecognizer(Protocol):
    """A ``TableStructureRecognizer`` that names itself well enough to be re-run and compared."""

    @property
    def name(self) -> str: ...

    @property
    def producer(self) -> str: ...

    def recognize(self, region: TableRegion) -> TableStructure | None: ...


def _region(source_page: pdfspine.Page, bbox: Bounds, spans: Sequence[TextSpan]) -> TableRegion:
    """The Table region at whole render pixels, with a ``render`` hook returning its PNG crop."""
    pixmap = source_page.get_displaylist().get_pixmap(dpi=RENDER_DPI, colorspace=3, alpha=False)
    if pixmap.n != 3:
        raise ValueError("SLANet-plus needs an RGB page render")
    image = _pil().frombytes("RGB", (pixmap.width, pixmap.height), bytes(pixmap.samples))
    width, height = float(source_page.rect.width), float(source_page.rect.height)
    scale_x, scale_y = pixmap.width / width, pixmap.height / height
    pixels = (
        max(0, floor(bbox[0] * scale_x)),
        max(0, floor(bbox[1] * scale_y)),
        min(pixmap.width, ceil(bbox[2] * scale_x)),
        min(pixmap.height, ceil(bbox[3] * scale_y)),
    )
    buffer = BytesIO()
    image.crop(pixels).save(buffer, format="PNG")
    png = buffer.getvalue()

    def render(dpi: int) -> bytes:
        if dpi != RENDER_DPI:
            raise ValueError(f"This region was rendered at {RENDER_DPI} dpi")
        return png

    return TableRegion(
        bbox=(pixels[0] / scale_x, pixels[1] / scale_y, pixels[2] / scale_x, pixels[3] / scale_y),
        words=tuple(Word(span.text, span.bbox) for span in spans),
        page_width=width,
        page_height=height,
        render=render,
    )


def infer_table_grid(
    source_page: pdfspine.Page,
    *,
    object_id: str,
    anchor: SourceAnchor,
    spans: Sequence[TextSpan],
    recognizer: ProducingRecognizer,
) -> TableIR | InferredGridRejection:
    """Run the model on the region ``anchor`` names and fill its grid from ``spans``.

    ``spans`` are the region's own occurrences in page order.
    """
    try:
        structure = recognizer.recognize(_region(source_page, anchor.bbox, spans))
    except TableStructureUnavailable:
        raise
    except (pdfspine.PdfError, RuntimeError, OSError, ValueError) as error:
        # One crop the model cannot run on is that table's fallback, not the ingest's failure.
        return InferredGridRejection("model_error", type(error).__name__)
    return inferred_table(
        object_id,
        anchor,
        None
        if structure is None
        else InferredStructure(
            structure.n_rows,
            structure.n_cols,
            tuple(
                InferredCell(cell.row, cell.col, cell.row_span, cell.col_span, cell.bbox)
                for cell in structure.cells
            ),
        ),
        spans,
        producer=recognizer.producer,
    )


def recheck_inferred_table(
    source_page: pdfspine.Page, table: TableIR, spans: Sequence[TextSpan]
) -> None:
    """Raise ``ValueError`` unless the configured model re-infers ``table`` exactly.

    ``spans`` are the page's text layer in page order; the table's own are taken from it.
    """
    recorded = structure_producer(table)
    if recorded is None:
        raise ValueError("Inferred table grid does not record its structure producer")
    recognizer = table_structure_recognizer()
    if recognizer.producer != recorded:
        raise ValueError(
            "Inferred table grid was produced by "
            f"{recorded}, but the configured model is {recognizer.producer}; "
            "re-run semantics for this document"
        )
    named = {span_id for cell in table.cells for span_id in cell.source_span_ids}
    again = infer_table_grid(
        source_page,
        object_id=table.object_id,
        anchor=table.source,
        spans=tuple(span for span in spans if span.span_id in named),
        recognizer=recognizer,
    )
    if again != table:
        raise ValueError("Inferred table grid does not re-derive from the same structure model")
