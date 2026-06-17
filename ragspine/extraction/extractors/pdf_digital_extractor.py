"""数字型 PDF 表格抽取器（二期 PDF 线）—— Docling 封装契约。

职责（PRD user story 9）：把数字型 PDF 的表格解析为带单元格级定位锚点的
StyledGrid，使 citation 能精确回指到「页 + 表 + 格」。与 xlsx_styled_extractor
对齐同一中间表示，下游（入库 / 复核 / 评测）零改动即可消费 PDF 来源的事实。

定位与命名约定：
    - cell_ref 用 'R{行}C{列}'（1-based，行列均从 1 起），与 Excel 'C4' 风格区分，
      表明来源是 PDF 表格而非工作表坐标。
    - source_locator 语义 = 页号 + 表序，体现在 StyledGrid.sheet 命名 'page{N}_table{M}'
      （N=页号 1-based，M=该页内表序 1-based）。
    - source_doc_id / source_file_hash 血缘照常写入（与 fact_metric 对齐）。
    - resolved_rgb 一律为 None —— PDF 不做颜色语义（颜色编码是 Excel/PPT 的范畴）。
    - 表格单元格文本做空白归一化（首尾 strip + 内部连续空白折叠为单空格）。
    - 正文叙述文本块不在本模块范围（只产表格网格）。

行为约定：
    - 输入是扫描型 / 不可读 PDF 时返回 []，由调用方依赖 pdf_router 分诊把这类文件
      路由到扫描管线，本模块不抛异常、不做 OCR。
    - 数字型 PDF 无表格时返回 []。

依赖约定（重要）：
    Docling 是重依赖，本模块顶部**不得直接 import docling**；在函数体内做惰性
    import，保证仅安装基础依赖时本契约仍可被 import。
"""

import hashlib
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from ragspine.extraction.ir import StyledCell, StyledGrid

# 抽取器版本标识（写入血缘，便于回归门禁区分解析器版本）。
EXTRACTOR_VERSION = "pdf_digital_docling_v0"

# 分诊阈值（沿用 pdf_router / scripts/classify_pdfs.py，库化前后规则一致）。
TEXT_MIN_CHARS = 50      # 每页判定「有实质文本」的最少字符数。
IMG_COVER_SCAN = 0.55    # 图片覆盖率超过此值视为「扫描底图」。


def _normalize_text(text: object) -> str:
    """空白归一化：首尾 strip + 内部连续空白折叠为单空格。"""
    return " ".join(str(text).split())


def _file_hash(path: Path) -> str:
    """源文件内容 sha256（十六进制），作为版本与审计血缘锚点。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _page_image_cover(page) -> float:
    """图片对页面的覆盖率 [0.0, 1.0]（多图叠放封顶为 1.0）。"""
    width, height = page.get_size()
    page_area = (width * height) or 1.0
    cover = 0.0
    for obj in page.get_objects(
        filter=(pdfium_raw.FPDF_PAGEOBJ_IMAGE,), max_depth=15
    ):
        # 裁剪到页面内
        left, bottom, right, top = obj.get_bounds()
        left, right = max(left, 0.0), min(right, width)
        bottom, top = max(bottom, 0.0), min(top, height)
        if right > left and top > bottom:
            cover += (right - left) * (top - bottom) / page_area
    return min(cover, 1.0)


def _page_chars(page) -> int:
    """可提取文本字符数（strip 后），与 pdf_router 度量口径一致。"""
    textpage = page.get_textpage()
    try:
        return len(textpage.get_text_range().strip())
    finally:
        textpage.close()


def _has_digital_page(path: Path) -> bool:
    """用 pypdfium2 快速判断 PDF 是否含数字型页（文本多 + 图片覆盖低）。

    与 pdf_router 同款逻辑：仅当存在「字符数 >= TEXT_MIN_CHARS 且
    图片覆盖 < IMG_COVER_SCAN」的页时才值得交给 Docling。扫描型（无文本层）、
    OCR 扫描型（隐形文本 + 高覆盖位图）都不满足，直接判为非数字型，避免把
    Docling 浪费在位图上、也避免 OCR 隐形文本被误抽。读取失败时按非数字型处理。
    """
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception:
        return False
    try:
        for idx in range(len(doc)):
            page = doc[idx]
            try:
                if (_page_chars(page) >= TEXT_MIN_CHARS
                        and _page_image_cover(page) < IMG_COVER_SCAN):
                    return True
            finally:
                page.close()
        return False
    finally:
        doc.close()


def _table_page_no(table) -> int:
    """从 Docling TableItem 的 provenance 取所在页号（1-based）。"""
    prov = getattr(table, "prov", None) or []
    if prov:
        page_no = getattr(prov[0], "page_no", None)
        if isinstance(page_no, int) and page_no >= 1:
            return page_no
    return 1


def _build_grid(table, sheet: str, source_doc_id: str,
                source_file_hash: str) -> StyledGrid:
    """把一个 Docling TableItem 转成 StyledGrid（稀疏：只存非空文本格）。

    cell_ref = 'R{行}C{列}'（1-based）= Docling 0-based offset + 1。
    value = 空白归一化后的单元格文本（字符串，不做类型推断）。
    resolved_rgb 恒为 None。空白格（如表头左上角）不入稀疏映射。
    """
    data = table.data
    grid = StyledGrid(
        sheet=sheet,
        source_doc_id=source_doc_id,
        source_file_hash=source_file_hash,
        n_rows=data.num_rows,
        n_cols=data.num_cols,
    )
    for cell in data.table_cells:
        value = _normalize_text(cell.text)
        if not value:
            continue
        r = cell.start_row_offset_idx + 1
        c = cell.start_col_offset_idx + 1
        ref = f"R{r}C{c}"
        grid.cells[ref] = StyledCell(value=value, cell_ref=ref, resolved_rgb=None)
    return grid


def extract_grids(path: str | Path) -> list[StyledGrid]:
    """抽取一个数字型 PDF 的全部表格 -> list[StyledGrid]（每张表一个 StyledGrid）。

    每个 StyledGrid：
        sheet            = 'page{N}_table{M}'（N 页号、M 该页表序，均 1-based）。
        source_doc_id    = 文件名；source_file_hash = 内容 hash。
        cells            = 'R{行}C{列}' -> StyledCell（resolved_rgb 恒为 None，
                           value 为空白归一化后的单元格文本）。
        n_rows / n_cols  = 表格逻辑行列数。

    扫描型 / 不可读 PDF -> 返回 []（依赖 pdf_router 分诊路由，不抛异常）。
    无表格 -> 返回 []。
    """
    path = Path(path)

    # 先用 pypdfium2 快速分诊：非数字型（扫描 / OCR 扫描 / 不可读）直接返回 []，
    # 不把 Docling 浪费在位图上，也避免 OCR 隐形文本被误抽。
    if not _has_digital_page(path):
        return []

    # Docling 是重依赖，惰性 import（仅在确认是数字型后才加载）。
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False  # 本模块只处理数字型，禁用 OCR。
    pipeline_options.do_table_structure = True
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )

    try:
        result = converter.convert(str(path))
    except Exception:
        return []

    document = result.document
    source_doc_id = path.name
    source_file_hash = _file_hash(path)

    grids: list[StyledGrid] = []
    per_page_table_seq: dict[int, int] = {}
    for table in document.tables:
        page_no = _table_page_no(table)
        per_page_table_seq[page_no] = per_page_table_seq.get(page_no, 0) + 1
        sheet = f"page{page_no}_table{per_page_table_seq[page_no]}"
        grids.append(
            _build_grid(table, sheet, source_doc_id, source_file_hash)
        )
    return grids
