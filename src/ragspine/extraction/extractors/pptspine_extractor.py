"""家族 .pptx 抽取器（W3c，可选/opt-in）—— pptspine 封装契约（纯 Rust PowerPoint 解析）。

职责：把 PowerPoint 的【表格】解析为带单元格级定位锚点的 StyledGrid，使 citation 能精确
回指到「页 + 表 + 格」；与 pdf_spine / docspine / pptx_styled 抽取器对齐同一 StyledGrid
中间表示，故可被结构化入库通路（ingestion.py 的 pptx_extractor 注入缝）与 registry
（mime->Extractor 备选选择项）复用。

为什么 additive、不替换默认（重要）：现有 .pptx 默认路径是 python-pptx 的
`pptx_styled_extractor`，它额外抽取 color/chart/styled-run/note —— 功能不弱。pptspine 0.1.0
提供更富的表合并（gridSpan/rowSpan/hMerge/vMerge）+ autoshape + 内嵌图 OCR，但**未必覆盖**
python-pptx 的 color/chart/note。故本抽取器是 **opt-in 的更富表合并备选**，默认仍 python-pptx
（保 color/chart/note 不丢）：经 registry 选择项 'pptx+pptspine'，或结构化派发注入
`pptx_extractor=PptspineGridExtractor()` 启用。

定位与命名约定（与 pptx_styled 对齐，保证两条 pptx 路 IR 同形、下游零分支）：
    - 每张原生表格 -> 一个 StyledGrid，sheet 命名 'slide{N}_table{M}'（N=幻灯片号 1-based，
      M=该页内表序 1-based；与 python-pptx 的 pptx_styled 完全一致）。结构化入库的
      source_locator 因此为 'sheet=slide{N}_table{M}!R{r}C{c}'，承载页+表+格 provenance。
    - cell_ref 用 'R{行}C{列}'（1-based）。
    - value = 空白归一化后的单元格文本（字符串，不做类型推断）。
    - resolved_rgb = pptspine 解析的单元格底色 cell['fill']（a:tcPr 的 solidFill/srgbClr ->
      大写 'RRGGBB'；未解析的 theme/scheme 色 -> None），经 _normalize_fill 归一后填入（W3d）。
      颜色由此流进既有 color-semantics（SME-gated）通路 —— 不扩 IR，仅填既有 resolved_rgb
      字段（与 xlsx/docspine 同口径；theme/scheme 色仍由 python-pptx 默认路径解析，故 pptspine
      仍是 opt-in）。
    - source_doc_id / source_file_hash 血缘照常写入（与 fact_metric 对齐）。

合并跨度（W3c「尽量保留」，复用既有 IR 字段、不扩 IR=不踩 W3d）：
    - pptspine 的表模型把整个网格逐格铺平（OOXML 全 R×C 网格）：合并锚点格带已解析的
      col_span/row_span（gridSpan/rowSpan），被吞的延续格（hMerge/vMerge）带 merged=True
      且空文本。故按 (行下标, 列下标) 直接定位真实网格列，无需列游标（与 docspine 不同）。
    - 锚点 col_span>1 或 row_span>1 -> is_merged_origin=True、merge_span=(行跨, 列跨)。
    - 延续格（merged=True）空文本、被吞，稀疏网格不收录。

已知限制（pptspine 0.1.0，诚实记录——亦是它留作 opt-in、默认仍 python-pptx 的原因之一）：
    - 每页只稳定返回【首张】表格：pptspine 0.1.0 的 graphicFrame 解析在遇到表格内层
      结束标签时即收束当页形状遍历，故同页第二张表及其后续形状会被丢。本抽取器逐页
      逐表的 sheet 命名（slide{N}_table{M}）与 table_no 自增逻辑仍正确（与 pptx_styled
      对齐），只是 pptspine 0.1.0 不会喂超过一张表/页。python-pptx 的默认路径不受此限。
    - color/chart/note：pptspine 0.1.0 不解析 theme/scheme 色、图表、演讲者备注；这些仍由
      默认 python-pptx 路径（pptx_styled_extractor）承担。故 pptspine 是「更富表合并」的
      opt-in 备选，绝不替换默认（铁律：不丢 color/chart/note）。

依赖约定（重要）：pptspine 是可选 extra（[ppt]）依赖，本模块顶部【不得】直接 import
pptspine；在函数体内惰性 import，保证仅装基础依赖时本契约仍可被 import（核心离线可跑）。
"""

import hashlib
from pathlib import Path
from typing import Any

from ragspine.extraction.ir import StyledCell, StyledGrid

# 抽取器版本标识（写入每条 fact 的 extractor_version，便于回归门禁按解析器区分血缘）。
EXTRACTOR_VERSION = "pptspine@1"


def _normalize_text(text: object) -> str:
    """空白归一化：首尾 strip + 内部连续空白折叠为单空格。"""
    return " ".join(str(text).split())


def _normalize_fill(fill: object) -> str | None:
    """把 pptspine 的 cell['fill'] 归一成 IR 契约的 resolved_rgb（W3d）。

    pptspine 把 a:tcPr 里的 solidFill/srgbClr 解析为大写 'RRGGBB' 串、未解析的填充
    （theme/scheme 色等）为 None；本函数再防御性地把 None/空串/'auto'/'none'（大小写
    无关）统一为 None，其余大写返回，与 StyledCell.resolved_rgb 契约对齐。
    """
    if not fill:
        return None
    text = str(fill).strip()
    if not text or text.lower() in ("auto", "none"):
        return None
    return text.upper()


def _file_hash(path: Path) -> str:
    """源文件内容 sha256 十六进制串（版本血缘标识）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_grid(table: dict[str, Any], sheet: str, source_doc_id: str,
                source_file_hash: str) -> StyledGrid:
    """把一个 pptspine table 形状 dict 转成 StyledGrid（稀疏：只存非空文本格）。

    table['rows'] 为逐行 list；每行 row['cells'] 为逐格 list（含被吞的延续格）。
    每格 dict：text / col_span / row_span / merged。pptspine 已把网格铺平，故
    (行下标 r, 列下标 c) 即真实网格坐标（cell_ref='R{r}C{c}'，1-based）。
    """
    rows: list[dict[str, Any]] = table["rows"]
    n_rows = len(rows)
    n_cols = max((len(row["cells"]) for row in rows), default=0)
    grid = StyledGrid(
        sheet=sheet,
        source_doc_id=source_doc_id,
        source_file_hash=source_file_hash,
        n_rows=n_rows,
        n_cols=n_cols,
    )
    for r, row in enumerate(rows, start=1):
        for c, cell in enumerate(row["cells"], start=1):
            # 被吞的合并延续格（hMerge/vMerge）：空文本、被吞，稀疏网格不收录。
            if cell["merged"]:
                continue
            value = _normalize_text(cell["text"])
            if not value:
                continue
            colspan = max(1, int(cell["col_span"]))
            rowspan = max(1, int(cell["row_span"]))
            merged = colspan > 1 or rowspan > 1
            ref = f"R{r}C{c}"
            grid.cells[ref] = StyledCell(
                value=value,
                cell_ref=ref,
                resolved_rgb=_normalize_fill(cell.get("fill")),
                is_merged_origin=merged,
                merge_span=(rowspan, colspan) if merged else None,
            )
    return grid


def extract_grids(path: str | Path) -> list[StyledGrid]:
    """抽取一个 .pptx 的全部原生表格 -> list[StyledGrid]（每张表一个）。

    每个 StyledGrid：sheet='slide{N}_table{M}'、cell_ref='R{行}C{列}'、value 为归一化
    文本、resolved_rgb 取 pptspine 解析的单元格底色（未解析/无填充为 None，W3d）；血缘
    source_doc_id / source_file_hash 照常。无表格 -> []。

    只遍历每页【顶层】形状里的表格（与 python-pptx 的 pptx_styled 同口径）。
    """
    path = Path(path)

    # pptspine 是可选 extra（[ppt]）依赖，惰性 import（核心不装 [ppt] 也可 import 本契约）。
    import pptspine

    source_doc_id = path.name
    source_file_hash = _file_hash(path)

    pres = pptspine.open(str(path))
    grids: list[StyledGrid] = []
    for slide_no, slide in enumerate(pres.slides(), start=1):
        table_no = 0
        for shape in slide.shapes():
            if shape["kind"] != "table":
                continue
            table_no += 1
            sheet = f"slide{slide_no}_table{table_no}"
            grids.append(_build_grid(shape, sheet, source_doc_id, source_file_hash))
    return grids


class PptspineGridExtractor:
    """家族 .pptx 抽取器（实现 registry 的 Extractor 协议：extract(path)->list[StyledGrid]）。

    pptspine 仍惰性 import（在模块级 extract_grids 函数体内），未装 [ppt] 时构造本类不报错，
    仅在真正抽取时才需要 pptspine。version='pptspine@1' 写入新入库事实的 extractor_version，
    按解析器在血缘上可溯源区分（与 pdf_spine@1 / docspine@1 / pptx_styled@1 同范式）。

    它是 **opt-in 的更富表合并备选**：默认 .pptx 仍走 python-pptx（pptx_styled），保
    color/chart/note 不丢；显式注入本抽取器或经 registry 'pptx+pptspine' 选用方启用。
    """

    version = EXTRACTOR_VERSION

    def extract(self, path: str | Path) -> list[StyledGrid]:
        # 委托模块级 extract_grids（test 对该模块属性打补丁时仍生效——同一命名空间）。
        return extract_grids(path)
