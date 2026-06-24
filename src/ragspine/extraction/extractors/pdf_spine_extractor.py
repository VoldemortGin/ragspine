"""数字型 PDF 表格抽取器（二期 PDF 线）—— pdfspine 封装契约（默认实现）。

职责（PRD user story 9）：把数字型 PDF 的表格解析为带单元格级定位锚点的
StyledGrid，使 citation 能精确回指到「页 + 表 + 格」。与 pdf_digital_extractor
（Docling 封装）字节对齐同一中间表示，故可作为它的零成本替代：同 sheet 命名、
同 'R{行}C{列}' 坐标、同稀疏丢空格、同血缘字段。区别仅在底层解析引擎——
pdfspine 是纯 Rust、确定性、无 torch 重依赖，体积远小于 Docling。

定位与命名约定（与 pdf_digital_extractor 完全一致）：
    - cell_ref 用 'R{行}C{列}'（1-based，行列均从 1 起），与 Excel 'C4' 风格区分。
    - source_locator 语义 = 页号 + 表序，体现在 StyledGrid.sheet 命名 'page{N}_table{M}'
      （N=页号 1-based，M=该页内表序 1-based）。
    - source_doc_id / source_file_hash 血缘照常写入（与 fact_metric 对齐）。
    - resolved_rgb 一律为 None —— PDF 不做颜色语义（颜色编码是 Excel/PPT 的范畴）。
    - 表格单元格文本做空白归一化（首尾 strip + 内部连续空白折叠为单空格）。
    - 正文叙述文本块不在本模块范围（只产表格网格）。

行为约定：
    - 输入是扫描型 / 不可读 PDF 时返回 []，由调用方依赖 pdf_router 分诊把这类文件
      路由到扫描管线，本模块不抛异常、不做 OCR（与 pdf_digital_extractor 同款分诊）。
    - 数字型 PDF 无表格时返回 []。

依赖约定（重要）：
    pdfspine 是可选 extra（[pdf]）依赖，本模块顶部**不得直接 import pdfspine**；在函数体内
    做惰性 import，保证仅安装基础依赖时本契约仍可被 import。
"""

from pathlib import Path
from typing import Any

from ragspine.extraction.extractors.pdf_digital_extractor import (
    _file_hash,
    _has_digital_page,
    _normalize_text,
)
from ragspine.extraction.ir import StyledCell, StyledGrid

# 抽取器版本标识（写入血缘，便于回归门禁区分解析器版本）。
EXTRACTOR_VERSION = "pdf_spine_v0"

# 表格检测策略：'lines' 用框线网格切分（数字型规整表格的稳定路径，确定性）。
# 不用 'text'：它按文本几何聚类，对带框表会过度切分并把叙述段误判成表格。
TABLE_STRATEGY = "lines"


def _build_grid(table: Any, sheet: str, source_doc_id: str,
                source_file_hash: str) -> StyledGrid:
    """把一个 pdfspine Table 转成 StyledGrid（稀疏：只存非空文本格）。

    Table.extract() 返回 row-major 的 list[list]，空格 / 合并续格为 None。
    cell_ref = 'R{行}C{列}'（1-based）= 行/列下标 + 1。
    value = 空白归一化后的单元格文本（字符串，不做类型推断）。
    resolved_rgb 恒为 None。空白格（如表头左上角）不入稀疏映射。
    n_rows / n_cols 取表格逻辑维度（与 Docling 实现口径一致）。
    """
    grid = StyledGrid(
        sheet=sheet,
        source_doc_id=source_doc_id,
        source_file_hash=source_file_hash,
        n_rows=table.row_count,
        n_cols=table.col_count,
    )
    for r, row in enumerate(table.extract(), start=1):
        for c, raw in enumerate(row, start=1):
            if raw is None:
                continue
            value = _normalize_text(raw)
            if not value:
                continue
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
    # 不在位图上浪费表格检测，也避免 OCR 隐形文本被误抽（复用 Docling 实现的同款分诊）。
    if not _has_digital_page(path):
        return []

    # pdfspine 是可选 extra（[pdf]）依赖，惰性 import（仅在确认是数字型后才加载）。
    import pdfspine

    source_doc_id = path.name
    source_file_hash = _file_hash(path)

    grids: list[StyledGrid] = []
    doc = pdfspine.open(str(path))
    try:
        for page_idx in range(len(doc)):
            page = doc.load_page(page_idx)
            finder = page.find_tables(strategy=TABLE_STRATEGY)
            for table_seq, table in enumerate(finder, start=1):
                sheet = f"page{page_idx + 1}_table{table_seq}"
                grids.append(
                    _build_grid(table, sheet, source_doc_id, source_file_hash)
                )
    finally:
        doc.close()
    return grids


class PdfSpineGridExtractor:
    """默认 GridExtractor：薄封装本模块的 pdfspine 数字型抽取（与 Docling 实现 IR 字节对齐）。

    pdfspine 仍惰性 import（在模块级 extract_grids 函数体内），未装 [pdf] 时构造本类
    不报错，仅在真正抽取时才需要 pdfspine。version = 'pdf_spine@1'：默认路径切到 pdfspine
    后，新入库事实的 extractor_version 即为此值，与历史 Docling 路径（'pdf_digital@1'）
    在血缘上可溯源区分。

    实现 pdf_digital_extractor.GridExtractor 协议（结构性满足：version + extract_grids），
    与 DoclingGridExtractor 可互换注入 ingest_file。
    """

    version = "pdf_spine@1"

    def extract_grids(self, path: str | Path) -> list[StyledGrid]:
        # 委托模块级 extract_grids（test 对该模块属性打补丁时仍生效——同一命名空间）。
        return extract_grids(path)
