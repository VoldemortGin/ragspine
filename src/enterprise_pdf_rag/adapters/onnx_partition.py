"""含图页的本地 ONNX 版面切分: pdfspine ``find_layout()``(PP-DocLayoutV3)替代每页一次的模型版面调用.

ADR 0030(onnx-layout-partitioner). ``"onnx-layout"`` 策略下组合顺序为: 纯文字页由 ADR 0028 的
确定性切分器零调用处理 → 其余页由本模块用 pdfspine 内置的本地 ONNX 版面模型(PP-DocLayoutV3,
进程内推理, 零 LLM 调用)产出与 ``page-layout-v2`` 同 schema 的对象划分 → 拿不准的页带机器
可读的原因码回退被包装的模型切分器(ADR 0013 修订 1 的原则: 读不懂的版面零代价回退, 不猜).

producer 含模型文件 sha256 前 12 位(``page-layout-onnx-v1:pdfspine/<ver>:<sha12>``), 模型换了
缓存自然隔离; onnxruntime 版本**不进**指纹——权重摘要已唯一标识所算的函数, ort 升级带来的数值
抖动远小于阈值粒度, 进指纹只会让每次 ort 升级作废全部已存产物(见 ADR 0030).

不变量: 对象里的文字全部来自 span 的逐字内容(本模块只分组, 从不改写), 每个 span 都有归属,
产物通过 ``validate_partition``; 诊断只记原因码与计数, 不记正文. Chart IR 仍只由模型分支生成
(用户决定); 本模块只提出"哪里是图表/表格/文本", 不读数.
"""

import importlib.util
import os
import shutil
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from hashlib import sha256
from math import hypot
from pathlib import Path

import pdfspine

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.shared_pdf import opened_pdf, source_pdf
from ragspine.common.evidence.configs import ROOT_DIR, get_settings
from ragspine.extraction.evidence.document.models import Bounds, DocumentSnapshot, TextSpan
from ragspine.extraction.evidence.figures.models import Confidence, content_id
from ragspine.extraction.evidence.page.geometry import contains
from ragspine.extraction.evidence.page.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    PagePartition,
)
from ragspine.extraction.evidence.page.ports import PagePartitioner
from ragspine.extraction.evidence.page.service import validate_partition

# ONNX 产物的 producer 前缀; 与模型("page-layout-mapper-v3:…")与确定性
# ("page-layout-deterministic-v1:…")产物天然区分.
ONNX_PRODUCER_PREFIX = "page-layout-onnx-v1"
# 版面模型文件名(PP-DocLayoutV3; 与 pdfspine 的默认文件名一致, 配置给目录时按它拼接).
ONNX_LAYOUT_MODEL_FILE = "pp_doc_layoutv3.onnx"
# pdfspine 自己的模型目录环境变量(ragspine 设置 APP_ONNX_LAYOUT_MODEL 优先, 此变量兜底).
ONNX_MODELS_ENV = "PDFSPINE_ONNX_MODELS"
# 什么都没配置时的权重目录(ADR 0039): notebook 的 onnx-check 格把权重下载到这里, 解析时最后找它;
# 在 data/ 下, 不进 git. 让 Databricks 上 pull 之后不配置任何变量也能用 ONNX 版面.
DEFAULT_ONNX_MODELS_DIR = ROOT_DIR / "data" / "models" / "pdfspine-onnx"
# 版面权重的下载地址(与 pdfspine ``_onnx.LAYOUT_MODEL_URL`` 一致; notebook 安装步骤用它).
ONNX_LAYOUT_MODEL_URL = (
    "https://www.modelscope.cn/models/RapidAI/RapidLayout/resolve/v1.2.0/"
    "onnx/pp_doc_layout/pp_doc_layoutv3.onnx"
)

# block 置信度下限: 达到它的框才进划分.
ONNX_MIN_BLOCK_SCORE = 0.5
# 可疑视觉框下限(透传给 pdfspine 的 ``layout_threshold``): [此值, ONNX_MIN_BLOCK_SCORE) 的
# 图表 / 表格 / 公式框不进划分, 但若没有任何已接受的视觉区覆盖它, 说明这页可能有图被漏掉,
# 整页回退模型而不是静默丢图. 依据: AIA 真实对照里 p18 右半页的柱状图只有 0.389 分, 单阈值
# 0.5 会把该页"干净地"切完、图表数字静默不可答——这是最坏的失败方向.
ONNX_SUSPECT_VISUAL_SCORE = 0.3
# 未归属 span(中心点不在任何 block 内)占比超过此值, 判定版面没读懂, 整页回退.
ONNX_MAX_UNASSIGNED_SPAN_SHARE = 0.2
# 推理渲染 DPI(pdfspine 默认同值; 显式写出, 它决定送入模型的页面图像).
ONNX_RENDER_DPI = 144

# pdfspine 归一化标签里的视觉类(可疑框守卫只看它们; 文本永不静默丢失, 由 span 覆盖规则兜底).
_VISUAL_LABELS = frozenset({"figure", "table", "isolate_formula"})

# 回退原因码(机器可读; 诊断里只出现这些码, 绝不出现正文).
ONNX_FALLBACK_REASONS = (
    "onnx_unavailable",
    "onnx_low_confidence",
    "onnx_overlapping_blocks",
    "onnx_unassigned_spans",
    "onnx_partition_invalid",
)
ONNX_MARK = "onnx-partition: ok"
# ADR 0039, ``layout_fallback="onnx-accept"``: 本该 ``onnx_low_confidence`` 回退的页接受了 ONNX
# 返回的全部框(含 [0.3, 0.5) 的低分框), 这条码记在该页诊断里.
ONNX_ACCEPTED_LOW_CONFIDENCE = "onnx_low_confidence_accepted"
ONNX_FALLBACK_MARK = "onnx-partition: model fallback ("

# 测试缝: 可注入的 find_spec(模拟 onnxruntime 缺失).
_find_spec = importlib.util.find_spec


def _runtime_missing() -> str | None:
    """缺失的推理依赖名; 全部可导入时为 ``None``(真正加载仍由 pdfspine 延迟完成)."""
    for module in ("onnxruntime", "numpy", "PIL"):
        if _find_spec(module) is None:
            return module
    return None


def _runtime_problem() -> str | None:
    """缺推理依赖时的中文报错文本; 依赖齐全为 ``None``."""
    missing = _runtime_missing()
    if missing is None:
        return None
    return (
        f'"onnx-layout" 版面策略需要可导入的推理依赖, 当前缺少 {missing}。请安装 '
        "`pip install 'pdfspine[onnx]'`(即 onnxruntime / numpy / Pillow)。缺依赖时不做"
        "静默的逐页回退, 以免看起来省了模型版面调用、实际上一页都没省。"
    )


def onnx_layout_unavailable(configured: str | None) -> str | None:
    """``"onnx-layout"`` 此刻能否启用: 能为 ``None``, 否则给中文原因(权重未配置 / 不存在 / 缺依赖).

    与 ``make_onnx_page_partitioner`` 的入库前预检是同一判断(同一解析顺序、同一报错文本),
    只查文件存在与依赖可导入, 不读权重、不导入 onnxruntime、不加载模型; 供 notebook 的
    ``LAYOUT_POLICY = "auto"`` 选择策略.
    """
    try:
        resolve_onnx_layout_model(configured)
    except ValueError as error:
        return str(error)
    return _runtime_problem()


def _configured_target(configured: str | None) -> Path | None:
    """权重该在的位置(不查存在): 显式配置的文件 / 目录, 其次 ``PDFSPINE_ONNX_MODELS``; 都没有为 None."""
    value = (configured or "").strip()
    if value:
        path = Path(value).expanduser()
        return path / ONNX_LAYOUT_MODEL_FILE if path.is_dir() or not path.suffix else path
    root = os.environ.get(ONNX_MODELS_ENV, "").strip()
    return Path(root).expanduser() / ONNX_LAYOUT_MODEL_FILE if root else None


def _download(url: str, target: Path) -> None:
    with urllib.request.urlopen(url, timeout=300) as response, target.open("wb") as sink:
        shutil.copyfileobj(response, sink)


def _fetch(label: str, url: str, target: Path, download: Callable[[str, Path], None]) -> str:
    """缺文件就下载到 ``target``(先写 ``.part`` 再改名); 返回一行中文说明, 失败不抛异常."""
    if target.is_file():
        return f"{label}: 已存在 {target}"
    partial = target.with_name(target.name + ".part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        download(url, partial)
        partial.replace(target)
    except OSError as error:
        partial.unlink(missing_ok=True)
        return f"{label}: 下载失败({type(error).__name__}: {error})。请手工下载 {url} 放到 {target}"
    return f"{label}: 已下载到 {target}"


def ensure_onnx_layout_weights(
    configured: str | None, *, download: Callable[[str, Path], None] = _download
) -> str:
    """notebook 安装步骤: 版面权重与表格结构权重缺文件就下载; 返回中文说明(每个文件一行).

    版面权重的位置: 已配置的 ``APP_ONNX_LAYOUT_MODEL`` / ``PDFSPINE_ONNX_MODELS``, 都没有时
    ``DEFAULT_ONNX_MODELS_DIR``(解析时最后也找它); SLANet-plus(ADR 0031)放在同一目录, 正是
    ``pdfspine_tsr`` 找它的地方. 下载失败不抛异常, 给出地址让人手工放置——之后的自检行与
    ``"auto"`` 选择会如实显示它仍不可用.
    """
    from enterprise_pdf_rag.adapters import pdfspine_tsr

    target = _configured_target(configured) or DEFAULT_ONNX_MODELS_DIR / ONNX_LAYOUT_MODEL_FILE
    return "\n".join(
        (
            _fetch("ONNX 版面权重", ONNX_LAYOUT_MODEL_URL, target, download),
            _fetch(
                "表格结构权重",
                pdfspine_tsr.MODEL_URL,
                target.parent / pdfspine_tsr.MODEL_FILE,
                download,
            ),
        )
    )


def onnx_layout_status(configured: str | None) -> str:
    """notebook 开头的自检行: ONNX 版面是否可用 + 权重路径; 不可用时附带安装 / 配置提示."""
    target = _configured_target(configured)
    default = DEFAULT_ONNX_MODELS_DIR / ONNX_LAYOUT_MODEL_FILE
    if target is not None:
        weights = str(target)
    elif default.is_file():
        weights = str(default)
    else:
        weights = f"未配置(默认位置 {default} 也没有)"
    problem = onnx_layout_unavailable(configured)
    if problem is None:
        return f"ONNX 版面自检: 可用=是, 权重={weights}"
    return f"ONNX 版面自检: 可用=否, 权重={weights}。{problem}"


def resolve_onnx_layout_model(configured: str | None) -> Path:
    """解析版面模型文件路径: 显式配置(文件或目录)优先, 其次 ``PDFSPINE_ONNX_MODELS`` 目录,
    最后 ``DEFAULT_ONNX_MODELS_DIR``(ADR 0039, 只在文件存在时).

    找不到就报错而不是静默回退: 静默回退会让每页都走模型版面, 用户以为省了调用实际没省.
    """
    value = (configured or "").strip()
    if value:
        path = Path(value).expanduser()
        if path.is_dir():
            path = path / ONNX_LAYOUT_MODEL_FILE
        if not path.is_file():
            raise ValueError(
                f"ONNX 版面模型文件不存在: {path}。请把 APP_ONNX_LAYOUT_MODEL 设为 "
                f"{ONNX_LAYOUT_MODEL_FILE} 文件本身或它所在的目录"
                "(Databricks 上用 Volume 里的绝对路径), 权重不随任何 wheel 分发。"
            )
        return path
    root = os.environ.get(ONNX_MODELS_ENV, "").strip()
    if root:
        path = Path(root).expanduser() / ONNX_LAYOUT_MODEL_FILE
        if not path.is_file():
            raise ValueError(
                f"ONNX 版面模型文件不存在: {path}。环境变量 {ONNX_MODELS_ENV} 指向的目录里"
                f"没有 {ONNX_LAYOUT_MODEL_FILE}; 请下载权重放进该目录, 或改设 "
                "APP_ONNX_LAYOUT_MODEL 指向模型文件。"
            )
        return path
    default = DEFAULT_ONNX_MODELS_DIR / ONNX_LAYOUT_MODEL_FILE
    if default.is_file():
        return default
    raise ValueError(
        '"onnx-layout" 版面策略需要本地 PP-DocLayoutV3 模型文件, 但没有配置路径。'
        f"请设置 APP_ONNX_LAYOUT_MODEL 指向 {ONNX_LAYOUT_MODEL_FILE}(或其所在目录; "
        f"Databricks 上放 Volume 并用绝对路径), 或设置环境变量 {ONNX_MODELS_ENV} 指向模型"
        "目录, 或运行 notebook 的 onnx-check 格把权重自动下载到默认位置 "
        f"{default}。权重从 pdfspine 文档记录的 ModelScope(RapidAI)地址下载, 不随 wheel 分发。"
    )


@dataclass(frozen=True, slots=True)
class _Region:
    """一个 ONNX block 映射出的候选对象(span 归属前)."""

    kind: ObjectKind
    bbox: Bounds
    interpretation: str


def _region_from_block(
    block: pdfspine.LayoutBlock, *, width: float, height: float
) -> _Region | None:
    """PP-DocLayoutV3 标签 -> 现有 ``LayoutObject`` 类型; 越界裁剪, 零面积丢弃.

    判定规则(pdfspine 已把模型类归一化为 ``label``, 原始类保留在 ``raw_label``):

    - ``table`` -> Table(网格证明 / 逐字转录照旧决定数值资格, ADR 0014).
    - ``figure``: 原始类 ``image`` / ``seal`` -> Image(永不可检索, lite 下零调用);
      其余(``chart``, 以及未来未知的 figure 原始类)保守地 -> Chart——Chart 由后续模型
      分支取 IR 并自行判定, 错标成本是两次模型调用, 而错标成 Image 的成本是图表数字
      永久不可答, 所以拿不准时宁可交给模型.
    - ``isolate_formula`` -> Formula(模型零调用的纯证明, ADR 0015).
    - 页眉 / 页脚 / 页码(``abandon`` 中带文字的原始类) -> Text 并在 interpretation 里
      带角色标记(schema 没有专门字段; 与确定性切分器的做法一致, span 绝不丢弃);
      ``header_image`` / ``footer_image`` -> Image.
    - 标题 / 正文 / 题注 / 脚注 -> Text. PP-DocLayoutV3 没有列表类, List 的分项 schema
      (每项的 span 分组)无从满足, 列表一律按 Text 收录(逐字内容不变, 只粗一级).
    """
    bbox = (
        min(max(float(block.bbox.x0), 0.0), width),
        min(max(float(block.bbox.y0), 0.0), height),
        min(max(float(block.bbox.x1), 0.0), width),
        min(max(float(block.bbox.y1), 0.0), height),
    )
    if bbox[2] - bbox[0] <= 0 or bbox[3] - bbox[1] <= 0:
        return None
    label = str(block.label)
    raw = str(block.raw_label or label)
    if label == "table":
        return _Region(ObjectKind.TABLE, bbox, "Table region (ONNX layout: table)")
    if label == "figure":
        if raw in ("image", "seal"):
            return _Region(ObjectKind.IMAGE, bbox, f"Image region (ONNX layout: {raw})")
        return _Region(ObjectKind.CHART, bbox, f"Chart region (ONNX layout: {raw})")
    if label == "isolate_formula":
        return _Region(ObjectKind.FORMULA, bbox, f"Formula region (ONNX layout: {raw})")
    if label == "abandon":
        if raw in ("header_image", "footer_image"):
            return _Region(ObjectKind.IMAGE, bbox, f"Image region (ONNX layout: {raw})")
        role = {
            "header": "Running page header",
            "footer": "Running page footer",
            "number": "Page number",
        }.get(raw, "Page furniture")
        return _Region(ObjectKind.TEXT, bbox, f"{role} (ONNX layout: {raw})")
    if label == "title":
        return _Region(ObjectKind.TEXT, bbox, f"Heading (ONNX layout: {raw})")
    if label == "figure_caption":
        return _Region(ObjectKind.TEXT, bbox, "Figure caption (ONNX layout)")
    if label == "table_caption":
        return _Region(ObjectKind.TEXT, bbox, "Table caption (ONNX layout)")
    if label == "table_footnote":
        return _Region(ObjectKind.TEXT, bbox, "Table footnote (ONNX layout)")
    if label == "formula_caption":
        return _Region(ObjectKind.TEXT, bbox, "Formula number (ONNX layout)")
    if label == "plain text" and raw == "footnote":
        return _Region(ObjectKind.TEXT, bbox, "Page footnote (ONNX layout)")
    # 正文与一切未知标签: 按 Text 收录(span 逐字进对象, 未知类型只影响粒度不影响证据链).
    return _Region(ObjectKind.TEXT, bbox, f"Text block (ONNX layout: {raw})")


def _center(bbox: Bounds) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2


def _center_inside(inner: Bounds, outer: Bounds) -> bool:
    # 与 adapters/pdfspine_tables._center_inside 逐字一致: 下游表格证明用同一归属判据.
    x, y = _center(inner)
    return outer[0] <= x <= outer[2] and outer[1] <= y <= outer[3]


def _rect_distance(point: tuple[float, float], rect: Bounds) -> float:
    dx = max(rect[0] - point[0], 0.0, point[0] - rect[2])
    dy = max(rect[1] - point[1], 0.0, point[1] - rect[3])
    return hypot(dx, dy)


def _union(first: Bounds, second: Bounds) -> Bounds:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _clamp(bbox: Bounds, *, width: float, height: float) -> Bounds:
    return (
        min(max(bbox[0], 0.0), width),
        min(max(bbox[1], 0.0), height),
        min(max(bbox[2], 0.0), width),
        min(max(bbox[3], 0.0), height),
    )


@dataclass(slots=True)
class _Assignment:
    regions: list[_Region]
    spans: dict[int, list[TextSpan]] = field(default_factory=dict)
    merged: int = 0


def _assign_spans(page: PageInput, regions: list[_Region]) -> tuple[str, _Assignment | None]:
    """span 按中心点归属 block; 嵌套取最内层, 真重叠或大量落空则整页回退.

    - 中心点恰在一个 block 内: 归属它.
    - 在多个 block 内: 仅当最小的候选框被其余每个候选框完全包含(纯嵌套, 例如表格题注框在
      表格框内)时取最内层; 两个部分重叠的框之间说明模型版面自相矛盾, 回退
      ``onnx_overlapping_blocks``.
    - 不在任何 block 内: 并入(按矩形距离)最近的 Text 块, 对象 bbox 取并集; 落空 span 超过
      ``ONNX_MAX_UNASSIGNED_SPAN_SHARE`` 或页上没有 Text 块可并时回退
      ``onnx_unassigned_spans``——与模型版面不同, 本切分器绝不产出 unassigned span.
    """
    assignment = _Assignment(regions)
    pending: list[TextSpan] = []
    for span in page.text.spans:
        candidates = [
            index for index, region in enumerate(regions) if _center_inside(span.bbox, region.bbox)
        ]
        if len(candidates) > 1:
            inner = min(
                candidates,
                key=lambda index: (
                    (regions[index].bbox[2] - regions[index].bbox[0])
                    * (regions[index].bbox[3] - regions[index].bbox[1])
                ),
            )
            if not all(
                contains(regions[index].bbox, regions[inner].bbox)
                for index in candidates
                if index != inner
            ):
                return "onnx_overlapping_blocks", None
            candidates = [inner]
        if candidates:
            assignment.spans.setdefault(candidates[0], []).append(span)
        else:
            pending.append(span)
    if pending:
        if len(pending) > ONNX_MAX_UNASSIGNED_SPAN_SHARE * len(page.text.spans):
            return "onnx_unassigned_spans", None
        text_regions = [
            index for index, region in enumerate(regions) if region.kind is ObjectKind.TEXT
        ]
        if not text_regions:
            return "onnx_unassigned_spans", None
        for span in pending:
            point = _center(span.bbox)
            nearest = min(
                text_regions, key=lambda index: _rect_distance(point, regions[index].bbox)
            )
            assignment.spans.setdefault(nearest, []).append(span)
            assignment.regions[nearest] = replace(
                regions[nearest],
                bbox=_clamp(
                    _union(regions[nearest].bbox, span.bbox), width=page.width, height=page.height
                ),
            )
            assignment.merged += 1
    return "ok", assignment


class OnnxPagePartitioner:
    """组合切分器: ONNX 版面可信的页零 LLM 调用, 其余页原样交给被包装的模型切分器."""

    def __init__(
        self,
        model: PagePartitioner,
        snapshot: DocumentSnapshot,
        *,
        producer: str,
        blocks_for: Callable[[PageInput], Sequence[pdfspine.LayoutBlock]],
        accept_low_confidence: bool = False,
    ) -> None:
        self.model = model
        self.snapshot = snapshot
        self.producer = producer
        # ADR 0039: 接受低置信结果的路由产出不同的划分, 指纹随之区分(默认逐字节不变).
        accept = ";accept-low-confidence" if accept_low_confidence else ""
        self.fingerprint = f"page-layout-onnx-router-v1:{producer}{accept};{model.fingerprint}"
        self._blocks_for = blocks_for
        self._accept_low_confidence = accept_low_confidence

    def partition(self, page: PageInput) -> PagePartition:
        if page.source_sha256 != self.snapshot.manifest.source.sha256:
            raise ValueError("ONNX partition got a page from another source")
        reason, partition = self._onnx(page)
        if partition is not None:
            return partition
        fallback = self.model.partition(page)
        return replace(
            fallback, diagnostics=(*fallback.diagnostics, f"{ONNX_FALLBACK_MARK}{reason})")
        )

    def _onnx(self, page: PageInput) -> tuple[str, PagePartition | None]:
        try:
            blocks = tuple(self._blocks_for(page))
        except (pdfspine.PdfError, OSError, RuntimeError, TypeError, ValueError):
            # 构造切分器时已做过可用性预检并明确报错; 走到这里是单页推理 / 渲染失败,
            # 按页回退并计入原因分布, 让用户看得见而不是悄悄变成全模型.
            return "onnx_unavailable", None
        regions = self._regions(page, blocks, ONNX_MIN_BLOCK_SCORE)
        accepted: tuple[str, ...] = ()
        if not regions or self._unexplained_suspect(page, blocks, regions):
            # 没有达到阈值的框(版面没读懂), 或低分视觉框没有任何已接受的视觉区覆盖(可能漏图):
            # 默认交回被包装的切分器; "onnx-accept" 改为接受 ONNX 返回的全部框(低分图表框也成
            # 对象, 不丢图), 完全没框时仍交回去(由文本切块兜底).
            regions = self._regions(page, blocks, 0.0) if self._accept_low_confidence else []
            if not regions:
                return "onnx_low_confidence", None
            accepted = (ONNX_ACCEPTED_LOW_CONFIDENCE,)
        reason, assignment = _assign_spans(page, regions)
        if assignment is None:
            return reason, None
        objects = tuple(
            self._object(page, region, tuple(assignment.spans.get(index, ())))
            for index, region in enumerate(assignment.regions)
        )
        partition = PagePartition(
            "layout-enrichment-v2",
            page.source_manifest_id,
            page.source_sha256,
            page.page_index,
            self.producer,
            objects,
            (),
            diagnostics=(
                ONNX_MARK,
                f"objects={len(objects)}",
                f"blocks={len(blocks)}",
                f"merged_spans={assignment.merged}",
                *accepted,
            ),
        )
        try:
            validate_partition(page, partition)
        except ValueError:
            # 产出不满足覆盖/几何约束说明这页我们没读懂: 零代价回退, 不猜.
            return "onnx_partition_invalid", None
        return "ok", partition

    @staticmethod
    def _regions(
        page: PageInput, blocks: tuple[pdfspine.LayoutBlock, ...], min_score: float
    ) -> list[_Region]:
        return [
            region
            for block in blocks
            if float(block.score) >= min_score
            and (region := _region_from_block(block, width=page.width, height=page.height))
            is not None
        ]

    def _unexplained_suspect(
        self,
        page: PageInput,
        blocks: tuple[pdfspine.LayoutBlock, ...],
        regions: list[_Region],
    ) -> bool:
        """是否存在没被任何已接受视觉区覆盖的可疑视觉框([0.3, 0.5) 的图 / 表 / 公式).

        "覆盖"按中心点互指: 可疑框中心落在某个已接受视觉区内(重复检测), 或某个已接受视觉区
        中心落在可疑框内(同一图的松框). 两者都不成立的可疑框指向一张可能被漏掉的图.
        """
        visuals = [
            region
            for region in regions
            if region.kind
            in (ObjectKind.TABLE, ObjectKind.CHART, ObjectKind.IMAGE, ObjectKind.FORMULA)
        ]
        for block in blocks:
            score = float(block.score)
            if not ONNX_SUSPECT_VISUAL_SCORE <= score < ONNX_MIN_BLOCK_SCORE:
                continue
            if str(block.label) not in _VISUAL_LABELS:
                continue
            suspect = _clamp(
                (
                    float(block.bbox.x0),
                    float(block.bbox.y0),
                    float(block.bbox.x1),
                    float(block.bbox.y1),
                ),
                width=page.width,
                height=page.height,
            )
            if suspect[2] - suspect[0] <= 0 or suspect[3] - suspect[1] <= 0:
                continue
            explained = any(
                _center_inside(suspect, visual.bbox) or _center_inside(visual.bbox, suspect)
                for visual in visuals
            )
            if not explained:
                return True
        return False

    def _object(
        self, page: PageInput, region: _Region, spans: tuple[TextSpan, ...]
    ) -> LayoutObject:
        span_ids = tuple(span.span_id for span in spans)
        return LayoutObject(
            content_id(
                "layout-object-v1",
                (page.source_sha256, page.page_index, region.kind.value, region.bbox, span_ids),
            ),
            region.kind,
            region.bbox,
            span_ids,
            region.interpretation,
            Confidence(None, "uncalibrated onnx layout detection (pp_doclayoutv3)"),
        )


def layout_blocks(
    document: pdfspine.Document, page: PageInput, options: Mapping[str, object]
) -> Sequence[pdfspine.LayoutBlock]:
    """一页的 ONNX 版面 block(测试缝: 桩测试替换本函数而不触碰真模型)."""
    if page.page_index >= document.page_count:
        raise ValueError("ONNX partition got a page beyond the source document")
    # ``Page.find_layout`` 以关键字参数接收 ``OnnxOptions`` 字段(**vision_options), 不是
    # ``options=``——传错名字会被当成未知选项而 TypeError(真模型对照时实测钉住).
    return document.load_page(page.page_index).find_layout(**dict(options))


def make_onnx_page_partitioner(
    model: PagePartitioner,
    sources: LocalDocumentStore,
    snapshot: DocumentSnapshot,
    *,
    layout_model: str | None = None,
    accept_low_confidence: bool = False,
) -> PagePartitioner:
    """``"onnx-layout"`` 策略的 ONNX 切分器; 在入库开始前完成可用性预检, 绝不静默回退.

    ``layout_model`` 为 ``None`` 时读 settings 的 ``onnx_layout_model``(env
    ``APP_ONNX_LAYOUT_MODEL``), 再退回 ``PDFSPINE_ONNX_MODELS``. 模型文件 sha256 前 12 位进
    producer, 模型换了缓存自然隔离. 每份文档一个切分器实例; pdfspine 按(模型路径, provider,
    变体)缓存 onnxruntime 会话, 逐页推理绝不重新加载模型; PDF 经 ``shared_pdfs()`` 作用域
    只读打开一次.
    """
    configured = layout_model if layout_model is not None else get_settings().onnx_layout_model
    path = resolve_onnx_layout_model(configured)
    problem = _runtime_problem()
    if problem is not None:
        raise ValueError(problem)
    digest = sha256(path.read_bytes()).hexdigest()[:12]
    producer = f"{ONNX_PRODUCER_PREFIX}:pdfspine/{pdfspine.__version__}:{digest}"
    options: dict[str, object] = {
        "layout_model": os.fspath(path),
        # 按可疑下限请求检测: [0.3, 0.5) 的视觉框只做漏图守卫, >=0.5 的才进划分.
        "layout_threshold": ONNX_SUSPECT_VISUAL_SCORE,
        "dpi": ONNX_RENDER_DPI,
    }

    def blocks_for(page: PageInput) -> Sequence[pdfspine.LayoutBlock]:
        pdf = source_pdf(sources, snapshot)
        with opened_pdf(pdf) as document:
            return layout_blocks(document, page, options)

    return OnnxPagePartitioner(
        model,
        snapshot,
        producer=producer,
        blocks_for=blocks_for,
        accept_low_confidence=accept_low_confidence,
    )
