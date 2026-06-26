"""家族 .docx 抽取器（W3b）—— docspine 封装契约（纯 Rust DOCX 解析，默认实现）。

职责：把 Word 文档的【表格】解析为带单元格级定位锚点的 StyledGrid，使 citation 能精确
回指到「表 + 格」；与 pdf_spine / pptx_styled 抽取器对齐同一 StyledGrid 中间表示，故可被
结构化入库通路（ingestion.py 的 _EXTRACTOR_BY_SUFFIX）与 registry（mime->Extractor 缝）复用。
正文段落归叙事通路（narrative_extract.extract_docx_narrative），不在本模块范围。

定位与命名约定（与 pdf_spine / pptx_styled 对齐）：
    - 每张【顶层】表格 -> 一个 StyledGrid，sheet 命名 'table{M}'（M=文档内表序，1-based；
      docx 无页/幻灯片概念，故用纯表序，对应 pdf 的 'page{N}_table{M}' / pptx 的
      'slide{N}_table{M}'）。
    - cell_ref 用 'R{行}C{列}'（1-based；列取真实网格列，按 gridSpan 推进列游标）。
    - value = 空白归一化后的单元格文本（字符串，不做类型推断）。
    - resolved_rgb = docspine 解析的单元格底纹色 cell['fill']（<w:shd w:fill> -> 大写
      'RRGGBB'，'auto'/无填充 -> None），经 _normalize_fill 归一后填入（W3d）。颜色由此
      流进既有 color-semantics（cluster_colors / detect_legend / apply_mapping，SME-gated）
      通路 —— 不扩 IR，仅填既有 resolved_rgb 字段（与 xlsx/pptx 同口径）。
    - source_doc_id / source_file_hash 血缘照常写入（与 fact_metric 对齐）。

合并跨度（W3b「尽量保留」，复用既有 IR 字段、不扩 IR）：
    - 水平 gridSpan>1 或纵向 vMerge -> is_merged_origin=True、merge_span=(行跨, 列跨)。
    - docspine 语义：cell['grid_span'] 为水平列跨；cell['v_merge'] ∈ {'none','restart',
      'continue'}，'restart' 为承载内容的锚点、'continue'（merged=True）为被吞的纵向续格
      （空文本，稀疏网格不收录）。纵向跨度 = 1 + 其下连续 'continue' 续格数。

嵌套表（W3d「有原则地表示」，cell['blocks'] 里的 kind=='table' 块）：
    - 每张嵌套表作为【独立 StyledGrid】递归产出，sheet 命名链式体现父子
      （{父sheet}.cell{r}_{c}.nested{k}，k 为该格内表序 1-based），父网格留 breadcrumb
      告警指向子网格 —— locator 链可追溯，绝不静默丢；嵌套富结构不污染父格 value。
    - extract_grids 返回扁平列表：父表在前、其嵌套子表（含深层递归）按阅读序紧随其后。

依赖约定（重要）：docspine 是可选 extra（[doc]）依赖，本模块顶部【不得】直接 import
docspine；在函数体内惰性 import，保证仅装基础依赖时本契约仍可被 import（核心离线可跑）。
"""

import hashlib
from pathlib import Path
from typing import Any

from ragspine.extraction.ir import StyledCell, StyledGrid

# 抽取器版本标识（写入每条 fact 的 extractor_version，便于回归门禁按解析器区分血缘）。
EXTRACTOR_VERSION = "docspine@1"


def _normalize_text(text: object) -> str:
    """空白归一化：首尾 strip + 内部连续空白折叠为单空格。"""
    return " ".join(str(text).split())


def _normalize_fill(fill: object) -> str | None:
    """把 docspine 的 cell['fill'] 归一成 IR 契约的 resolved_rgb（W3d）。

    docspine 已把 <w:shd w:fill> 解析为大写 'RRGGBB' 串、并把 'auto' 归一为 None；
    本函数再防御性地把 None/空串/'auto'/'none'（大小写无关）统一为 None，其余大写返回，
    与 StyledCell.resolved_rgb（'RRGGBB' 大写、无填充为 None）契约对齐。
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
                source_file_hash: str) -> list[StyledGrid]:
    """把一张 docspine 表 dict 转成 StyledGrid 列表：[本表, *递归嵌套子表]。

    用列游标按 gridSpan 推进得到真实网格列；vMerge restart/continue 计算纵向跨度；
    单元格底纹色 cell['fill'] 喂 resolved_rgb（W3d）；单元格内嵌套表（cell['blocks'] 里的
    kind=='table' 块）作为独立 StyledGrid 递归产出，sheet 命名链式体现父子
    （{父sheet}.cell{r}_{c}.nested{k}），父网格留 breadcrumb 告警，绝不静默丢（W3d）。
    本表恒在列表首位，嵌套子表按阅读序（行优先、cell 内多表按序）紧随其后。
    """
    grid = StyledGrid(
        sheet=sheet,
        source_doc_id=source_doc_id,
        source_file_hash=source_file_hash,
        n_rows=int(table["row_count"]),
        n_cols=int(table["col_count"]),
    )
    # 先排布全表算出合并真实跨度，再产出 StyledCell（纵向跨度需扫到下方续格才确定）。
    placements: list[dict[str, Any]] = []
    origin_at: dict[int, dict[str, Any]] = {}  # grid_col -> 当前活跃的纵向合并锚点
    for r, row in enumerate(table["rows"], start=1):
        col = 1
        for cell in row["cells"]:
            colspan = max(1, int(cell["grid_span"]))
            vmerge = cell["v_merge"]
            if vmerge == "continue":
                # 纵向续格：把上方锚点的行跨 +1，自身不入网格（空文本、被吞）。
                origin = origin_at.get(col)
                if origin is not None:
                    origin["rowspan"] += 1
                col += colspan
                continue
            placement: dict[str, Any] = {
                "r": r,
                "c": col,
                "colspan": colspan,
                "rowspan": 1,
                "text": _normalize_text(cell["text"]),
                "fill": cell.get("fill"),
                "nested_tables": [b for b in cell["blocks"] if b["kind"] == "table"],
            }
            placements.append(placement)
            if vmerge == "restart":
                origin_at[col] = placement
            else:
                origin_at.pop(col, None)
            col += colspan

    nested_grids: list[StyledGrid] = []
    for p in placements:
        ref = f"R{p['r']}C{p['c']}"
        text: str = p["text"]
        if text:
            rowspan = int(p["rowspan"])
            colspan = int(p["colspan"])
            merged = colspan > 1 or rowspan > 1
            grid.cells[ref] = StyledCell(
                value=text,
                cell_ref=ref,
                resolved_rgb=_normalize_fill(p["fill"]),
                is_merged_origin=merged,
                merge_span=(rowspan, colspan) if merged else None,
            )
        # 嵌套表：作为独立 StyledGrid 递归产出（即便父格空文本也不丢），父留 breadcrumb 告警。
        for k, nested_block in enumerate(p["nested_tables"], start=1):
            child_sheet = f"{sheet}.cell{p['r']}_{p['c']}.nested{k}"
            grid.add_warning(
                f"{sheet}!{ref} 含嵌套表，已作为独立 StyledGrid 产出：{child_sheet}"
            )
            nested_grids.extend(
                _build_grid(nested_block, child_sheet, source_doc_id, source_file_hash)
            )
    return [grid, *nested_grids]


def extract_grids(path: str | Path) -> list[StyledGrid]:
    """抽取一个 .docx 的全部表格 -> list[StyledGrid]（顶层表 + 递归嵌套子表，扁平化）。

    顶层表 sheet='table{M}'，其嵌套子表 sheet='table{M}.cell{r}_{c}.nested{k}'（父在前、
    子按阅读序紧随，W3d）。每个 StyledGrid：cell_ref='R{行}C{列}'、value 为归一化文本、
    resolved_rgb 取 docspine 解析的单元格底纹色（无填充为 None，W3d）；血缘 source_doc_id /
    source_file_hash 照常。无表格 -> []。
    """
    path = Path(path)

    # docspine 是可选 extra（[doc]）依赖，惰性 import（核心不装 [doc] 也可 import 本契约）。
    import docspine

    source_doc_id = path.name
    source_file_hash = _file_hash(path)

    doc = docspine.open(str(path))
    grids: list[StyledGrid] = []
    for table_seq, table in enumerate(doc.tables(), start=1):
        sheet = f"table{table_seq}"
        grids.extend(_build_grid(table, sheet, source_doc_id, source_file_hash))
    return grids


class DocspineGridExtractor:
    """家族 .docx 抽取器（实现 registry 的 Extractor 协议：extract(path)->list[StyledGrid]）。

    docspine 仍惰性 import（在模块级 extract_grids 函数体内），未装 [doc] 时构造本类不报错，
    仅在真正抽取时才需要 docspine。version='docspine@1' 写入新入库事实的 extractor_version，
    按解析器在血缘上可溯源区分（与 pdf_spine@1 / pptx_styled@1 同范式）。
    """

    version = EXTRACTOR_VERSION

    def extract(self, path: str | Path) -> list[StyledGrid]:
        # 委托模块级 extract_grids（test 对该模块属性打补丁时仍生效——同一命名空间）。
        return extract_grids(path)
