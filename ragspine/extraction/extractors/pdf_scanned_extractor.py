"""扫描型 PDF OCR/VLM 抽取契约（三期扫描线，story #10）—— 两层策略的核心。

PRD user story 10：扫描型 PDF 经 OCR/VLM 抽取且每个值带置信度，低置信结果被自动
拦截而不是混入事实表。运行时环境拍板（2026-06-12）：生产跑 Ubuntu + NVIDIA GPU
（PaddleOCR-VL 一线支持环境），Mac 仅作开发机。

测试分两层（与 PRD「Further Notes」一致）：
    ①「模型无关」的逻辑（pypdfium2 渲染、OcrTable -> StyledGrid 映射、置信度阈值分流、
       复核队列集成）—— 用可注入的 fake backend 在任何机器上跑 TDD。
    ②真实模型（PaddleOcrVlBackend）的集成 —— 打 pytest `gpu` marker，只在 Ubuntu GPU 跑。

为支持第①层，extract_grids 接受任意实现 OcrBackend 协议的后端（依赖注入）：真实
后端在 Ubuntu 上用 PaddleOcrVlBackend，本地逻辑测试用 FakeBackend。

中立数据类（不耦合任何具体 OCR 实现）：
    OcrCell / OcrTable / OcrPageResult —— backend.recognize 的返回契约，
    与 StyledGrid 解耦，便于替换底层 OCR 引擎。

命名与坐标约定（与 pdf_digital_extractor / pptx_styled_extractor 对齐）：
    - sheet 命名 'page{N}_table{M}'（N=页号 1-based，M=该页表序 1-based）。
    - cell_ref 用 'R{行}C{列}'（1-based）。
    - value = 空白归一化后的单元格文本（字符串）。
    - resolved_rgb 恒为 None（扫描线不做颜色语义）。
    - StyledCell.confidence = OCR 置信度（OCR 通道专用字段）。

依赖约定（重要）：
    paddleocr / paddle 是重依赖且仅在 Ubuntu+GPU 可用，本模块顶部**不得直接 import**
    paddle / paddleocr —— PaddleOcrVlBackend 在构造 / recognize 时才惰性 import，
    保证仅安装基础依赖时本契约仍可被 import（import 必须成功）。

实现已完成，dataclass 字段契约保持冻结，行为契约见 tests/extraction/extractors/test_pdf_scanned_extractor.py
（fake backend 逻辑层）与 tests/extraction/extractors/test_scanned_gpu.py（真实模型集成，gpu marker）。
"""

import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pypdfium2 as pdfium

from ragspine.extraction.ir import StyledCell, StyledGrid

# 抽取器版本标识（写入血缘，便于回归门禁区分解析器版本）。
EXTRACTOR_VERSION = "pdf_scanned_paddleocrvl_v0"

# 渲染分辨率：扫描页位图渲染为 PNG 时的 DPI（越高 OCR 越清晰、越慢）。
RENDER_DPI = 200

# 低置信入队默认参数（与 review_queue 约定对齐）。
LOW_CONFIDENCE_REASON = "low_confidence_ocr"
LOW_CONFIDENCE_PRIORITY = 30


@dataclass
class OcrCell:
    """OCR 识别出的单个表格单元格（中立数据类，不耦合具体 OCR 实现）。

    字段语义约定：
        row:         行号（1-based）。
        col:         列号（1-based）。
        text:        识别出的文本（由 extract_grids 做空白归一化后写入 StyledCell）。
        confidence:  识别置信度 [0.0, 1.0]。
    """

    row: int
    col: int
    text: str
    confidence: float


@dataclass
class OcrTable:
    """OCR 识别出的一张表（中立数据类）。

    字段语义约定：
        n_rows:  表格逻辑行数。
        n_cols:  表格逻辑列数。
        cells:   OcrCell 列表（稀疏：可只含有内容的格）。
    """

    n_rows: int
    n_cols: int
    cells: list[OcrCell] = field(default_factory=list)


@dataclass
class OcrPageResult:
    """单页 OCR 识别结果（中立数据类，backend.recognize 的返回契约）。

    字段语义约定：
        page_no:   页号（1-based）。
        tables:    本页识别出的表列表。
        warnings:  本页 OCR 级告警（如「整页低置信」「未检出表格」）。
    """

    page_no: int
    tables: list[OcrTable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _ReviewQueue(Protocol):
    """低置信入队所需的接口（与 review_queue.ReviewQueue.enqueue 对齐）。"""

    def enqueue(
        self,
        reason: str,
        payload: dict[str, object],
        locator: str,
        priority: int = ...,
    ) -> int: ...


class OcrBackend(Protocol):
    """OCR/VLM 后端协议（依赖注入点）。

    任何实现本协议的对象都可传给 extract_grids：真实后端用 PaddleOcrVlBackend
    （Ubuntu+GPU），本地逻辑测试用 FakeBackend。把 backend 抽象成协议，使「渲染 +
    映射 + 阈值分流 + 复核集成」的逻辑可在无 GPU 的机器上完整测试（PRD 两层策略）。
    """

    def recognize(self, image_bytes: bytes, page_no: int) -> OcrPageResult:
        """识别单页图像（PNG bytes）-> OcrPageResult。"""
        ...


def _normalize_text(text: object) -> str:
    """空白归一化：首尾 strip + 内部连续空白折叠为单空格（与 pdf_digital 约定一致）。"""
    return " ".join(str(text).split())


def _file_hash(path: Path) -> str:
    """源文件内容 sha256（十六进制），作为版本与审计血缘锚点。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _render_pages_png(path: Path) -> list[bytes] | None:
    """用 pypdfium2 逐页渲染为 PNG bytes（1-based 页序）。

    不可读 / 空文件 -> 返回 None（调用方据此直接返回 [] 且不调 backend）；
    零页 PDF -> 返回 []（同样不会触发任何 OCR 调用）。
    """
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception:
        return None
    try:
        images: list[bytes] = []
        for idx in range(len(doc)):
            page = doc[idx]
            try:
                bitmap = page.render(scale=RENDER_DPI / 72)
                try:
                    pil_image = bitmap.to_pil()
                finally:
                    bitmap.close()
            finally:
                page.close()
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            images.append(buf.getvalue())
        return images
    except Exception:
        return None
    finally:
        doc.close()


def _build_grid(
    table: OcrTable,
    sheet: str,
    source_doc_id: str,
    source_file_hash: str,
    min_confidence: float,
    queue: _ReviewQueue | None,
) -> StyledGrid:
    """把一个 OcrTable 转成 StyledGrid（稀疏：只存 backend 给出的格）。

    cell_ref = 'R{行}C{列}'（1-based）。value 为空白归一化文本，resolved_rgb 恒为
    None，confidence = OCR 置信度。低于 min_confidence 的格仍入网格，但触发 grid
    warning，并在给了 queue 时按 reason='low_confidence_ocr' / priority=30 入队
    （payload 含 locator / text / confidence，供 SME 不翻原件即可核对）。
    """
    grid = StyledGrid(
        sheet=sheet,
        source_doc_id=source_doc_id,
        source_file_hash=source_file_hash,
        n_rows=table.n_rows,
        n_cols=table.n_cols,
    )
    has_low_confidence = False
    for cell in table.cells:
        ref = f"R{cell.row}C{cell.col}"
        value = _normalize_text(cell.text)
        grid.cells[ref] = StyledCell(
            value=value,
            cell_ref=ref,
            resolved_rgb=None,
            confidence=cell.confidence,
        )
        if cell.confidence < min_confidence:
            has_low_confidence = True
            locator = f"{sheet}!{ref}"
            if queue is not None:
                queue.enqueue(
                    reason=LOW_CONFIDENCE_REASON,
                    payload={
                        "locator": locator,
                        "text": value,
                        "confidence": cell.confidence,
                    },
                    locator=locator,
                    priority=LOW_CONFIDENCE_PRIORITY,
                )
    if has_low_confidence:
        grid.add_warning(
            f"{sheet}: 检测到低置信 OCR 格（confidence < {min_confidence}），已标记入复核"
        )
    return grid


def extract_grids(
    path: str | Path,
    backend: OcrBackend,
    *,
    min_confidence: float = 0.85,
    queue: _ReviewQueue | None = None,
) -> list[StyledGrid]:
    """抽取一个扫描型 PDF 的全部表格 -> list[StyledGrid]（每张表一个 StyledGrid）。

    流程（模型无关，可用 fake backend 测试）：
        1) pypdfium2 逐页渲染为 PNG bytes。
        2) backend.recognize(png, page_no) -> OcrPageResult。
        3) 每个 OcrTable -> 一个 StyledGrid：
               sheet            = 'page{N}_table{M}'（N 页号、M 该页表序，均 1-based）。
               source_doc_id    = 文件名；source_file_hash = 内容 hash。
               cells            = 'R{行}C{列}' -> StyledCell；value 为空白归一化文本，
                                  resolved_rgb=None，confidence=OCR 置信度。
               n_rows / n_cols  = OcrTable 维度。

    低置信分流（user story 10）：
        - confidence < min_confidence 的格**仍入网格**（不丢数据），但：
            · grid 追加一条 warning（标记存在低置信格）。
            · 若传了 queue（ReviewQueue），按 reason='low_confidence_ocr'、priority=30、
              payload 含 locator / text / confidence 把该格入队待 SME 复核。

    不可读 / 零页 PDF -> 返回 []（不抛异常，且不调用 backend）。
    """
    path = Path(path)

    # 先用 pypdfium2 优雅打开并逐页渲染：不可读 / 空文件 / 零页一律在调用 backend
    # 之前短路为 []（先分诊、后识别）。
    images = _render_pages_png(path)
    if not images:
        return []

    source_doc_id = path.name
    source_file_hash = _file_hash(path)

    grids: list[StyledGrid] = []
    for page_no, png in enumerate(images, start=1):
        page_result = backend.recognize(png, page_no)
        for table_seq, table in enumerate(page_result.tables, start=1):
            sheet = f"page{page_no}_table{table_seq}"
            grids.append(
                _build_grid(
                    table,
                    sheet,
                    source_doc_id,
                    source_file_hash,
                    min_confidence,
                    queue,
                )
            )
    return grids


class PaddleOcrVlBackend:
    """PaddleOCR-VL 真实后端（三期扫描线，目标环境 Ubuntu + NVIDIA GPU）。

    实现 OcrBackend 协议。重依赖 paddleocr / paddlepaddle-gpu 仅在 Ubuntu+GPU 可用，
    因此：
        - 模块顶层**不 import** paddle / paddleocr。
        - 构造器接受可选模型配置，但在首次 recognize 前不加载任何模型（延迟初始化）；
          惰性 import paddleocr 也只发生在方法体内，避免在非目标平台 import 即失败。
        - 真实集成测试打 pytest `gpu` marker，只在 Ubuntu GPU 环境跑。

    依据的 API 版本与契约（PaddleOCR 3.x / PaddleX 表格识别管线）：
        本后端用 ``paddleocr.PPStructureV3`` 文档解析管线（PaddleOCR 3.x 引入，
        是 PP-StructureV2 的后继，PaddleOCR-VL 走的同一管线）。``pipeline.predict(img)``
        返回逐元素结果，其中表格段为 ``table_res_list``（见官方
        docs/version3.x table recognition 结构），每张表含：
            - ``pred_html``：表结构的 HTML 串（``<tr>/<td>`` 编码行列与 colspan）。
            - ``table_ocr_pred``：表内文本的 OCR 结果，含
              ``rec_texts``（逐文本框文本）与 ``rec_scores``（逐文本框识别置信度）。
        说明：PaddleOCR 的表格识别**只给到文本框（cell/检测框）粒度的置信度**
        （``rec_scores``），不提供更细的字符级置信度。因此本后端把每个文本框的
        ``rec_score`` 作为对应单元格的置信度落到 ``OcrCell.confidence``——这是该引擎
        能提供的最细粒度，符合上层「逐格置信度分流」的契约。``structure_score``
        （整表结构置信度）不混入单元格置信度。

    构造参数：
        model_config: 可选 dict，透传给 PPStructureV3 的构造关键字参数（如关闭
                      公式识别、指定 GPU device、选择表格结构模型等），缺省用管线默认。
    """

    def __init__(
        self, model_config: dict[str, object] | None = None, **kwargs: object
    ) -> None:
        # 延迟初始化：仅保存配置，不在此 import / 加载模型（避免非 GPU 平台构造即失败）。
        self._model_config: dict[str, object] = dict(model_config or {})
        self._model_config.update(kwargs)
        # paddleocr 的 PPStructureV3 无类型信息（GPU 专用、惰性 import），故为 Any。
        self._pipeline: Any = None  # 首次 recognize 时惰性构建。

    def _ensure_pipeline(self) -> Any:
        """首次使用时惰性 import paddleocr 并构建 PPStructureV3 管线（延迟初始化）。"""
        if self._pipeline is None:
            from paddleocr import PPStructureV3  # 惰性 import：仅 GPU 环境可用。

            self._pipeline = PPStructureV3(**self._model_config)
        return self._pipeline

    def recognize(self, image_bytes: bytes, page_no: int) -> OcrPageResult:
        """识别单页 PNG bytes -> OcrPageResult（逐格带置信度）。

        把 PNG bytes 解码为图像数组后喂给 PPStructureV3.predict，再把每张表的
        ``pred_html`` 解析为行列结构，并把 ``table_ocr_pred.rec_texts/rec_scores``
        按出现顺序对齐到非空单元格，逐格落 ``OcrCell.confidence``（文本框粒度，见
        类 docstring 说明）。无法解析的页返回空表 + 一条 warning，不抛异常。
        """
        import io

        import numpy as np
        from PIL import Image

        pipeline = self._ensure_pipeline()

        # PNG bytes -> RGB ndarray（PPStructureV3.predict 接受 numpy 图像）。
        image = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
        outputs = pipeline.predict(input=image)

        tables: list[OcrTable] = []
        warnings: list[str] = []
        for res in outputs:
            # PaddleOCR 3.x 结果对象支持 .json（dict）/ 也可直接索引；统一取 dict。
            data = res.json if hasattr(res, "json") else res
            if isinstance(data, dict) and "res" in data:
                data = data["res"]
            for table_res in data.get("table_res_list", []) or []:
                table = self._table_res_to_ocr_table(table_res)
                if table is not None:
                    tables.append(table)
        if not tables:
            warnings.append(f"page{page_no}: PPStructureV3 未检出任何表格")
        return OcrPageResult(page_no=page_no, tables=tables, warnings=warnings)

    @staticmethod
    def _table_res_to_ocr_table(table_res: dict[str, Any]) -> OcrTable | None:
        """把单张表的 PaddleOCR 结果（pred_html + table_ocr_pred）映射为 OcrTable。

        - 用 pred_html 解析出行列网格（每个 <td> 一格，跳过纯结构空格）。
        - 用 table_ocr_pred 的 rec_texts/rec_scores 提供每个文本的识别置信度，按
          文本内容对齐到对应 <td>（同文本多次出现时按出现顺序消费）。
        """
        pred_html = table_res.get("pred_html") or ""
        ocr_pred = table_res.get("table_ocr_pred") or {}
        rec_texts = list(ocr_pred.get("rec_texts") or [])
        rec_scores = list(ocr_pred.get("rec_scores") or [])

        # text -> 该文本各次出现的置信度队列（按出现顺序消费，支持重复文本）。
        score_by_text: dict[str, list[float]] = {}
        for text, score in zip(rec_texts, rec_scores, strict=False):
            score_by_text.setdefault(_normalize_text(text), []).append(float(score))
        default_score = (
            sum(rec_scores) / len(rec_scores) if rec_scores else 1.0
        )

        rows = _parse_html_table_rows(pred_html)
        if not rows:
            return None

        cells: list[OcrCell] = []
        n_cols = 0
        for r, row_texts in enumerate(rows, start=1):
            n_cols = max(n_cols, len(row_texts))
            for c, text in enumerate(row_texts, start=1):
                value = _normalize_text(text)
                if not value:
                    continue
                queue = score_by_text.get(value)
                confidence = queue.pop(0) if queue else default_score
                cells.append(
                    OcrCell(row=r, col=c, text=value, confidence=confidence)
                )
        return OcrTable(n_rows=len(rows), n_cols=n_cols, cells=cells)


def _parse_html_table_rows(pred_html: str) -> list[list[str]]:
    """把 PaddleOCR 的 pred_html 表结构解析为二维文本网格（每个 <td> 一格）。

    用标准库 html.parser（无第三方依赖），逐 <tr> 收集 <td> 文本；colspan 用空串
    占位以保持列对齐。仅取 <table> 内的单元格文本，结构标签本身丢弃。
    """
    from html.parser import HTMLParser

    class _TableParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.rows: list[list[str]] = []
            self._row: list[str] | None = None
            self._in_cell = False
            self._buf: list[str] = []
            self._pending_colspan = 1

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "tr":
                self._row = []
            elif tag == "td" and self._row is not None:
                self._in_cell = True
                self._buf = []
                self._pending_colspan = 1
                for key, val in attrs:
                    if key == "colspan" and val and val.isdigit():
                        self._pending_colspan = max(1, int(val))

        def handle_endtag(self, tag: str) -> None:
            if tag == "td" and self._in_cell and self._row is not None:
                self._row.append("".join(self._buf).strip())
                # colspan>1：补占位空格保持列对齐。
                for _ in range(self._pending_colspan - 1):
                    self._row.append("")
                self._in_cell = False
            elif tag == "tr" and self._row is not None:
                self.rows.append(self._row)
                self._row = None

        def handle_data(self, data: str) -> None:
            if self._in_cell:
                self._buf.append(data)

    parser = _TableParser()
    parser.feed(pred_html)
    return parser.rows
