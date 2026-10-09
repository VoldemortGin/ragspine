"""不调模型的兜底版面: 只按文字层把一页的 span 聚行、按竖直空白分块, 每块一个 Text 对象.

ADR 0039(layout-fallback). ``layout_fallback`` 为 ``"onnx-accept"`` / ``"text-only"`` 时, 它
顶替路由切分器(ADR 0028 确定性分诊 / ADR 0030 ONNX)回退位上的模型版面: 拿不准的页不再每页
一次 VLM 调用, 而是在这里零调用地产出划分——页永远不会停在"需要模型"而丢掉.

代价: 只出 Text 对象, 页上的图表 / 图片 / 公式不成对象(图表数字在这些页不可答), 分栏页的
左右栏可能并进同一块(只影响检索粒度, 逐字内容不变). 不变量同另两个切分器: 对象文字全部来自
span 逐字内容(只分组, 从不改写), 每个 span 都有归属(落在页外的 span 显式记为未归属), 产物通过
``validate_partition``; 诊断只记 code 与计数, 不记正文.
"""

from enterprise_pdf_rag.adapters.deterministic_partition_geometry import (
    HEADING_SIZE_RATIO,
    PARAGRAPH_GAP_FACTOR,
    TextLine,
    body_font_size,
    text_lines,
)
from ragspine.extraction.evidence.document.models import Bounds, DocumentSnapshot
from ragspine.extraction.evidence.figures.models import Confidence, content_id
from ragspine.extraction.evidence.page.geometry import contains
from ragspine.extraction.evidence.page.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    PagePartition,
)
from ragspine.extraction.evidence.page.service import validate_partition

# 文本兜底产物的 producer(同时是切分器指纹); 与模型 / 确定性 / ONNX 产物天然区分.
TEXT_BLOCK_PRODUCER_PREFIX = "page-layout-text-blocks-v1"
# 每份文本兜底产物的首条诊断: 这页的版面没用任何模型, 只按文字层切块.
TEXT_BLOCK_MARK = "text-partition: ok"


def _blocks(lines: tuple[TextLine, ...]) -> list[list[TextLine]]:
    """相邻行竖直间距超过行字号的 ``PARAGRAPH_GAP_FACTOR`` 倍、或遇到标题行, 就开新块."""
    body_size = body_font_size(lines)
    blocks: list[list[TextLine]] = []
    previous: TextLine | None = None
    for line in lines:
        heading = body_size > 0 and line.size >= body_size * HEADING_SIZE_RATIO
        gap = previous is not None and line.top - previous.bottom > PARAGRAPH_GAP_FACTOR * max(
            line.size, previous.size, 1.0
        )
        if previous is None or gap or heading:
            blocks.append([line])
        else:
            blocks[-1].append(line)
        previous = line
    return blocks


class TextBlockPartitioner:
    """零模型调用的版面: 文字层的行块即 Text 对象; 永远产出一份合法划分."""

    fingerprint = TEXT_BLOCK_PRODUCER_PREFIX

    def __init__(self, snapshot: DocumentSnapshot) -> None:
        self.snapshot = snapshot

    def partition(self, page: PageInput) -> PagePartition:
        if page.source_sha256 != self.snapshot.manifest.source.sha256:
            raise ValueError("Text-block partition got a page from another source")
        page_bounds = (0.0, 0.0, page.width, page.height)
        inside = [span for span in page.text.spans if contains(page_bounds, span.bbox)]
        outside = tuple(
            span.span_id for span in page.text.spans if not contains(page_bounds, span.bbox)
        )
        lines = text_lines(inside)
        objects = []
        for block in _blocks(lines):
            spans = [span for line in block for span in line.spans]
            span_ids = tuple(span.span_id for span in spans)
            bbox: Bounds = (
                min(span.bbox[0] for span in spans),
                min(span.bbox[1] for span in spans),
                max(span.bbox[2] for span in spans),
                max(span.bbox[3] for span in spans),
            )
            objects.append(
                LayoutObject(
                    content_id(
                        "layout-object-v1",
                        (
                            page.source_sha256,
                            page.page_index,
                            ObjectKind.TEXT.value,
                            bbox,
                            span_ids,
                        ),
                    ),
                    ObjectKind.TEXT,
                    bbox,
                    span_ids,
                    "Text block (text-layer fallback, no layout model)",
                    Confidence(None, "text-layer line blocks; no layout model"),
                )
            )
        partition = PagePartition(
            "layout-enrichment-v2",
            page.source_manifest_id,
            page.source_sha256,
            page.page_index,
            self.fingerprint,
            tuple(objects),
            outside,
            diagnostics=(
                TEXT_BLOCK_MARK,
                f"objects={len(objects)}",
                f"lines={len(lines)}",
                f"unassigned_spans={len(outside)}",
            ),
        )
        validate_partition(page, partition)
        return partition
