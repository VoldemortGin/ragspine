"""无图文本页的确定性版面切分: pdfspine 块信息直接产出 ``page-layout-v2`` 同构对象划分.

ADR 00NN(deterministic-text-page-partition). 组合切分器按页分诊: "可确定性处理"的页
(无图片 / 无剩余图形 / 无旋转文字 / 栏式可读)由本模块从 span 几何直接切分, 不调用模型;
拿不准的页带着机器可读的原因码回退到被包装的模型切分器(ADR 0013 修订 1 的原则:
读不懂的版面必须零代价回退, 而不是去猜). 两种产物 producer 不同, 缓存与审计互不混淆.

不变量: 对象里的文字全部来自 span 的逐字内容(本模块只分组, 从不改写), 每个 span 都有
归属, 产物通过 ``validate_partition``; 诊断只记原因码与计数, 不记正文.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from math import ceil
from typing import Any

import pdfspine
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.deterministic_partition_geometry import (
    BlockSpec,
    RunningKey,
    TextLine,
    body_blocks,
    body_font_size,
    column_layout,
    is_page_number,
    running_lines,
    split_margin_lines,
    text_lines,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.pdfspine_tables import LINE_MAX_THICKNESS
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.shared_pdf import opened_pdf, source_pdf
from ragspine.extraction.evidence.document.models import Bounds, DocumentSnapshot
from ragspine.extraction.evidence.figures.models import Confidence, content_id
from ragspine.extraction.evidence.page.geometry import contains
from ragspine.extraction.evidence.page.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    PagePartition,
    ProcessingManifest,
    StageState,
)
from ragspine.extraction.evidence.page.ports import PagePartitioner
from ragspine.extraction.evidence.page.service import validate_partition

# 确定性产物的 producer 前缀; 与模型 producer("page-layout-mapper-v3:…")天然区分.
DETERMINISTIC_PRODUCER_PREFIX = "page-layout-deterministic-v1"
# 回退原因码(机器可读; 诊断里只出现这些码, 绝不出现正文).
FALLBACK_REASONS = (
    "no_text_layer",
    "rotated_text",
    "span_outside_page",
    "page_geometry_mismatch",
    "has_image",
    "residual_graphics",
    "ambiguous_columns",
    "partition_invalid",
)
_DETERMINISTIC_MARK = "deterministic-partition: ok"
_FALLBACK_MARK = "deterministic-partition: model fallback ("

# -- 跨页重复装饰(色块 / 面板 / 分隔条): 同形同位出现在 >=30% 且 >=2 页.
DECORATION_PAGE_SHARE = 0.3
MIN_DECORATION_PAGES = 2
# -- 跨页重复的小图片(logo 之类)按装饰忽略; 超过页面积 5% 的图片永远回退.
IMAGE_DECORATION_MAX_SHARE = 0.05
# -- 覆盖 >=90% 页面积的单个填充矩形是背景, 不触发回退.
BACKGROUND_MIN_SHARE = 0.9
# -- 表格区域内的绘制(格线 / 表头底纹)不算剩余图形; 区域外扩 2pt 容纳贴边描边.
TABLE_BBOX_SLACK = 2.0
# -- 只有 >=2 行且 >=2 列的有线命中才可能成为 Table 对象(单格框是文本框, 不是表).
MIN_TABLE_ROWS = 2
MIN_TABLE_COLS = 2
_AXIS_TOLERANCE = 1e-6


def _bounds(rect: Iterable[object]) -> Bounds:
    values = tuple(float(value) for value in rect)  # type: ignore[arg-type]
    if len(values) != 4:
        raise ValueError("pdfspine returned invalid bounds")
    return values[0], values[1], values[2], values[3]


def _center_inside(inner: Bounds, outer: Bounds) -> bool:
    # 与 adapters/pdfspine_tables._center_inside 逐字一致: 下游表格证明用同一归属判据.
    x = (inner[0] + inner[2]) / 2
    y = (inner[1] + inner[3]) / 2
    return outer[0] <= x <= outer[2] and outer[1] <= y <= outer[3]


def _overlaps(first: Bounds, second: Bounds) -> bool:
    return not (
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
    )


def drawing_signature(drawing: dict[str, Any]) -> tuple[object, ...]:
    """跨页重复判定用的形状键: 类型 + 取整矩形 + 操作序列 + 线宽(不含颜色即可区分)."""
    rect = _bounds(drawing["rect"])
    return (
        str(drawing.get("type", "")),
        tuple(round(value) for value in rect),
        tuple(str(item[0]) for item in drawing["items"]),
        round(float(drawing.get("width") or 0.0), 2),
    )


def image_signature(info: dict[str, Any]) -> tuple[object, ...]:
    """跨页重复判定用的图片键: 取整 bbox + 像素尺寸(同一 logo 每页同位同图)."""
    return (
        tuple(round(value) for value in _bounds(info["bbox"])),
        int(info.get("width") or 0),
        int(info.get("height") or 0),
    )


def is_thin_ruling_drawing(drawing: dict[str, Any]) -> bool:
    """整个绘制都是细线类(轴对齐细描边 / 细矩形)才算: 表格线 / 下划线 / 分隔线."""
    width = float(drawing.get("width") or 0.0)
    stroked = "s" in str(drawing.get("type", ""))
    for item in drawing["items"]:
        operator = item[0]
        if operator == "l":
            (ax, ay), (bx, by) = tuple(item[1]), tuple(item[2])
            axis_aligned = abs(ay - by) <= _AXIS_TOLERANCE or abs(ax - bx) <= _AXIS_TOLERANCE
            if not (stroked and width <= LINE_MAX_THICKNESS and axis_aligned):
                return False
        elif operator == "re":
            rx0, ry0, rx1, ry1 = (float(value) for value in item[1])
            thin = min(rx1 - rx0, ry1 - ry0) <= LINE_MAX_THICKNESS
            if not thin:
                return False
        else:
            return False
    return True


def is_page_background(drawing: dict[str, Any], *, width: float, height: float) -> bool:
    """覆盖 >=90% 页面积的单个填充矩形(整页底色)."""
    if drawing.get("fill") is None:
        return False
    items = drawing["items"]
    if len(items) != 1 or items[0][0] != "re":
        return False
    x0, y0, x1, y1 = _bounds(drawing["rect"])
    return (x1 - x0) * (y1 - y0) >= BACKGROUND_MIN_SHARE * width * height


@dataclass(frozen=True, slots=True)
class _DocumentStats:
    """整份文档扫一遍得到的轻量跨页特征(只有键与计数, 不保留正文)."""

    running: frozenset[RunningKey]
    decoration_drawings: frozenset[tuple[object, ...]]
    decoration_images: frozenset[tuple[object, ...]]


@dataclass(frozen=True, slots=True)
class _EmittedTable:
    bbox: Bounds
    span_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PartitionCounts:
    """确定性处理的页数 / 回退页数与原因码分布(从已存产物重导出, 重跑也稳定)."""

    deterministic_pages: int
    model_fallback_pages: int
    fallback_reasons: dict[str, int]


EMPTY_PARTITION_COUNTS = PartitionCounts(0, 0, {})


class TextPageRouterPartitioner:
    """组合切分器: 可确定性处理的页零模型调用, 其余页原样交给被包装的模型切分器."""

    def __init__(
        self, model: PagePartitioner, sources: LocalDocumentStore, snapshot: DocumentSnapshot
    ) -> None:
        self.model = model
        self.sources = sources
        self.snapshot = snapshot
        self.deterministic_fingerprint = (
            f"{DETERMINISTIC_PRODUCER_PREFIX}:pdfspine/{pdfspine.__version__}"
        )
        self.fingerprint = (
            f"page-layout-text-page-router-v1:{self.deterministic_fingerprint};{model.fingerprint}"
        )
        self._stats: _DocumentStats | None = None

    def partition(self, page: PageInput) -> PagePartition:
        if page.source_sha256 != self.snapshot.manifest.source.sha256:
            raise ValueError("Deterministic partition got a page from another source")
        pdf = source_pdf(self.sources, self.snapshot)
        with opened_pdf(pdf) as document:
            stats = self._document_stats(document)
            reason, partition = self._deterministic(page, document, stats)
        if partition is not None:
            return partition
        fallback = self.model.partition(page)
        return replace(fallback, diagnostics=(*fallback.diagnostics, f"{_FALLBACK_MARK}{reason})"))

    def _document_stats(self, document: pdfspine.Document) -> _DocumentStats:
        if self._stats is not None:
            return self._stats
        pages = self.snapshot.manifest.pages
        sidecars = [
            (record.height, read_text_sidecar(self.sources, self.snapshot, index).spans)
            for index, record in enumerate(pages)
        ]
        needed = max(MIN_DECORATION_PAGES, ceil(DECORATION_PAGE_SHARE * len(pages)))
        drawing_counts: Counter[tuple[object, ...]] = Counter()
        image_counts: Counter[tuple[object, ...]] = Counter()
        for index in range(len(pages)):
            source_page = document.load_page(index)
            drawing_counts.update({drawing_signature(d) for d in source_page.get_drawings()})
            image_counts.update({image_signature(i) for i in source_page.get_image_info()})
        self._stats = _DocumentStats(
            running_lines(sidecars),
            frozenset(key for key, count in drawing_counts.items() if count >= needed),
            frozenset(key for key, count in image_counts.items() if count >= needed),
        )
        return self._stats

    def _deterministic(
        self, page: PageInput, document: pdfspine.Document, stats: _DocumentStats
    ) -> tuple[str, PagePartition | None]:
        spans = page.text.spans
        if not spans:
            return "no_text_layer", None
        if any(
            abs(span.direction[0] - 1.0) > _AXIS_TOLERANCE
            or abs(span.direction[1]) > _AXIS_TOLERANCE
            for span in spans
        ):
            return "rotated_text", None
        page_bounds = (0.0, 0.0, page.width, page.height)
        if any(not contains(page_bounds, span.bbox) for span in spans):
            return "span_outside_page", None
        if page.page_index >= document.page_count:
            return "page_geometry_mismatch", None
        source_page = document.load_page(page.page_index)
        if source_page.rotation != 0 or _bounds(source_page.rect) != page_bounds:
            return "page_geometry_mismatch", None
        for info in source_page.get_image_info():
            x0, y0, x1, y1 = _bounds(info["bbox"])
            share = max(x1 - x0, 0.0) * max(y1 - y0, 0.0) / (page.width * page.height)
            if image_signature(info) in stats.decoration_images and (
                share <= IMAGE_DECORATION_MAX_SHARE
            ):
                continue
            return "has_image", None
        detected, emitted = self._tables(source_page, page)
        for drawing in source_page.get_drawings():
            if drawing_signature(drawing) in stats.decoration_drawings:
                continue
            rect = _bounds(drawing["rect"])
            if any(
                contains(
                    (
                        bbox[0] - TABLE_BBOX_SLACK,
                        bbox[1] - TABLE_BBOX_SLACK,
                        bbox[2] + TABLE_BBOX_SLACK,
                        bbox[3] + TABLE_BBOX_SLACK,
                    ),
                    rect,
                )
                for bbox in detected
                if rect[0] < rect[2] and rect[1] < rect[3]
            ):
                continue
            if is_thin_ruling_drawing(drawing):
                continue
            if is_page_background(drawing, width=page.width, height=page.height):
                continue
            return "residual_graphics", None
        partition = self._partition_text(page, stats, emitted)
        if partition is None:
            return "ambiguous_columns", None
        try:
            validate_partition(page, partition)
        except ValueError:
            # 产出不满足覆盖/几何约束说明这页我们没读懂: 零代价回退, 不猜.
            return "partition_invalid", None
        return "ok", partition

    def _tables(
        self, source_page: pdfspine.Page, page: PageInput
    ) -> tuple[tuple[Bounds, ...], tuple[_EmittedTable, ...]]:
        """lines 策略命中且 span 归属与单元格完全一致的表; 其余区域一律按文本处理."""
        try:
            found = tuple(source_page.find_tables(strategy="lines"))
        except (pdfspine.PdfError, OSError, RuntimeError, TypeError, ValueError):
            return (), ()
        detected = tuple(_bounds(table.bbox) for table in found)
        emitted: list[_EmittedTable] = []
        for table in found:
            bbox = _bounds(table.bbox)
            if table.row_count < MIN_TABLE_ROWS or table.col_count < MIN_TABLE_COLS:
                continue
            if not (
                0.0 <= bbox[0] < bbox[2] <= page.width and 0.0 <= bbox[1] < bbox[3] <= page.height
            ):
                continue
            inside = tuple(span for span in page.text.spans if _center_inside(span.bbox, bbox))
            cells = tuple(table.origin_cells)
            consistent = bool(inside) and all(
                any(_center_inside(span.bbox, _bounds(cell.bbox)) for cell in cells)
                for span in inside
            )
            consistent = consistent and all(
                any(_center_inside(span.bbox, _bounds(cell.bbox)) for span in inside)
                for cell in cells
                if cell.state == "present"
            )
            if consistent:
                emitted.append(_EmittedTable(bbox, tuple(span.span_id for span in inside)))
        kept = tuple(
            table
            for index, table in enumerate(emitted)
            if not any(
                _overlaps(table.bbox, other.bbox)
                for position, other in enumerate(emitted)
                if position != index
            )
        )
        return detected, kept

    def _partition_text(
        self, page: PageInput, stats: _DocumentStats, tables: tuple[_EmittedTable, ...]
    ) -> PagePartition | None:
        table_span_ids = {span_id for table in tables for span_id in table.span_ids}
        lines = text_lines(span for span in page.text.spans if span.span_id not in table_span_ids)
        split = split_margin_lines(lines, page_height=page.height, running=stats.running)
        layout = column_layout(split.body)
        if layout.mode == "ambiguous":
            return None
        if layout.mode == "columns":
            assert layout.boundary is not None
            body_spans = [span for line in split.body for span in line.spans]
            sequences = [
                text_lines(
                    span
                    for span in body_spans
                    if ((span.bbox[0] + span.bbox[2]) / 2 < layout.boundary) is left
                )
                for left in (True, False)
            ]
        else:
            sequences = [split.body]
        body_size = body_font_size(split.body)
        specs: list[tuple[float, float, ObjectKind, BlockSpec | _EmittedTable, str]] = []
        for column, sequence in enumerate(sequences):
            for block in body_blocks(sequence, body_size=body_size):
                kind = ObjectKind.LIST if block.kind == "list" else ObjectKind.TEXT
                role = (
                    "Deterministic list block (bullet or numbered items)"
                    if block.kind == "list"
                    else "Deterministic text block (heading with following paragraphs)"
                )
                specs.append((float(column), block.lines[0].top, kind, block, role))
        for table in tables:
            specs.append((0.0, table.bbox[1], ObjectKind.TABLE, table, "Ruled table"))
        specs.sort(key=lambda item: (item[0], item[1]))
        objects: list[LayoutObject] = []
        for line in split.header:
            objects.append(self._margin_object(page, line, "header"))
        for _column, _top, kind, payload, role in specs:
            if isinstance(payload, _EmittedTable):
                objects.append(
                    self._object(page, ObjectKind.TABLE, payload.bbox, payload.span_ids, role)
                )
            else:
                span_ids = tuple(span.span_id for line in payload.lines for span in line.spans)
                bbox = _union(tuple(span.bbox for line in payload.lines for span in line.spans))
                objects.append(
                    self._object(
                        page,
                        kind,
                        bbox,
                        span_ids,
                        role,
                        list_items=tuple(
                            tuple(span.span_id for line in item for span in line.spans)
                            for item in payload.items
                        )
                        if kind is ObjectKind.LIST
                        else (),
                        list_ordered=payload.ordered if kind is ObjectKind.LIST else None,
                    )
                )
        for line in split.footer:
            objects.append(self._margin_object(page, line, "footer"))
        return PagePartition(
            "layout-enrichment-v2",
            page.source_manifest_id,
            page.source_sha256,
            page.page_index,
            self.deterministic_fingerprint,
            tuple(objects),
            (),
            diagnostics=(
                _DETERMINISTIC_MARK,
                f"objects={len(objects)}",
                f"tables={len(tables)}",
                f"layout={layout.mode}",
            ),
        )

    def _margin_object(self, page: PageInput, line: TextLine, band: str) -> LayoutObject:
        role = f"Page number ({band} band)" if is_page_number(line.text) else f"Running page {band}"
        return self._object(
            page,
            ObjectKind.TEXT,
            _union(tuple(span.bbox for span in line.spans)),
            line.span_ids,
            role,
        )

    def _object(
        self,
        page: PageInput,
        kind: ObjectKind,
        bbox: Bounds,
        span_ids: tuple[str, ...],
        role: str,
        *,
        list_items: tuple[tuple[str, ...], ...] = (),
        list_ordered: bool | None = None,
    ) -> LayoutObject:
        return LayoutObject(
            content_id(
                "layout-object-v1",
                (page.source_sha256, page.page_index, kind.value, bbox, span_ids),
            ),
            kind,
            bbox,
            span_ids,
            role,
            Confidence(None, "deterministic text-page partition from pdfspine blocks"),
            list_item_span_ids=list_items,
            list_ordered=list_ordered,
        )


def _union(boxes: tuple[Bounds, ...]) -> Bounds:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def make_text_page_partitioner(
    model: PagePartitioner, sources: LocalDocumentStore, snapshot: DocumentSnapshot
) -> PagePartitioner:
    """``"deterministic-text-pages"`` 策略的组合切分器(接入点只在这一处选择)."""
    return TextPageRouterPartitioner(model, sources, snapshot)


def partition_counts(outputs: ProcessingStore, manifest: ProcessingManifest) -> PartitionCounts:
    """从已存版面产物统计确定性页 / 回退页与原因码分布(缓存重放下同样成立)."""
    deterministic = 0
    reasons: Counter[str] = Counter()
    for record in manifest.pages:
        outcome = record.partition
        if outcome.state is not StageState.SUCCEEDED or outcome.artifact is None:
            continue
        partition = TypeAdapter(PagePartition).validate_json(outputs.assets.get(outcome.artifact))
        if partition.producer.startswith(DETERMINISTIC_PRODUCER_PREFIX):
            deterministic += 1
            continue
        for diagnostic in partition.diagnostics:
            if diagnostic.startswith(_FALLBACK_MARK) and diagnostic.endswith(")"):
                reasons[diagnostic[len(_FALLBACK_MARK) : -1]] += 1
                break
    return PartitionCounts(deterministic, sum(reasons.values()), dict(reasons))
