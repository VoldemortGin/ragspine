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
    - resolved_rgb 一律 None —— docx 填充色语义归 W3d（颜色语义是 Excel/PPT 范畴，与
      pdf_spine 同口径）。
    - source_doc_id / source_file_hash 血缘照常写入（与 fact_metric 对齐）。

合并跨度（W3b「尽量保留」，复用既有 IR 字段、不扩 IR=不踩 W3d）：
    - 水平 gridSpan>1 或纵向 vMerge -> is_merged_origin=True、merge_span=(行跨, 列跨)。
    - docspine 语义：cell['grid_span'] 为水平列跨；cell['v_merge'] ∈ {'none','restart',
      'continue'}，'restart' 为承载内容的锚点、'continue'（merged=True）为被吞的纵向续格
      （空文本，稀疏网格不收录）。纵向跨度 = 1 + 其下连续 'continue' 续格数。
    - 嵌套表（cell 内还有表）：本期按单元格文本抽取，嵌套富结构留 grid 告警待 W3d，
      绝不静默丢弃。

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


def _file_hash(path: Path) -> str:
    """源文件内容 sha256 十六进制串（版本血缘标识）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_grid(table: dict[str, Any], sheet: str, source_doc_id: str,
                source_file_hash: str) -> StyledGrid:
    """把一张 docspine 顶层表 dict 转成 StyledGrid（稀疏：只存非空文本格）。

    用列游标按 gridSpan 推进得到真实网格列；vMerge restart/continue 计算纵向跨度。
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
                "nested": any(b["kind"] == "table" for b in cell["blocks"]),
            }
            placements.append(placement)
            if vmerge == "restart":
                origin_at[col] = placement
            else:
                origin_at.pop(col, None)
            col += colspan

    for p in placements:
        text: str = p["text"]
        if not text:
            continue
        ref = f"R{p['r']}C{p['c']}"
        rowspan = int(p["rowspan"])
        colspan = int(p["colspan"])
        merged = colspan > 1 or rowspan > 1
        grid.cells[ref] = StyledCell(
            value=text,
            cell_ref=ref,
            resolved_rgb=None,
            is_merged_origin=merged,
            merge_span=(rowspan, colspan) if merged else None,
        )
        if p["nested"]:
            grid.add_warning(
                f"{sheet}!{ref} 含嵌套表，本期(W3b)按单元格文本抽取，嵌套富结构待 W3d"
            )
    return grid


def extract_grids(path: str | Path) -> list[StyledGrid]:
    """抽取一个 .docx 的全部【顶层】表格 -> list[StyledGrid]（每张表一个）。

    每个 StyledGrid：sheet='table{M}'、cell_ref='R{行}C{列}'、value 为归一化文本、
    resolved_rgb 恒 None；血缘 source_doc_id / source_file_hash 照常。无表格 -> []。
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
        grids.append(_build_grid(table, sheet, source_doc_id, source_file_hash))
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
