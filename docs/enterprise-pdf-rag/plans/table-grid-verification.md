> 实施分支 `feat/table-grid-proof`（2026-09-20 合入）。本文件是**方案原文**，只在顶部加了这行注记；实现与方案的偏离、最终口径与遗留以 [ADR 0014](../adr/0014-ruled-table-grid-proof.md) 与 [交接文档](../CLAUDE_HANDOFF.md) 为准。

# 表格网格结构来源证明：让 `TableIR.verification` 有资格成为 `VERIFIED`

仓库 `/Users/linhan/startup/spine/ragspine`，基线 `main = c15525b`（`feat/page-metadata` 已 fast-forward 进 main，ADR 0013 / policy v4 在 main）。只读设计，不改仓库。
所有路径相对 `src/enterprise_pdf_rag/`，除非写明 `tests/`、`docs/`、`scripts/`；行号一律按 `git show c15525b:<path> | cat -n` 核对。
pdfspine 版本 `0.11.0`（`.venv/bin/python -c "import pdfspine; print(pdfspine.__version__)"`）。

> 一句话：ADR 0011 把 "VERIFIED `TableIR`" 列入 Rejected alternatives 的理由是 *"a new qualification with no source rule"*（`docs/enterprise-pdf-rag/adr/0011-...:147-149`）。本方案就是补这条 **source rule**：网格的每条行/列边界、每个 cell 的四边、每个合并格的"缺失内线"都必须在页面 `get_drawings()` 的实际 ruling 线段里找到逐段证据；找不到就保持 `PENDING`，不猜。无线表（空白分隔）、部分划线表、吸附出来的边界、双线边框全部 fail closed。

---

## 0. 目标与不变量

### 0.1 可验证目标

| # | 目标 | 验证方式 |
|---|---|---|
| G1 | 一张**完全划线**的原生表（含合并单元格、多级表头）经 `PdfspineTableAdapter().extract` 后 `TableIR.verification is VERIFIED`，每个 `TableCell.verification is VERIFIED`，且 IR 里带 `GridEvidence` / `CellBorderEvidence` | `tests/enterprise_pdf_rag/adapters/test_pdfspine_tables.py`（§6.3） |
| G2 | 无线表、外框有内线无、吸附边界（snap 3.0 合并 2pt 偏差）、双线边框、合并处有内线、边不连续 → 保持 `PENDING`，`diagnostics` 给出具体拒绝原因 | 同上 + `tests/.../processing/test_table_grid_proof.py`（§6.2） |
| G3 | 旧 `ir.json`（无 `grid_evidence` 字段，`data/ingestion/` 两个 v2 快照）仍能 `TypeAdapter(TableIR).validate_json` 并保持 `PENDING`；旧快照照常 `load` / `resolve` | `tests/.../processing/test_table_grid.py`（§6.2）+ 真实样本 smoke（§6.5） |
| G4 | 回答链：`cells.<id>` 引用在 VERIFIED 表上可附 `row/col/header`，`verify.py` 按 IR 逐项校验；PENDING 表上带 `row/col/header` 的 claim 被拒；`header` 只接受证据等级 `proved` 的表头文本、逐字相等 | `tests/.../answers/test_verify.py` + `test_answer_service.py` e2e（§6.4） |
| G5 | 资格判定（`eligibility`）与 policy 字符串不因本方案而拒收任何旧快照 | `tests/.../adapters/test_draft_publication.py` 现有用例全绿 + 真实样本 smoke |
| G6 | `processing/`、`answers/` 仍只 import stdlib(+pydantic)；pdfspine 只在 `adapters/` | `scripts/enterprise_pdf_rag/check_architecture.py` + `check_conformance.py` |

### 0.2 不变量逐条对照

| 不变量 | 本方案如何守住 |
|---|---|
| 逐字证据 | 网格证据是 **线段级** 的：每条边界/每条 cell 边都记 `SegmentRef(path_index, item_index, edge, p0, p1)` 指回 `get_drawings()` 的具体 path；`header` 引用必须逐字（`==`，不 casefold）等于表头 cell 文本。cell 文本仍由 ADR 0011 的 `check_table_transcription` 逐字证明，本方案不碰。 |
| fail closed | `prove_grid` 返回 `GridRejection(reason)` 的所有情形（§2.3）都保持 `PENDING`；`UNKNOWN` slot、虚线、超厚线、吸附边界、双线、部分线一律不证。 |
| 零人工 | 证据全部由 `ruling_segments(page)` + 纯几何规则产出；没有任何人工标注、没有夹具反向登记。 |
| LLM 只在构建阶段 | 网格证明不调用模型；回答链 `verify.py` 只做确定性比对。 |
| same-SVG two branches | 网格证明是 IR 分支的资格，不读 description；description 分支（逐字转写）不变；两者都绑定同一 `svg` 资产（`LiteralQualification.source_svg`）。 |
| 快照不可变 | 不改任何已落盘资产字节。新 `ir.json` 因新增字段而换 sha → 新 member id → 新 snapshot id；旧快照文件原样保留。 |
| 旧快照可挂载 | `TableIR.grid_evidence: GridEvidence | None = None`、`TableCell.border: CellBorderEvidence | None = None` 默认 None → 旧 JSON 解析为 PENDING；`ProcessingStore.load:115-131` 的四元组绑定不变；policy 只在读取侧分支（§3.4）。 |
| import 白名单 | 新增纯模块 `processing/table_grid_proof.py`、`processing/geometry.py` 扩展只用 stdlib；`ruling_segments` / `fill_rectangles` 放 `adapters/pdfspine_tables.py`（`check_architecture.py:8-16` 的 `PACKAGES` 白名单）。 |

---

## 1. 现状事实（函数/行号级）

### 1.1 事实表

| 路径:行号 | 签名 / 常量 | 要点 |
|---|---|---|
| `processing/table_models.py:23-34` | `class TableCell(cell_id,row,col,row_span,col_span,bbox,source_span_ids,text,content_state,verification=PENDING)` | 无任何边框 / ruling / header 字段 |
| `:36-38` | `TableCell.__post_init__` | **无条件** `verification is not PENDING → ValueError("Inferred table cells must remain pending")` |
| `:79-88` | `class TableIR(object_id,source,row_count,col_count,cells,slots,verification=PENDING,diagnostics=())` | `diagnostics` 是唯一自由文本槽位 |
| `:90-94` | `TableIR.__post_init__` | **无条件** IR 与每个 cell 都必须 PENDING → `"Inferred table IR must remain pending"` |
| `:108-115` | 同上 | cell ⊂ `source.bbox` 严格 `<=`，无容差（canonical vs canonical） |
| `processing/table_transcription.py:31-62` | `check_table_transcription(table, spans, *, anchor)` | 只验文字（span ⊂ anchor、span 中心 ⊂ cell、PRESENT 文本逐字），**不读任何线** |
| `processing/geometry.py:13` / `:16-24` | `COORDINATE_TOLERANCE = 1e-6`；`contains(outer, inner, *, tolerance)` | 只放宽 outer；文件只有这两个公开名字 |
| `adapters/pdfspine_tables.py:27-38` | `_contains(outer, inner, *, tolerance=0.5)` | 0.5pt 只用于"检测表 bbox ⊂ layout bbox" |
| `:41-47` | `_center_inside(inner, outer)` | span→cell 归属靠 bbox 中心，无容差 |
| `:53-88` | `PdfspineTableAdapter.extract(pdf, *, page, item)` | `:60-66` 拒绝旋转页 / page rect ≠ `(0,0,w,h)`；`:68` **唯一** `find_tables(strategy="lines", clip=item.bbox)`；`:69-71` 事后 `_contains` 过滤；`:79-85` `len(matches)!=1` → 无表 |
| `:110-205` | `_map_table(table, *, page, item)` | `:128-168` 装配 cell，`cell_id = content_id("table-cell-v1", (object_id,row,col,row_span,col_span,bbox,text,state,source_span_ids))`（**不含 verification / 证据** → 加证据不改 cell_id）；`:185-188` 两条 diagnostics（第二条自述 "remain pending"）；`:189-203` 构造 `TableIR(..., diagnostics=)` 不传 verification |
| `adapters/semantic_objects.py:225-304` | `SemanticObjectAdapter._table(page,item,writer,stages,crop)` | `:237-246` 存 `svg` + `table_detection`；`:247-261` 无表 → `ir/description/qualification` 三 diagnostic；`:262` **即使转写失败也保留 `ir`**；`:264-283` 转写失败分支；`:284-303` 成功 → `description` + `LiteralQualification(object_id, source, manifest, span_ids, _ref(ir), _ref(description), _ref(svg))` |
| `:54-72` / `:74-92` | `_Writer.save(stage, payload, media_type)` / `_Writer.diagnostic(stage, reason, *, failed=False)` | 新 stage 只需一个名字 |
| `processing/typed_ir.py:121-132` | `class LiteralQualification(object_id, source, source_manifest_id, source_span_ids, ir, description, source_svg, scope="literal-source-transcription-v1")` | 回执只绑三件资产，无"网格证明"槽 |
| `adapters/literal_qualification.py:28-33` | `validate_literal_member(sources, assets, scope, member) -> tuple[TextIR|ListIR|GroupIR|TableIR, ObjectDescription, LiteralQualification]` | `:44` `source = sources.load(scope.source_manifest_id)`；`:50-51` 读 text sidecar；`:109-123` TABLE 分支：object_id / anchor / `table_span_ids == receipt.source_span_ids` / `check_table_transcription` |
| `adapters/processing_retrieval.py:60` | `_POLICY = "source-transcription-and-scoped-chart-qualification-v4"` | `:66-72` `PROJECTED_CHART_POLICIES`，`:74` `CONTEXTUAL_POLICIES`，`:77-98` `member_text` 按 policy 分支 |
| `:101-130` | `eligibility(record) -> tuple[bool, str|None]` | `:107-113` 五类白名单含 TABLE；`:116-120` required 四个固定 stage 名（非 CHART：`ir/description/qualification/svg`）；`:124-128` TABLE 专用拒绝串 |
| `:144-256` | `ProcessingRetrieval.build(scope, records, contexts=None)` | `:173-203` 只有 CHART 填 `lineage_refs`；`:217` `_qualified` → `validate_literal_member`；`:240` `RetrievalPlan(scope, members, _POLICY, _INDEX)` |
| `adapters/processing_store.py:115-131` | `ProcessingStore.load` 成员绑定复核 | 只认 `(qualified_ir|ir, qualified_description|description, qualification, svg)` 四元组 + `lineage_refs ⊆ stages`；全文无 `policy` 字样 |
| `adapters/draft_publication.py:25-27` | `DraftQualification.qualification_policy: Literal["retrieval-eligibility-kind-and-stage-completeness-v2"]` | `:38-79` `qualify_draft` 只用 `eligibility()` 计数 |
| `processing/context_builder.py:46-56` | `class CellEvidence(cell_id,row,col,row_span,col_span,bbox,text,content_state,source_span_ids)` | 无 header / 网格字段 |
| `:70-85` | `class ContextBlock(... verification, description_text, spans, list_items, cells, row_count, col_count, grammar, chart_fields)` | |
| `:109-116` | `prompt_text()` TABLE 分支 | `:116` `f"cells.{cell.cell_id} ({cell.row},{cell.col}): {shown}"` —— 可引用路径唯一生成处 |
| `:182-210` | `build_context_block` TableIR 分支 | `:192` verification 取 **`context.description.verification`**（CHART 分支 `:219` 取 `ir.verification`） |
| `answers/verify.py:61-70` | `_PATH_PREFIX[CELL]="cells."`、`_BLOCK_KINDS[CELL]={TABLE}` | |
| `:81-82` | `_norm(text) = " ".join(text.split()).casefold()` | cell 文本比对口径（casefold，**不适合** header 逐字） |
| `:137-165` | `_verify_cell(claim, block)` | 只比 `_norm(claim.text) == _norm(cell.text)`；不读 row/col，不读 `block.verification`；`:155-163` 组 `ClaimCitation(member_id, kind, page_index, field_path, (cell_id,*span_ids), bbox, text)` |
| `:293-342` | `verify_claims(model, blocks, *, chart_evidence)` | `:320-326` kind/path 门；`:329-330` CELL 分发 |
| `answers/prompt.py:17-23` | `class ModelClaim(claim_id, member_id, kind, field_path, text)`，`extra="forbid"`、`strict=True` | 模型**无法**传 row/col/header |
| `:41-56` | `SYSTEM_RULES` | `:45-49` 规则 1 只讲 `cells.<cell_id>` + "exactly the cell content" |
| `answers/models.py:109-119` | `class ClaimCitation(member_id, kind, page_index, field_path, evidence_ids, bbox, quote, chart_citation=None, page_title=None)` | 无 row/col/header |
| `:122-129` | `class VerifiedClaim(claim_id, kind, text, value, unit, citations)` | |
| `documents/models.py:6` / `:14-21` | `Bounds = tuple[float,float,float,float]`；`TextSpan(span_id, text, bbox, origin, font, size, direction)` | `font` 可用于 `font_bold` 启发式 |
| `figures/models.py:73-83` | `SourceAnchor(source_revision, document_sha256, page_index, bbox, coordinate_frame="page-top-left", rotation=0, transform=identity)` | |
| `scripts/enterprise_pdf_rag/check_architecture.py:8-16` | `PACKAGES = (figures, documents, processing, answers)`；`EXTRA_ALLOWED = {answers: {pydantic}}` | `processing/` 纯 stdlib；`answers/` 可 pydantic |

### 1.2 ADR / 文档规则原文（本方案要修订的那几句）

- ADR 0011 Context（`:20-22`）：`TableIR / TableCell.verification are pinned PENDING by construction in processing/table_models.py, so a "verified grid" cannot exist without a new qualification rule.`
- ADR 0011 Rejected alternatives（`:147-149`）：`**A VERIFIED TableIR.** The grid's rows, columns and merges are inferred and pinned PENDING; verifying them is a new qualification with no source rule.`
- ADR 0011 BUG-1（`:176-183`）：`canonical-vs-canonical checks, table_models.py, pdfspine_tables.py and the chart geometry are untouched.` —— 本方案**必须**动这两个文件，需 ADR 记录（§3.5）。
- `docs/enterprise-pdf-rag/CLAUDE_HANDOFF.md:46`：拍板"'已验证网格'不可实现，改字面转写"。
- `docs/enterprise-pdf-rag/testing-and-ingestion.md:18`：`单元格引用只证明原文，不证明行列关系`。
- ADR 0009 `:44-48, :55`：笔画来源证明范式（有限正宽 solid stroke、fail closed、"Colour is never an exclusion rule"）—— 本方案的 ruling 规则与之同构。

### 1.3 pdfspine 0.11.0 能力核实（`.venv/bin/python` 现场实测，本次补测的结论标 ★）

| 事实 | 证据 |
|---|---|
| `Page.find_tables(*, strategy="lines", backend=None, vision_options=None, line_max_thickness=3.0, snap_tolerance=3.0, min_line_length=3.0, clip=None, **_ignored) -> TableFinder` | `document.pyi`；仓库只传 `strategy` 与 `clip` |
| `clip=` 在 `lines/lines_strict/text` 策略下**静默丢弃**；未知 strategy 静默退化为默认 | `pdfspine-capabilities.md §3.1`（probe3 实测） |
| `Table` 属性：`bbox,rows,cols,row_count,col_count,header,cells,spans,slots,origin_cells,confidence,source,text_source,metadata`；**没有 lines/edges** | `document.pyi` + `dir(Table)` |
| `Table.rows` / `Table.cols` = **snap 后**的行/列边界坐标（top-left 系），与 cell bbox 逐值一致 | probe1/probe2；★ 本次 A 例 `rows=[30,90,130,170] cols=[20,120,220,280]` 与画线坐标逐值相等 |
| `Page.get_drawings()` 返回 top-left 坐标（与 span、`Table.rows/cols` 同系）；`get_cdrawings()` 是 bottom-left | `pdfspine-capabilities.md §2.3`；★ 本次 `draw_line((20,20),(220,20))` → `get_drawings()[0]['items'][0] == ('l', Point(20,20), Point(220,20))` |
| drawing dict 键集合固定 9 个：`type('s'|'f'|'fs'), rect, color, fill, width, dashes, closePath, even_odd, items`；`items` 元素 `('l', Point, Point)` / `('re', Rect)` / `('c', p0,p1,p2,p3)` | ★ 实测；`Point`/`Rect` 可 `tuple()` 迭代 |
| ★ `draw_rect(rect, width=1)` → `type='s'`, `items=[('re', Rect)]`（一条 path 四条边）；`find_tables(lines)` 能用逐格 `draw_rect` 画的 2×2 识别出表 | 本次 D 例 |
| ★ 细填充矩形（`draw_rect(..., fill=(0,0,0), width=0)`，高 0.5pt）→ `type='f'`, `width=0.0`, `items=[('re', Rect(20,29.75,280,30.25))]`；`find_tables(lines)` **把它当线**，`rows=[30.0,...]`（取中线） | 本次 E 例 —— 真实财报常用细矩形当表格线，必须支持 |
| ★ 合并单元格：首行左两格无竖线 → `origin_cells[0]` `row_span=1,col_span=2`；某列两行间缺横线 → `row_span=2`；`slots` 里 `continuation` 回指 origin | 本次 A 例 |
| ★ 只画外框（`draw_rect` 或四条线）+ 文字 → `find_tables(lines)` **0 张表**；无线 → 0 张表 | 本次 B / B2 / C 例 —— "外框有内线无"在检测阶段就 `result.table is None`，到不了证明阶段 |
| ★ 吸附：一条横线 y=30 只到 x=150、另一条 y=32 从 x=150 起 → 默认 `snap_tolerance=3.0` 得 `rows=[31.0, ...]`（两条线都不在 31.0）；`snap_tolerance=0.5` 得 `rows=[30,32,90,150]`（多出一行） | 本次 F 例 —— 这是 "3.0 吸附 vs 0.5 匹配"冲突的实证 |
| ★ 双线边框（y=30 与 y=32 各一条全宽线 + y=30 重画一次）→ `rows[0] = 30.666…`（三条线平均），任何一条线都不在 0.5pt 内 | 本次实测 |
| ★ 首行无文字的表 → `Table.header == [None, None]`，首行 cell `state='blank'` | 本次实测 |
| ★ `draw_line` 签名 `(p1, p2, *, color=(0,0,0), width=1.0, oc=0, **_ignored)` —— **没有 `dashes` 参数**（传了被吞）；`draw_rect(rect, *, color, fill=None, width, oc)`；`insert_text(point, text, *, fontname='helv', fontsize=11.0, color, fontfile, oc)`；`insert_font(fontname, fontfile, fontbuffer, ...)` | 本次 `inspect.signature` —— 夹具能画：实线、粗线、矩形、细填充矩形、文字；**画不了虚线**（虚线只能靠真实样本或手写 content stream，列入非目标） |
| ★ `Page.filled_rectangles(include_white=False) -> tuple[FilledRectangle(rect, fill), ...]` 与 `get_drawings()` **同为 top-left**（同一填充带两者都给 `(20,30,280,90)`）；填充带下方仍能 `find_tables` 出网格 | 本次实测；用于 `fill` 表头证据 |
| `strategy="lines"` 在 FinTabNet.c 150 页实测召回 21.5% / 精确率 20.6% | `src/ragspine/extraction/tables/structure.py:7-8` —— 本方案定位"有线才证、无线不证"，不改检测策略 |

### 1.4 真实样本观察

- AIA 1–20 页：**0 个 Table 对象**（第 18/20 页数字面板被判成 Group + Text）；p20 敏感度矩阵区域走 `PdfspineTableAdapter` 得 `found 1 page table(s) and 0 exact region match(es)`（`tests/.../test_pdfspine_tables.py:130-176`）。
- `data/ingestion/{3f7233e3…, f41da5ba…}/processing/`：各 1 张合成表（page_index 2，4×2，`bbox=[20,120,300,224]`，行边界 y=120/146/172/198/224，列边界 x=20/150/300），TableIR 与每个 cell `verification` 全 `"pending"`，无任何 ruling 字段；线只留在 `svg.svg`（8 条 `<path d="M20 140L300 140" .../>`，bottom-left，翻转后与 cell 边界逐点吻合）。两个快照 policy 仍是 `…-v2`，`producer = "generic-pdf-processing-v1"`，无 `document_metadata`。→ 这是 §6.5 只读 smoke 的"期望 VERIFIED"样本。
- `data/ingestion` 内 `content_state` 统计：present 84 / blank 12 / unavailable 0；`slots.state` 96 次全 `origin`（无合并）。

---

## 2. 数据模型与模块

### 2.1 `processing/geometry.py` —— 新增线段值对象与覆盖/匹配函数（纯 stdlib）

现有 `COORDINATE_TOLERANCE` / `contains` 原样保留。新增：

```python
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from collections.abc import Sequence

# 线段位置与网格边界的匹配口径，沿用 adapters/pdfspine_tables._contains 的 0.5pt：
# 这是"检测框 ⊂ 版面框"已经在用的、针对 pdfspine 自身浮点/吸附噪声的容差；
# COORDINATE_TOLERANCE (1e-6) 仍只用于 canonical-vs-canonical 的相等判定。
RULING_TOLERANCE = 0.5


class Axis(StrEnum):
    HORIZONTAL = "horizontal"  # 沿 x 延伸，position 是 y
    VERTICAL = "vertical"      # 沿 y 延伸，position 是 x


@dataclass(frozen=True, slots=True)
class Segment:
    """One axis-aligned ruling in page-top-left points (the span / Table.rows frame)."""

    path_index: int   # 在 page.get_drawings() 里的下标
    item_index: int   # 在 drawing["items"] 里的下标
    edge: str         # "l" | "re-top" | "re-bottom" | "re-left" | "re-right" | "re-thin"
    axis: Axis
    position: float   # 横线的 y / 竖线的 x；细填充矩形取中线
    start: float      # 沿轴起点（min）
    end: float        # 沿轴终点（max）
    thickness: float  # 描边 width 或细矩形短边；PDF 宽度 0 = 最细可见线，允许

    def __post_init__(self) -> None:
        values = (self.position, self.start, self.end, self.thickness)
        if (
            self.path_index < 0
            or self.item_index < 0
            or not self.edge
            or not all(isfinite(value) for value in values)
            or self.start >= self.end
            or self.thickness < 0
        ):
            raise ValueError("Ruling segment must be finite, ordered and non-negative")


def coordinate_matches(a: float, b: float, *, tolerance: float = RULING_TOLERANCE) -> bool:
    """Canonical coordinate ``a`` is the same ruling position as ``b`` within ``tolerance`` pt."""
    return abs(a - b) <= tolerance


def rulings_at(
    segments: Sequence[Segment], axis: Axis, position: float, *, tolerance: float = RULING_TOLERANCE
) -> tuple[Segment, ...]:
    return tuple(
        segment
        for segment in segments
        if segment.axis is axis and coordinate_matches(segment.position, position, tolerance=tolerance)
    )


def covering_segments(
    segments: Sequence[Segment],
    axis: Axis,
    position: float,
    start: float,
    end: float,
    *,
    tolerance: float = RULING_TOLERANCE,
) -> tuple[Segment, ...] | None:
    """Rulings at ``position`` whose union covers ``[start, end]`` with no gap over ``tolerance``.

    Returns the segments used, in sweep order, or ``None`` when the edge is not
    continuously ruled. Several collinear pieces may be stitched; a segment fully
    inside the already-covered reach is skipped so the result is deterministic.
    """
    if end - start <= tolerance:
        raise ValueError("Edge to cover must be longer than the tolerance")
    candidates = sorted(
        (
            segment
            for segment in rulings_at(segments, axis, position, tolerance=tolerance)
            if segment.end >= start - tolerance and segment.start <= end + tolerance
        ),
        key=lambda segment: (segment.start, segment.end),
    )
    reach = start
    used: list[Segment] = []
    for segment in candidates:
        if segment.start > reach + tolerance:
            return None  # gap
        if segment.end > reach:
            used.append(segment)
            reach = segment.end
        if reach >= end - tolerance:
            return tuple(used)
    return None


def segments_crossing(
    segments: Sequence[Segment],
    axis: Axis,
    position: float,
    start: float,
    end: float,
    *,
    tolerance: float = RULING_TOLERANCE,
) -> tuple[Segment, ...]:
    """Rulings at ``position`` that run inside the open interval ``(start, end)`` by more than ``tolerance``."""
    return tuple(
        segment
        for segment in rulings_at(segments, axis, position, tolerance=tolerance)
        if min(segment.end, end - tolerance) - max(segment.start, start + tolerance) > tolerance
    )


def ruling_digest(segments: Sequence[Segment]) -> str:
    """Content digest of the page's rulings in paint order; binds evidence to the drawings."""
    return sha256(
        repr(
            tuple(
                (s.path_index, s.item_index, s.edge, s.axis.value, s.position, s.start, s.end, s.thickness)
                for s in segments
            )
        ).encode()
    ).hexdigest()
```

**各容差用在哪（写死在代码注释里）**

| 判定 | 容差 | 理由 |
|---|---|---|
| 线段 `position` ≈ 行/列边界值；cell 边覆盖的端点余量与拼接缝隙；`segments_crossing` 的开区间内缩 | `RULING_TOLERANCE = 0.5` | 与 `pdfspine_tables._contains` 同口径（`:31`），针对 pdfspine 侧噪声；远小于 `snap_tolerance=3.0`，所以吸附出来的边界（F 例 31.0、双线 30.67）**匹配不上** |
| cell bbox 四边 == `rows[row]` / `rows[row+row_span]` / `cols[col]` / `cols[col+col_span]`；`rows[0],rows[-1],cols[0],cols[-1]` == `source.bbox` | `COORDINATE_TOLERANCE = 1e-6` | canonical vs canonical（pdfspine 的 cell bbox 就是由 rows/cols 组出来的），按 ADR 0011 `:180-183` / `testing-and-ingestion.md:170` 纪律不放宽 |
| 表头 `fill` 矩形 ⊇ 行带 bbox | `contains(fill, band, tolerance=0.5)` | 填充矩形常比行带略大/略小 0.x pt |
| 表头 `ruling_thick` 的厚度差 | `> RULING_TOLERANCE` | 0.5pt 以下的线宽差不当"更粗" |

### 2.2 `processing/table_models.py` —— 证据类型 + 解除硬钉

新增（放在 `TableCell` 之前）：

```python
@dataclass(frozen=True, slots=True)
class SegmentRef:
    """A ruling piece in page.get_drawings(): which path/item/edge, and its two endpoints (top-left pt)."""

    path_index: int
    item_index: int
    edge: str
    p0: tuple[float, float]
    p1: tuple[float, float]
    thickness: float

    def __post_init__(self) -> None:
        if self.path_index < 0 or self.item_index < 0 or not self.edge or self.thickness < 0:
            raise ValueError("Segment reference must name a drawing path and a non-negative thickness")
        if self.p0 == self.p1 or not all(isfinite(v) for v in (*self.p0, *self.p1)):
            raise ValueError("Segment reference must have two distinct finite endpoints")


@dataclass(frozen=True, slots=True)
class MergeProof:
    """Interior grid boundaries a merged cell spans, each proved free of rulings inside the cell."""

    interior_rows: tuple[int, ...]  # 行边界下标（在 rows 里），row_span>1 时非空
    interior_cols: tuple[int, ...]  # 列边界下标（在 cols 里），col_span>1 时非空


@dataclass(frozen=True, slots=True)
class CellBorderEvidence:
    top: tuple[SegmentRef, ...]
    bottom: tuple[SegmentRef, ...]
    left: tuple[SegmentRef, ...]
    right: tuple[SegmentRef, ...]
    merge_proof: MergeProof | None = None

    def __post_init__(self) -> None:
        if not (self.top and self.bottom and self.left and self.right):
            raise ValueError("Every cell edge needs at least one ruling segment")


class HeaderEvidenceKind(StrEnum):
    RULING_THICK = "ruling_thick"
    FILL = "fill"
    FONT_BOLD = "font_bold"
    FIRST_ROW_RULE = "first_row_rule"


class HeaderStrength(StrEnum):
    PROVED = "proved"
    HEURISTIC = "heuristic"


@dataclass(frozen=True, slots=True)
class HeaderEvidence:
    kind: HeaderEvidenceKind
    strength: HeaderStrength
    rows: tuple[int, ...]           # 表头行下标（从 0 起连续），列表头时为空
    cols: tuple[int, ...]           # 表头列下标（从 0 起连续），行表头时为空
    segments: tuple[SegmentRef, ...] = ()   # ruling_thick 的粗线；fill 时为空
    fills: tuple[Bounds, ...] = ()          # fill 的矩形

    def __post_init__(self) -> None:
        if bool(self.rows) == bool(self.cols):
            raise ValueError("Header evidence names header rows or header columns, not both or neither")
        if self.kind in (HeaderEvidenceKind.FONT_BOLD, HeaderEvidenceKind.FIRST_ROW_RULE) and (
            self.strength is not HeaderStrength.HEURISTIC
        ):
            raise ValueError("Font and first-row header hints are heuristics, never proofs")
        if self.kind is HeaderEvidenceKind.RULING_THICK and (
            self.strength is HeaderStrength.PROVED and not self.segments
        ):
            raise ValueError("A proved thick-rule header cites its ruling segments")
        if self.kind is HeaderEvidenceKind.FILL and self.strength is HeaderStrength.PROVED and not self.fills:
            raise ValueError("A proved fill header cites its filled rectangles")


@dataclass(frozen=True, slots=True)
class GridEvidence:
    """Why this grid is a source fact: every boundary is a real ruling in the page drawings."""

    rows: tuple[float, ...]         # 行边界 y（len == row_count + 1，严格递增）
    cols: tuple[float, ...]         # 列边界 x（len == col_count + 1，严格递增）
    ruling_digest: str              # geometry.ruling_digest(ruling_segments(page))
    segment_count: int
    tolerance: float                # 证明时用的 RULING_TOLERANCE
    headers: tuple[HeaderEvidence, ...] = ()
    producer: str = "ruled-grid-structure-v1"

    def __post_init__(self) -> None:
        for values in (self.rows, self.cols):
            if len(values) < 2 or any(b - a <= 0 for a, b in zip(values, values[1:], strict=False)):
                raise ValueError("Grid boundaries must be strictly increasing")
        if len(self.ruling_digest) != 64 or self.segment_count < 1 or self.tolerance <= 0:
            raise ValueError("Grid evidence must bind a ruling digest, a segment count and a tolerance")

    def proved_header_rows(self) -> frozenset[int]:
        return frozenset(r for h in self.headers if h.strength is HeaderStrength.PROVED for r in h.rows)

    def proved_header_cols(self) -> frozenset[int]:
        return frozenset(c for h in self.headers if h.strength is HeaderStrength.PROVED for c in h.cols)
```

> 与任务书字段名的两处出入：(1) `SegmentRef` 多了 `item_index` / `edge` / `thickness` —— `('re', Rect)` 一个 item 出四条边，只有 `path_index` 不唯一；`thickness` 是 `ruling_thick` 表头证据的依据。(2) `GridEvidence.header: HeaderEvidence | None` 改为 `headers: tuple[HeaderEvidence, ...]` —— 多级表头 + 行/列表头可同时存在，且要同时保留 heuristic 与 proved 两级供审阅。

**`TableCell` 改动（`:23-38`）**

```python
@dataclass(frozen=True, slots=True)
class TableCell:
    ...（原字段不动）
    verification: Verification = Verification.PENDING
    border: CellBorderEvidence | None = None          # 新增，放在最后，默认 None

    def __post_init__(self) -> None:
        # 原 :37-38 两行替换为：
        if self.verification is Verification.REJECTED:
            raise ValueError("Inferred table cells are pending or verified, never rejected")
        if (self.verification is Verification.VERIFIED) != (self.border is not None):
            raise ValueError(
                "Inferred table cells must remain pending unless a producer supplies border evidence"
            )
        ...（:39-63 原样）
```

**`TableIR` 改动（`:79-94`）**

```python
@dataclass(frozen=True, slots=True)
class TableIR:
    ...（原字段不动）
    verification: Verification = Verification.PENDING
    diagnostics: tuple[str, ...] = ()
    grid_evidence: GridEvidence | None = None         # 新增，放在最后，默认 None

    def __post_init__(self) -> None:
        # 原 :91-94 替换为：
        if (self.verification is Verification.VERIFIED) != (self.grid_evidence is not None):
            raise ValueError(
                "Inferred table IR must remain pending unless a producer supplies grid evidence"
            )
        if any(cell.verification is not self.verification for cell in self.cells):
            raise ValueError("Table cells share the table's grid verification")
        ...（:95-140 原样）
        if self.grid_evidence is not None:
            self._check_grid_evidence()   # 见下

    def _check_grid_evidence(self) -> None:
        evidence = self.grid_evidence
        assert evidence is not None
        rows, cols = evidence.rows, evidence.cols
        if len(rows) != self.row_count + 1 or len(cols) != self.col_count + 1:
            raise ValueError("Grid evidence boundaries do not match the table dimensions")
        x0, y0, x1, y1 = self.source.bbox
        if any(
            abs(a - b) > COORDINATE_TOLERANCE
            for a, b in ((cols[0], x0), (rows[0], y0), (cols[-1], x1), (rows[-1], y1))
        ):
            raise ValueError("Grid evidence boundaries do not match the table bbox")
        for cell in self.cells:
            cx0, cy0, cx1, cy1 = cell.bbox
            expected = (
                cols[cell.col], rows[cell.row], cols[cell.col + cell.col_span], rows[cell.row + cell.row_span]
            )
            if any(abs(a - b) > COORDINATE_TOLERANCE for a, b in zip(cell.bbox, expected, strict=True)):
                raise ValueError("Table cell bbox is not aligned to the proved grid boundaries")
            border = cell.border
            assert border is not None
            for refs, axis_position, lo, hi in (
                (border.top, cy0, cx0, cx1), (border.bottom, cy1, cx0, cx1),
                (border.left, cx0, cy0, cy1), (border.right, cx1, cy0, cy1),
            ):
                # 结构一致性（非几何证明；几何证明在 table_grid_proof.prove_grid）：
                # 每个引用的线段确实躺在这条边的位置上
                for ref in refs:
                    horizontal = ref.p0[1] == ref.p1[1]
                    pos = ref.p0[1] if horizontal else ref.p0[0]
                    if abs(pos - axis_position) > evidence.tolerance:
                        raise ValueError("Cell border evidence does not lie on the cell edge")
            spans_more = cell.row_span > 1 or cell.col_span > 1
            if spans_more != (border.merge_proof is not None):
                raise ValueError("Merged cells carry a merge proof; single cells do not")
            if border.merge_proof is not None and (
                border.merge_proof.interior_rows != tuple(range(cell.row + 1, cell.row + cell.row_span))
                or border.merge_proof.interior_cols != tuple(range(cell.col + 1, cell.col + cell.col_span))
            ):
                raise ValueError("Merge proof must name exactly the interior boundaries the cell spans")
        for header in evidence.headers:
            if any(r >= self.row_count for r in header.rows) or any(c >= self.col_count for c in header.cols):
                raise ValueError("Header evidence names rows or columns outside the grid")
        for slots in self.slots:
            if any(slot.state is SlotState.UNKNOWN for slot in slots):
                raise ValueError("A verified grid has no unknown slots")
```

`table_models.py` 需新 import：`from enterprise_pdf_rag.processing.geometry import COORDINATE_TOLERANCE`（同包，白名单内）。

**语义**：`verification` 仍是普通字段（pydantic `TypeAdapter(TableIR)` 往返需要），但 `__post_init__` 把它变成"由证据推导"：有 `grid_evidence` ⇔ VERIFIED；没有 ⇔ PENDING。旧 producer 不传新字段 → 行为不变（G3）。`REJECTED` 对 IR 无意义，禁止。

**冻结测试 `tests/enterprise_pdf_rag/processing/test_table_grid.py:232-266` 的改法**（不是删除，是拆成两条）：

```python
def test_table_without_grid_evidence_stays_pending() -> None:
    # 原 :233-245：VERIFIED 但无 border → 仍拒绝（消息改为 match="border evidence"）
    with pytest.raises(ValueError, match="border evidence"):
        TableCell("cell", 0, 0, 1, 1, (0.0, 0.0, 10.0, 10.0), ("span",), "value",
                  CellContentState.PRESENT, Verification.VERIFIED)
    cell = TableCell("cell", 0, 0, 1, 1, (0.0, 0.0, 10.0, 10.0), ("span",), "value", CellContentState.PRESENT)
    # 原 :257-266：VERIFIED 但无 grid_evidence → 仍拒绝
    with pytest.raises(ValueError, match="grid evidence"):
        TableIR("table", _source(), 1, 1, (cell,), ((TableSlot(SlotState.ORIGIN, "cell"),),), Verification.VERIFIED)
    # 新增：PENDING 但带证据也拒绝（证据 ⇔ VERIFIED 是双向的）
    with pytest.raises(ValueError, match="grid evidence"):
        TableIR("table", _source(), 1, 1, (cell,), ((TableSlot(SlotState.ORIGIN, "cell"),),),
                grid_evidence=_evidence(rows=(0.0, 10.0), cols=(0.0, 10.0)))


def test_table_with_grid_evidence_is_verified() -> None:
    border = _border(top_y=0.0, bottom_y=10.0, left_x=0.0, right_x=10.0)   # 4 条 SegmentRef
    cell = TableCell("cell", 0, 0, 1, 1, (0.0, 0.0, 10.0, 10.0), ("span",), "value",
                     CellContentState.PRESENT, Verification.VERIFIED, border)
    table = TableIR("table", SourceAnchor("manifest", "a" * 64, 19, (0.0, 0.0, 10.0, 10.0)), 1, 1,
                    (cell,), ((TableSlot(SlotState.ORIGIN, "cell"),),), Verification.VERIFIED,
                    grid_evidence=_evidence(rows=(0.0, 10.0), cols=(0.0, 10.0)))
    assert table.verification is Verification.VERIFIED and table.cells[0].verification is Verification.VERIFIED
    # 反例：证据边界与 cell bbox 不对齐 / 引用的线段不在边上 / 单格却带 merge_proof
    with pytest.raises(ValueError, match="aligned"):
        TableIR(..., grid_evidence=_evidence(rows=(0.0, 12.0), cols=(0.0, 10.0)))
    with pytest.raises(ValueError, match="lie on the cell edge"):
        TableCell(..., Verification.VERIFIED, _border(top_y=3.0, ...)) 后组 TableIR
    with pytest.raises(ValueError, match="merge proof"):
        ... border 带 MergeProof((), ()) 的单格
```

`:19` `test_grid_preserves_merged_blank_unavailable_and_unknown_slots` 的 `:86 verification is PENDING` 断言不动（手工 IR 无证据）。`:75 test_table_transcription.py` 同理不动。

### 2.3 `processing/table_grid_proof.py`（新文件，纯 stdlib）—— 证明规则本体

producer（adapter）与 validator（`literal_qualification`）共用，同 `check_table_transcription` 的"producer 与 validator 共享同一条规则"模式。

```python
"""Prove a native table's grid from the page's rulings; anything unproved stays pending.

A boundary counts only when a real axis-aligned ruling lies on it; a cell edge counts
only when rulings cover it end to end; a merge counts only when the interior
boundaries it spans carry no ruling inside the cell. Nothing here reads text.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from enterprise_pdf_rag.documents.models import Bounds, TextSpan
from enterprise_pdf_rag.figures.models import Verification
from enterprise_pdf_rag.processing.geometry import (
    COORDINATE_TOLERANCE, RULING_TOLERANCE, Axis, Segment, contains,
    covering_segments, ruling_digest, rulings_at, segments_crossing,
)
from enterprise_pdf_rag.processing.table_models import (
    CellBorderEvidence, GridEvidence, HeaderEvidence, HeaderEvidenceKind, HeaderStrength,
    MergeProof, SegmentRef, SlotState, TableIR,
)

GRID_SCOPE = "ruled-grid-structure-v1"


@dataclass(frozen=True, slots=True)
class GridRejection:
    reason: str
    cell_id: str | None = None


@dataclass(frozen=True, slots=True)
class GridProof:
    evidence: GridEvidence
    borders: Mapping[str, CellBorderEvidence]   # cell_id -> border


def segment_ref(segment: Segment) -> SegmentRef:
    if segment.axis is Axis.HORIZONTAL:
        p0, p1 = (segment.start, segment.position), (segment.end, segment.position)
    else:
        p0, p1 = (segment.position, segment.start), (segment.position, segment.end)
    return SegmentRef(segment.path_index, segment.item_index, segment.edge, p0, p1, segment.thickness)


def strip_grid_evidence(table: TableIR) -> TableIR:
    """The same grid as an unproved observation (validators re-prove from this)."""
    return replace(
        table,
        verification=Verification.PENDING,
        grid_evidence=None,
        cells=tuple(replace(cell, verification=Verification.PENDING, border=None) for cell in table.cells),
    )


def prove_grid(
    table: TableIR,
    segments: Sequence[Segment],
    *,
    rows: Sequence[float],
    cols: Sequence[float],
    fills: Sequence[Bounds] = (),
    spans: Sequence[TextSpan] = (),
    tolerance: float = RULING_TOLERANCE,
) -> GridProof | GridRejection:
    """Bind every boundary, edge and merge of ``table`` to rulings, or say why not.

    ``rows`` / ``cols`` are pdfspine's snapped boundaries (``Table.rows`` / ``Table.cols``);
    they are inputs to be proved, not trusted. ``fills`` (filled rectangles) and ``spans``
    only feed header evidence. The table must be pending and carry no evidence.
    """
    if table.verification is not Verification.PENDING or table.grid_evidence is not None:
        raise ValueError("prove_grid takes an unproved table")
    rows, cols = tuple(float(v) for v in rows), tuple(float(v) for v in cols)
    if len(rows) != table.row_count + 1 or len(cols) != table.col_count + 1:
        return GridRejection("grid boundary count does not match the table dimensions")
    if any(b - a <= tolerance for a, b in zip(rows, rows[1:], strict=False)) or any(
        b - a <= tolerance for a, b in zip(cols, cols[1:], strict=False)
    ):
        return GridRejection("grid boundaries are not strictly increasing beyond the tolerance")
    x0, y0, x1, y1 = table.source.bbox
    if any(abs(a - b) > COORDINATE_TOLERANCE for a, b in ((cols[0], x0), (rows[0], y0), (cols[-1], x1), (rows[-1], y1))):
        return GridRejection("grid boundaries do not match the table bbox")
    if any(slot.state is SlotState.UNKNOWN for line in table.slots for slot in line):
        return GridRejection("grid has unknown slots; an irregular grid is not proved")
    # 规则 1：每条行/列边界上必须有真实线段（吸附出来的边界、双线平均值在这里被拒）
    for index, y in enumerate(rows):
        if not rulings_at(segments, Axis.HORIZONTAL, y, tolerance=tolerance):
            return GridRejection(f"row boundary {index} at y={y!r} has no ruling within {tolerance}pt")
    for index, x in enumerate(cols):
        if not rulings_at(segments, Axis.VERTICAL, x, tolerance=tolerance):
            return GridRejection(f"column boundary {index} at x={x!r} has no ruling within {tolerance}pt")
    borders: dict[str, CellBorderEvidence] = {}
    for cell in table.cells:
        cx0, cy0, cx1, cy1 = cell.bbox
        # 规则 2：行列索引与边界一致（cell.row/col 就是边界在 rows/cols 里的下标）
        expected = (cols[cell.col], rows[cell.row], cols[cell.col + cell.col_span], rows[cell.row + cell.row_span])
        if any(abs(a - b) > COORDINATE_TOLERANCE for a, b in zip(cell.bbox, expected, strict=True)):
            return GridRejection("cell bbox is not aligned to the grid boundaries", cell.cell_id)
        # 规则 3：四边各自被线段连续覆盖（允许多段拼接）
        edges = {
            "top": covering_segments(segments, Axis.HORIZONTAL, cy0, cx0, cx1, tolerance=tolerance),
            "bottom": covering_segments(segments, Axis.HORIZONTAL, cy1, cx0, cx1, tolerance=tolerance),
            "left": covering_segments(segments, Axis.VERTICAL, cx0, cy0, cy1, tolerance=tolerance),
            "right": covering_segments(segments, Axis.VERTICAL, cx1, cy0, cy1, tolerance=tolerance),
        }
        for name, covered in edges.items():
            if covered is None:
                return GridRejection(f"cell ({cell.row},{cell.col}) {name} edge is not continuously ruled", cell.cell_id)
        # 规则 4：合并格由"缺失内线"证明——内部边界在该格跨度内不得有线
        interior_rows = tuple(range(cell.row + 1, cell.row + cell.row_span))
        interior_cols = tuple(range(cell.col + 1, cell.col + cell.col_span))
        for k in interior_rows:
            if segments_crossing(segments, Axis.HORIZONTAL, rows[k], cx0, cx1, tolerance=tolerance):
                return GridRejection(f"merged cell ({cell.row},{cell.col}) has an interior ruling at row boundary {k}", cell.cell_id)
        for k in interior_cols:
            if segments_crossing(segments, Axis.VERTICAL, cols[k], cy0, cy1, tolerance=tolerance):
                return GridRejection(f"merged cell ({cell.row},{cell.col}) has an interior ruling at column boundary {k}", cell.cell_id)
        merge_proof = MergeProof(interior_rows, interior_cols) if (interior_rows or interior_cols) else None
        borders[cell.cell_id] = CellBorderEvidence(
            *(tuple(segment_ref(s) for s in edges[name]) for name in ("top", "bottom", "left", "right")),  # type: ignore[arg-type]
            merge_proof,
        )
    evidence = GridEvidence(
        rows, cols, ruling_digest(segments), len(segments), tolerance,
        header_evidence(table, segments, rows=rows, cols=cols, fills=fills, spans=spans, tolerance=tolerance),
    )
    return GridProof(evidence, borders)


def verified_table(table: TableIR, proof: GridProof) -> TableIR:
    """Attach the proof; cell ids are untouched (content_id never included verification)."""
    if set(proof.borders) != {cell.cell_id for cell in table.cells}:
        raise ValueError("Grid proof does not cover exactly the table's cells")
    return replace(
        table,
        verification=Verification.VERIFIED,
        grid_evidence=proof.evidence,
        cells=tuple(
            replace(cell, verification=Verification.VERIFIED, border=proof.borders[cell.cell_id])
            for cell in table.cells
        ),
    )


def check_grid_evidence(table: TableIR, segments: Sequence[Segment], *, fills: Sequence[Bounds] = (), spans: Sequence[TextSpan] = ()) -> None:
    """Raise unless ``table``'s stored evidence re-proves from ``segments`` (validator side)."""
    if table.verification is not Verification.PENDING:
        assert table.grid_evidence is not None
        pending = strip_grid_evidence(table)
        proof = prove_grid(
            pending, segments, rows=table.grid_evidence.rows, cols=table.grid_evidence.cols,
            fills=fills, spans=spans, tolerance=table.grid_evidence.tolerance,
        )
        if isinstance(proof, GridRejection):
            raise ValueError(f"Table grid evidence does not re-prove from the pinned source: {proof.reason}")
        if verified_table(pending, proof) != table:
            raise ValueError("Table grid evidence differs from the re-proved grid")
```

**表头证据 `header_evidence(...)`（同文件）**

```python
def header_evidence(
    table: TableIR, segments: Sequence[Segment], *, rows, cols, fills, spans, tolerance
) -> tuple[HeaderEvidence, ...]:
    found: list[HeaderEvidence] = []
    # (a) ruling_thick —— proved：某条内部横边界全宽连续覆盖，其最细线段仍比其它所有内部横边界
    #     的最粗线段粗 > tolerance；表头行 = 该边界以上所有行。必须存在别的内部边界可比较。
    interior = range(1, len(rows) - 1)
    for k in interior:
        covered = covering_segments(segments, Axis.HORIZONTAL, rows[k], cols[0], cols[-1], tolerance=tolerance)
        if covered is None:
            continue
        own = min(s.thickness for s in covered)
        others = [s.thickness for j in interior if j != k for s in rulings_at(segments, Axis.HORIZONTAL, rows[j], tolerance=tolerance)]
        if others and own - max(others) > tolerance:
            found.append(HeaderEvidence(HeaderEvidenceKind.RULING_THICK, HeaderStrength.PROVED,
                                        tuple(range(k)), (), tuple(segment_ref(s) for s in covered)))
            break
    # 同样规则用于竖边界 → 表头列（rows=(), cols=range(k)）
    ...
    # (b) fill —— proved：从第 0 行起连续若干行的整行带 (cols[0], rows[r], cols[-1], rows[r+1])
    #     各被某个填充矩形包含（contains(fill, band, tolerance=tolerance)），且不是全部行；
    #     列表头同理。
    ...
    # (c) font_bold —— heuristic：第 0..k-1 行所有 PRESENT cell 的 span.font 都含 "Bold"（大小写不敏感），
    #     第 k 行不满足。只作审阅提示。
    ...
    # (d) first_row_rule —— heuristic：row_count >= 2 且第 0 行至少一个 PRESENT cell → rows=(0,)。永远记录，永远不参与引用。
    if table.row_count >= 2 and any(c.row == 0 and c.content_state is CellContentState.PRESENT for c in table.cells):
        found.append(HeaderEvidence(HeaderEvidenceKind.FIRST_ROW_RULE, HeaderStrength.HEURISTIC, (0,), ()))
    return tuple(found)
```

`proved` 只可能来自 (a)(b)；(c)(d) 构造时就被 `HeaderEvidence.__post_init__` 钉为 heuristic。

### 2.4 `adapters/pdfspine_tables.py` —— producer

新增两个模块级函数 + 改 `extract` / `_map_table`：

```python
from enterprise_pdf_rag.documents.models import Bounds
from enterprise_pdf_rag.processing.geometry import COORDINATE_TOLERANCE, Axis, Segment
from enterprise_pdf_rag.processing.table_grid_proof import (
    GridRejection, prove_grid, verified_table,
)

LINE_MAX_THICKNESS = 3.0  # 与 find_tables(line_max_thickness=3.0) 默认值一致：pdfspine 自己不当线的，我们也不当线


def ruling_segments(page: pdfspine.Page, *, line_max_thickness: float = LINE_MAX_THICKNESS) -> tuple[Segment, ...]:
    """Axis-aligned solid strokes and thin filled rectangles from ``page.get_drawings()``.

    ``get_drawings()`` is page-top-left like text spans and ``Table.rows``/``cols``;
    never use ``get_cdrawings()`` here (PDF bottom-left). Dashed paths, diagonal lines,
    curves and strokes thicker than ``line_max_thickness`` are not rulings.
    """
    segments: list[Segment] = []
    for path_index, drawing in enumerate(page.get_drawings()):
        kind = str(drawing.get("type", ""))
        if drawing.get("dashes") not in (None, ""):
            continue                                    # 虚线：不证（fail closed）
        width = float(drawing.get("width") or 0.0)
        stroked, filled = "s" in kind, "f" in kind
        for item_index, item in enumerate(drawing["items"]):
            op = item[0]
            if op == "l" and stroked and width <= line_max_thickness:
                (ax, ay), (bx, by) = tuple(item[1]), tuple(item[2])
                if abs(ay - by) <= COORDINATE_TOLERANCE and abs(ax - bx) > COORDINATE_TOLERANCE:
                    segments.append(Segment(path_index, item_index, "l", Axis.HORIZONTAL, ay, min(ax, bx), max(ax, bx), width))
                elif abs(ax - bx) <= COORDINATE_TOLERANCE and abs(ay - by) > COORDINATE_TOLERANCE:
                    segments.append(Segment(path_index, item_index, "l", Axis.VERTICAL, ax, min(ay, by), max(ay, by), width))
            elif op == "re":
                rx0, ry0, rx1, ry1 = (float(v) for v in item[1])
                w, h = rx1 - rx0, ry1 - ry0
                if filled and h <= line_max_thickness and w > h:              # 细填充矩形 = 横线
                    segments.append(Segment(path_index, item_index, "re-thin", Axis.HORIZONTAL, (ry0 + ry1) / 2, rx0, rx1, h))
                elif filled and w <= line_max_thickness and h > w:            # 细填充矩形 = 竖线
                    segments.append(Segment(path_index, item_index, "re-thin", Axis.VERTICAL, (rx0 + rx1) / 2, ry0, ry1, w))
                elif stroked and width <= line_max_thickness and w > line_max_thickness and h > line_max_thickness:
                    segments.extend((                                           # 描边矩形 = 四条边
                        Segment(path_index, item_index, "re-top", Axis.HORIZONTAL, ry0, rx0, rx1, width),
                        Segment(path_index, item_index, "re-bottom", Axis.HORIZONTAL, ry1, rx0, rx1, width),
                        Segment(path_index, item_index, "re-left", Axis.VERTICAL, rx0, ry0, ry1, width),
                        Segment(path_index, item_index, "re-right", Axis.VERTICAL, rx1, ry0, ry1, width),
                    ))
            # 'c'（贝塞尔）、斜线、超厚线：忽略
    return tuple(segments)


def fill_rectangles(page: pdfspine.Page) -> tuple[Bounds, ...]:
    """Non-white filled rectangles (header bands); page-top-left like ``get_drawings``."""
    return tuple(
        tuple(float(v) for v in rectangle.rect)  # type: ignore[return-value]
        for rectangle in page.filled_rectangles(include_white=False)
    )
```

> 已实测：`filled_rectangles()` 与 `get_drawings()` 同为 top-left（§1.3 ★），无需翻转。

`extract`（`:53-88`）改动：`:68` 保留 `find_tables(strategy="lines", clip=item.bbox)`（clip 是 no-op，加一行注释：`# clip is ignored by the lines strategy; the _contains filter below is what scopes the match`），`:86` 改为 `return self._map_table(matches[0], page=page, item=item, source_page=source_page)`。

`_map_table`（`:110-205`）改动：签名加 `source_page: pdfspine.Page`；`:185-188` diagnostics 第二条去掉 "and remain pending"；`:189-203` 之后：

```python
        pending = TableIR(item.object_id, SourceAnchor(...), table.row_count, table.col_count, tuple(cells), slots, diagnostics=diagnostics)
        segments = ruling_segments(source_page)
        proof = prove_grid(
            pending, segments,
            rows=table.rows, cols=table.cols,
            fills=fill_rectangles(source_page),
            spans=spans_in_table,
        )
        if isinstance(proof, GridRejection):
            diagnostics = (*diagnostics, f"Grid structure pending: {proof.reason} (rulings={len(segments)}).")
            return TableExtractionResult(replace(pending, diagnostics=diagnostics), diagnostics)
        diagnostics = (*diagnostics, f"Grid structure proved from {len(segments)} ruling segment(s); scope={GRID_SCOPE}.")
        return TableExtractionResult(verified_table(replace(pending, diagnostics=diagnostics), proof), diagnostics)
```

`cell_id` 输入（`:141-154`）不变 → `cells.<id>` 引用在证明前后一致。

**现状要点保持**：`find_tables(clip=)` 静默丢弃，事后 `_contains` 过滤照旧；`strategy="lines"` 质量差 → 本方案不改检测，只对"检测到且全划线"的表做证明；`strategy` 白名单校验不做（非目标，见 §9）。

---

## 3. stage 产出与资格判定

### 3.1 产物落盘（推荐：并入现有 `ir` / `table_detection`，不加新 required stage）

| 产物 | 落点 | 内容 |
|---|---|---|
| 网格证据 | **`ir` stage 的 `TableIR` 本身**（`TableIR.grid_evidence` + 每个 `TableCell.border`） | 证据就是 IR 的 verification 依据，跟着 IR 走；`semantic_objects._table:262` 不改 |
| 拒绝原因 | `table_detection` stage 的 `TableExtractionResult.diagnostics` + `TableIR.diagnostics` | `"Grid structure pending: <reason> (rulings=N)."` |
| 资格回执 | `qualification` stage 的 `LiteralQualification` 新增两个可选字段：`grid_scope: str | None = None`（VERIFIED 时 = `"ruled-grid-structure-v1"`）、`ruling_digest: str | None = None` | 让回执显式绑定"这份 ir 的网格证明来自哪一组线"；旧回执缺字段 → None，兼容 |

**为什么不加新 stage 名**：`ProcessingStore.load:115-131` 与 `eligibility:116-120` 只认四个固定名字，新名字要么进 `lineage_refs`（`build:173-203` 现在只给 CHART 填），要么改两处校验；两者都扩大 diff 而不增加保证——证据在 `ir` 里已经是内容寻址、被 `member.ir` 绑定的。备选（不推荐）：新 stage `grid_proof`（`writer.save("grid_proof", TypeAdapter(GridEvidence).dump_json(...))`）+ TABLE 走 `lineage_refs=(grid_proof,)`，需改 `build:173-203` 加 TABLE 分支与 `processing_store.py:130` 无需改。

### 3.2 资格判定：网格证明作为 qualification 的第二条规则叠加

- `eligibility()`（`processing_retrieval.py:101-130`）**不改**：TABLE 仍以字面转写 VERIFIED 放行；VERIFIED 与 PENDING 网格都进快照（PENDING 表仍可被 `cells.<id>` 纯文本引用，现状不变）。
- `validate_literal_member`（`literal_qualification.py:109-123`）TABLE 分支叠加第二条规则（见 §5）：若 `table.verification is VERIFIED` → 重新 `ruling_segments` + `check_grid_evidence`，并核对 `receipt.grid_scope == GRID_SCOPE` 且 `receipt.ruling_digest == table.grid_evidence.ruling_digest`；若 PENDING → 要求 `receipt.grid_scope is None`。这样索引时（`build:217`）与回答时（`resolve_processing_context` → `_qualified`）都会重证。
- 不新建 `adapters/table_grid_qualification.py`：规则本体在 `processing/table_grid_proof.py`（纯），pdfspine 取线在 `pdfspine_tables.ruling_segments`，两处都已存在，不需要第三个文件。

### 3.3 `semantic_objects._table` 的 diagnostic 文案（`:247-283`）

- `:253` / `:257`（无表）不变。
- `:270-280`（转写失败）：diagnostic 末尾追加 `grid=<verified|pending>`，便于审阅页看出"网格已证但文字没过"。
- 成功分支 `:287-295`：`LiteralQualification(..., grid_scope=GRID_SCOPE if result.table.verification is VERIFIED else None, ruling_digest=... or None)`。

### 3.4 policy v5：与 diagram / formula 方案共用同一次升级

本方案**本身不需要改任何 policy 字符串**：索引文本仍是 description（`member_index_text:85-89` TABLE 无投影）；`eligibility` 不变；`row/col/header` 的开放按成员的 `ir.verification` 判定，不按 policy。快照 id 会因 `ir.json` 字节变化而自然换新。

但 diagram / formula 方案要新增可检索 kind → `_POLICY` 升 v5。**已拍板（§10）：这四个常量由 diagram 方案唯一编辑**，本方案与 formula 方案只做语义确认；`DraftQualification.qualification_policy`（`draft_publication.py:25-27` 的 `Literal[...-v2]`）**不升 v3**——那串描述的是"按 kind + stage 完整性判资格"这个**方法**，不是 kind 集合本身（ADR 0013 加 metadata stage 时同样没有升它），且 diagram / formula 两份方案都把 `draft_publication.py` 明确列在"不改"清单里。四步模板（照抄 ADR 0013 的 v3→v4 diff，`3555708..c15525b -- adapters/processing_retrieval.py`）：

1. `processing_retrieval.py:60` `_POLICY = "source-transcription-and-scoped-chart-qualification-v5"`（三份方案的取值逐字一致）；注释追加一行 `v5 embeds the qualified-IR projection of Diagram and Formula members (ADR 0015)`（逐字以 diagram 方案 §4.2 为准；本方案的网格证据在 `ir` 资产里，不是 policy 语义，不进这条注释）。
2. `:66-72` `PROJECTED_CHART_POLICIES` 加入 `"...-v4"`（旧值保留，新值经 `_POLICY` 自动进入）。
3. `:74` `CONTEXTUAL_POLICIES = frozenset({_POLICY, "...-v4"})`（v4 快照仍拼页头）。
4. 新增门控集合 `VISUAL_PROJECTION_POLICIES = frozenset({_POLICY})` 与 `member_text` 里 Diagram / Formula 两支投影门——**全部在 diagram 方案里**；本方案**零读取侧 policy 分支**，不新增也不修改任何 `if policy in ...`；重跑 `metadata → index → publish` 只切 `current-processing` 指针。

因为 `DraftQualification.qualification_policy` 保持 `...-v2`（见上），`tests/.../test_generic_publication_e2e.py:182` 的断言也**不需要改**。

### 3.5 ADR：新 ADR 0014 + ADR 0011 一处旁注

- 新增 `docs/enterprise-pdf-rag/adr/0014-ruled-table-grid-proof.md`（Status: Accepted；`Amends ADR 0011 rejected alternative "A VERIFIED TableIR"`）。章节：Context（引 ADR 0011 `:147-149` 原文 + pdfspine 实测 §1.3）、Decision（§2.1–2.4 的规则 1–4 + 表头证据等级 + 容差表 + fail closed 清单 + "无线不证" + 消费侧 row/col/header 只对 VERIFIED 开放）、Rejected alternatives（① 用 `snap_tolerance` 作匹配容差 → 会把吸附出来的边界当证据；② 新 `grid_proof` stage；③ header heuristic 参与 VERIFIED；④ `strategy="text"` 表也证）、Consequences（`table_models.py` / `pdfspine_tables.py` 从 ADR 0011 BUG-1 的"不动"名单移出；`data/ingestion` 两个 v2 快照仍可挂载，重建后表变 VERIFIED）。
- ADR 0011 `:147-149` 段末追加一句：`Superseded for ruled tables by [ADR 0014](0014-ruled-table-grid-proof.md), which supplies the source rule.`（ADR 正文其余不动；ADR 0011 `:20-22` 与 `:176-183` 属历史陈述，不改）。
- ADR 编号协调（**已拍板，§10**）：**ADR 0014 = 本方案（划线表格网格的来源证明，含 ADR 0011 Rejected alternative 的旁注）；ADR 0015 = Diagram + Formula 可检索（两份方案合写一份，因为它们是同一次 policy v5 升级）**。不存在 ADR 0016。编号与合并先后无关，先合并的那份也不占用对方的号。
- 同步文档：`CLAUDE_HANDOFF.md:46` 追加"2026-09-2x：ADR 0014 补了 source rule，划线表的网格可 VERIFIED"；`testing-and-ingestion.md:18` 把"不证明行列关系"改为"划线表证明行列关系（ADR 0014），无线表仍只证明原文"；两份文档都有 `covers:`/`verified-against:` frontmatter，改后跑 `make drift`。

---

## 4. 索引 / 检索 / 回答链接入点

### 4.1 eligibility（`processing_retrieval.py:101-130`）—— 不改

VERIFIED 与 PENDING 表都放行（现状）。**只有 VERIFIED 表允许 `row/col/header` 引用**；纯文本 `cells.<id>` 引用两者都行（现状）。理由：PENDING 表的文字仍是逐字证明过的，拒掉它会把 `data/ingestion` 之类旧快照和所有无线表的 TableQA 一起拒掉，收益为负。

### 4.2 index_text —— 不改

`member_index_text:85-89` TABLE 走 description，网格结构不进索引文本（"cells." / "row" 不得出现在 embedder 输入里，e2e `:212` 断言保持）。

### 4.3 `processing/context_builder.py`

`CellEvidence`（`:46-56`）新增两个默认字段：

```python
@dataclass(frozen=True, slots=True)
class HeaderRef:
    cell_id: str
    text: str
    axis: str            # "row" | "col"


@dataclass(frozen=True, slots=True)
class CellEvidence:
    ...（原 9 个字段）
    verification: Verification = Verification.PENDING
    headers: tuple[HeaderRef, ...] = ()   # 只放 proved 表头；heuristic 永不进这里
```

`ContextBlock`（`:70-85`）新增 `grid_verification: Verification = Verification.PENDING`（`verification` 字段继续表示转写资格，不动 `:192` 的语义，避免 e2e `:228` 与 `test_context_builder.py:323/:337` 的 `verification=verified` 断言含义漂移）。

`prompt_text()` TABLE 分支（`:109-116`）改为：

```python
        elif self.kind is BlockKind.TABLE:
            lines.append(
                f"table rows={self.row_count} cols={self.col_count} grid={self.grid_verification.value}"
            )
            for cell in self.cells:
                if cell.content_state is CellContentState.PRESENT:
                    shown = cell.text if cell.text is not None else "<UNAVAILABLE>"
                else:
                    shown = f"<{cell.content_state.name}>"
                line = f"cells.{cell.cell_id} ({cell.row},{cell.col}): {shown}"
                if self.grid_verification is Verification.VERIFIED:
                    header = " | ".join(f'"{h.text}"' for h in cell.headers) or "<NONE>"
                    line += f" row={cell.row} col={cell.col} header={header}"
                lines.append(line)
```

现有断言 `"cells.c-1 (0,0): Revenue" in rendered`（`test_context_builder.py:257`、`:335`；`test_generic_publication_e2e.py:231`）是子串匹配，追加后缀不破坏。

`build_context_block` TableIR 分支（`:182-210`）：

```python
    if isinstance(ir, TableIR):
        ...
        header_cells = _header_cells(ir)   # 见下
        return ContextBlock(
            *common, BlockKind.TABLE, member.page_index, context.scope,
            context.description.verification,      # :192 不变：转写资格
            context.description.text,
            cells=tuple(
                CellEvidence(cell.cell_id, cell.row, cell.col, cell.row_span, cell.col_span, cell.bbox,
                             cell.text, cell.content_state, cell.source_span_ids,
                             cell.verification, _headers_for(cell, header_cells))
                for cell in ir.cells
            ),
            row_count=ir.row_count, col_count=ir.col_count,
            grid_verification=ir.verification,
        )


def _header_cells(ir: TableIR) -> tuple[tuple[TableCell, str], ...]:
    """PRESENT cells lying in a proved header row ("row") or proved header column ("col")."""
    if ir.grid_evidence is None:
        return ()
    rows, cols = ir.grid_evidence.proved_header_rows(), ir.grid_evidence.proved_header_cols()
    found = []
    for cell in ir.cells:
        if cell.content_state is not CellContentState.PRESENT or cell.text is None:
            continue
        if cell.row in rows:
            found.append((cell, "row"))
        if cell.col in cols:
            found.append((cell, "col"))
    return tuple(found)


def _headers_for(cell: TableCell, header_cells) -> tuple[HeaderRef, ...]:
    """Header cells whose column span covers this cell's column (row headers) or whose row span covers its row (column headers); a header cell never heads itself."""
    refs = []
    for header, axis in header_cells:
        if header.cell_id == cell.cell_id:
            continue
        if axis == "row" and header.col <= cell.col < header.col + header.col_span and header.row < cell.row:
            refs.append(HeaderRef(header.cell_id, header.text, "row"))
        elif axis == "col" and header.row <= cell.row < header.row + header.row_span and header.col < cell.col:
            refs.append(HeaderRef(header.cell_id, header.text, "col"))
    return tuple(refs)
```

多级表头："Group A"（row 0，col_span 2）与 "Value"（row 1，col 1）都覆盖 cell (2,1) → `header="Group A" | "Value"`；claim 的 `header` 等于其中任何一个即可。`:185-186` 的注释 `# The inferred grid is pinned PENDING; ...` 改为 `# Transcription verification (description) and grid verification (ir) are separate facts.`

### 4.4 `answers/prompt.py`

`ModelClaim`（`:17-23`）加三个可选字段（`strict=True` 下 `int | None` 只收整数或 null；`extra="forbid"` 不变）：

```python
class ModelClaim(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    claim_id: str
    member_id: str
    kind: Literal["quote", "cell", "chart_value"]
    field_path: str
    text: str
    row: int | None = None
    col: int | None = None
    header: str | None = None
```

`SYSTEM_RULES` 规则 1（`:45-49`）末尾追加一句（其余规则原样）：

```
"   A `cell` claim may also carry `row`, `col` and `header`, copied exactly from the "
"`row=… col=… header=\"…\"` suffix printed after that cell; only cells in a block whose "
"table line says `grid=verified` print that suffix, and `header` must be one of the quoted "
"header texts, verbatim. Never add row, col or header to a cell that prints none.\n"
```

> 改 `SYSTEM_RULES` 会改 `request_fingerprint`（`answer_service` 用 system + prompt 指纹做缓存）——旧缓存条目失效属预期，`test_answer_service.py:158` 的 `"instructions" in SYSTEM_RULES` 不受影响。

### 4.5 `answers/models.py`

`ClaimCitation`（`:109-119`）追加默认字段：

```python
    page_title: str | None = None
    row: int | None = None            # VERIFIED 网格时填 cell.row
    col: int | None = None            # VERIFIED 网格时填 cell.col
    header: str | None = None         # 仅当 claim 引用了表头
    header_cell_id: str | None = None # 该表头 cell 的 id（也进 evidence_ids）
```

`VerifiedClaim`（`:122-129`）不改。`AbstainReason` 不加新值（用现有 `CLAIM_NOT_IN_EVIDENCE` / `VALUE_UNAVAILABLE` / `MODEL_OUTPUT_INVALID`）。

### 4.6 `answers/verify.py::_verify_cell`（`:137-165`）

```python
def _verify_cell(claim: ModelClaim, block: ContextBlock) -> VerifiedClaim | RejectedClaim:
    cell_id = claim.field_path.removeprefix("cells.")
    cell = next((item for item in block.cells if item.cell_id == cell_id), None)
    if cell is None:
        return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "cited cell is not in the block")
    if cell.content_state is not CellContentState.PRESENT or cell.text is None:
        return _reject(claim, AbstainReason.VALUE_UNAVAILABLE, f"cell content is {cell.content_state.value}")
    if _norm(claim.text) != _norm(cell.text):
        return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claim text differs from cell")
    header: HeaderRef | None = None
    relational = (claim.row, claim.col, claim.header)
    if any(value is not None for value in relational):
        if block.grid_verification is not Verification.VERIFIED or cell.verification is not Verification.VERIFIED:
            return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "grid relations of this table are not verified")
        if (claim.row is not None and claim.row != cell.row) or (claim.col is not None and claim.col != cell.col):
            return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claimed row/col differ from the cell")
        if claim.header is not None:
            header = next((h for h in cell.headers if h.text == claim.header), None)   # 逐字：== ，不 _norm
            if header is None:
                return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claimed header is not a proved header of the cell")
    grid_verified = block.grid_verification is Verification.VERIFIED
    return VerifiedClaim(
        claim.claim_id, ClaimKind.CELL, claim.text, None, None,
        (
            ClaimCitation(
                block.member_id, block.kind, block.page_index, claim.field_path,
                (cell.cell_id, *cell.source_span_ids, *((header.cell_id,) if header else ())),
                cell.bbox, cell.text,
                row=cell.row if grid_verified else None,
                col=cell.col if grid_verified else None,
                header=None if header is None else header.text,
                header_cell_id=None if header is None else header.cell_id,
            ),
        ),
    )
```

`verify_claims`（`:293-342`）不改：kind/path 门（`:320-326`）照旧；`row/col/header` 只在 `_verify_cell` 内生效，对 QUOTE / CHART_VALUE claim 若模型误填，`_verify_quote` / `_verify_chart_value` 忽略这些字段（或在 `:320` 前加一行 `if kind is not ClaimKind.CELL and any(...)` → `MODEL_OUTPUT_INVALID`；推荐加，两行）。

### 4.7 chat 引用字段（HTTP 层）

`adapters/http/chat_schemas.py` 若把 `ClaimCitation` 逐字段映射成响应模型（ADR 0013 时加了 `page_title`），需同样追加 `row/col/header/header_cell_id` 四个 `Optional`；若是 `asdict` 直出则自动带上。实施时 `git grep -n page_title src/enterprise_pdf_rag/adapters/http/` 定位，与 `page_title` 同处同法。

---

## 5. 既有文件最小改动清单

| 文件 | 函数 / 位置 | 改动一句话 | 行号（c15525b） |
|---|---|---|---|
| `processing/geometry.py` | 模块尾 | 新增 `RULING_TOLERANCE`、`Axis`、`Segment`、`coordinate_matches`、`rulings_at`、`covering_segments`、`segments_crossing`、`ruling_digest`；`contains` 不动 | 追加于 `:24` 后 |
| `processing/table_models.py` | 模块顶 | import `COORDINATE_TOLERANCE`；新增 `SegmentRef` / `MergeProof` / `CellBorderEvidence` / `HeaderEvidenceKind` / `HeaderStrength` / `HeaderEvidence` / `GridEvidence` | `:1-8` import；类插在 `:20` 后 |
| 同上 | `TableCell` | 加 `border: CellBorderEvidence | None = None`；`__post_init__` 硬钉改为 "VERIFIED ⇔ border" | `:34` 后加字段；`:36-38` 替换 |
| 同上 | `TableIR` | 加 `grid_evidence: GridEvidence | None = None`；硬钉改为 "VERIFIED ⇔ grid_evidence"、cells 同步；新增 `_check_grid_evidence` | `:88` 后加字段；`:90-94` 替换；方法追加于 `:140` 后 |
| `processing/table_grid_proof.py` | 新文件 | `GridRejection` / `GridProof` / `segment_ref` / `strip_grid_evidence` / `prove_grid` / `verified_table` / `check_grid_evidence` / `header_evidence` / `GRID_SCOPE` | — |
| `processing/typed_ir.py` | `LiteralQualification` | 追加 `grid_scope: str | None = None`、`ruling_digest: str | None = None` | `:132` 后 |
| `adapters/pdfspine_tables.py` | 模块级 | 新增 `LINE_MAX_THICKNESS`、`ruling_segments(page)`、`fill_rectangles(page)`；import `replace`、`Segment`、`Axis`、`prove_grid`… | `:18` 后 |
| 同上 | `extract` | `:68` 加 clip no-op 注释；`:86` 传 `source_page=` | `:68`、`:86` |
| 同上 | `_map_table` | 签名加 `source_page`；`:187` 去掉 "and remain pending"；构造后 `prove_grid` → `verified_table` 或追加 pending diagnostic | `:111-113`、`:185-205` |
| `adapters/semantic_objects.py` | `_table` | `LiteralQualification(...)` 传 `grid_scope` / `ruling_digest`；转写失败 diagnostic 追加 `grid=` | `:270-280`、`:287-295` |
| `adapters/literal_qualification.py` | `validate_literal_member` TABLE 分支 | 在 `check_table_transcription` 后：VERIFIED → `pdf = sources.get(source.manifest.source)`；`pdfspine.open` → `ruling_segments` / `fill_rectangles` → `check_grid_evidence(table, segments, fills=, spans=text.spans)`；核对 `receipt.grid_scope` / `receipt.ruling_digest`；PENDING → 要求两字段为 None | `:122` 后插入；`:1-16` 加 import `pdfspine`、`ruling_segments`、`fill_rectangles`、`check_grid_evidence`、`GRID_SCOPE` |
| `processing/context_builder.py` | `CellEvidence` / `ContextBlock` / `prompt_text` / `build_context_block` | 新增 `HeaderRef`；`CellEvidence` 加 `verification`、`headers`；`ContextBlock` 加 `grid_verification`；TABLE 渲染加 `grid=` 与 `row= col= header=` 后缀；新增 `_header_cells` / `_headers_for` | `:46-56`、`:70-85`、`:109-116`、`:182-210` |
| `answers/prompt.py` | `ModelClaim` / `SYSTEM_RULES` | 加 `row/col/header` 可选字段；规则 1 追加一句 | `:17-23`、`:45-49` |
| `answers/models.py` | `ClaimCitation` | 追加 `row/col/header/header_cell_id` 默认 None | `:119` 后 |
| `answers/verify.py` | `_verify_cell`；`verify_claims` | 行列/表头校验（§4.6）；非 CELL claim 带 row/col/header → `MODEL_OUTPUT_INVALID` | `:137-165`；`:320` 前 |
| `adapters/http/chat_schemas.py` | `ClaimCitationOut` | 与 `page_title` 同法追加 `row/col/header/header_cell_id` 四个可选字段（`:66-76` 是显式逐字段映射，ADR 0013 刚加过 `page_title`，照抄同一处） | `git grep page_title` 定位 |
| `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` | `ClaimCitationOut` 的属性 | 手工重生成（`check_schema.py:103,107-119` 全等比对且无 `--write`；diagram / formula 方案也各自要改这个文件的 enum，合并后只跑一次重生成脚本，脚本见 formula 方案 §7 阶段 3） | — |
| `docs/enterprise-pdf-rag/adr/0014-ruled-table-grid-proof.md` | 新文件 | §3.5 | — |
| `docs/enterprise-pdf-rag/adr/0011-...md` | Rejected alternatives | 段末一句 "Superseded for ruled tables by ADR 0014" | `:149` 后 |
| `docs/enterprise-pdf-rag/CLAUDE_HANDOFF.md` / `testing-and-ingestion.md` | `:46` / `:18` | 措辞更新 + frontmatter `verified-against` | |

**不动的**：`eligibility()`、`processing_store.py`、`draft_publication.py`（除非承担 v5 常量编辑）、`table_transcription.py`、`source_objects.py`、`index_text.py`、`page_partition.py`、`figures/`、`stroke_visibility.py` / `source_paint.py`（先例参考，不复用代码：它们绑定 SVG glyph / ReplayDevice，网格证明只需 `get_drawings()` 的轴对齐线段，引入 ReplayDevice 反而带来第二套 bottom-left 坐标）。

---

## 6. 测试计划

### 6.1 离线夹具：`tests/enterprise_pdf_rag/adapters/test_pdf_ingestion.py` 的 `authored_pdf(table_page=)` 扩展

现状：`:23-33` 常量、`:36-44` `_draw_table(page, fontname)`、`:47-71` `authored_pdf(..., table_page: bool = False)`；只用 `draw_line(width=1)` / `insert_text` / `insert_font`（`:38-43`、`:61-67`）。本次新增 `draw_rect`（签名见 §1.3，已实测可用）。

```python
from dataclasses import dataclass, field
from collections.abc import Mapping


@dataclass(frozen=True)
class TableSpec:
    """A ruled grid to author: boundaries, cell texts, merges and header styling."""

    rows: tuple[float, ...] = TABLE_ROWS
    cols: tuple[float, ...] = TABLE_COLUMNS
    cells: Mapping[tuple[int, int], str] = field(default_factory=lambda: dict(TABLE_CELLS))
    merges: tuple[tuple[int, int, int, int], ...] = ()   # (row, col, row_span, col_span)
    header_rows: int = 0                                  # header_rule_width 加粗 rows[header_rows]
    header_rule_width: float | None = None
    line_width: float = 1.0
    ruled: bool = True          # False → 无线表（反例）
    frame_only: bool = False    # True → 只画外框 draw_rect（反例）
    split_segments: bool = False  # True → 每条边界按列/行分段画，测多段拼接
    fill_header: bool = False   # True → 表头行带填充矩形（fill 证据）
    text_dy: float = 18.0       # 文字基线相对行顶的偏移；DEFAULT 保持 :43 的 +18 → PDF 字节不变

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.cols[0], self.rows[0], self.cols[-1], self.rows[-1])


DEFAULT_TABLE = TableSpec()   # 与 table_page=True 逐字节相同（整线、先横后竖、width=1）

# 多级表头 + 合并单元格：row 0 "Group" 横跨 col 0-1；(2,2)+(3,2) 纵向合并；rows[2] 为 2.0pt 粗线
MULTI_HEADER_TABLE = TableSpec(
    rows=(48.0, 70.0, 92.0, 114.0, 136.0),
    cols=(20.0, 90.0, 160.0, 220.0),
    cells={(0, 0): "Group", (0, 2): "Unit", (1, 0): "Metric", (1, 1): "Value",
           (2, 0): "Revenue", (2, 1): "1,234", (2, 2): "m", (3, 0): "Margin", (3, 1): "12%"},
    merges=((0, 0, 1, 2), (2, 2, 2, 1)),
    header_rows=2,
    header_rule_width=2.0,
    text_dy=16.0,   # 22pt 行高
)
FILL_HEADER_TABLE = TableSpec(header_rows=1, fill_header=True)   # fill 表头证据（首行灰底）
FRAME_ONLY_TABLE = TableSpec(frame_only=True)     # 反例：外框有内线无 → find_tables 0 张表
UNRULED_TABLE = TableSpec(ruled=False)            # 反例：无线表 → 0 张表
SPLIT_TABLE = TableSpec(split_segments=True)      # 同 DEFAULT 网格，线分段画 → 证明须拼接


def _blocked(spec: TableSpec, *, boundary: int, index: int, horizontal: bool) -> bool:
    """Is the piece of boundary ``boundary`` in column/row ``index`` inside a merged cell?"""
    for row, col, row_span, col_span in spec.merges:
        if horizontal and row < boundary < row + row_span and col <= index < col + col_span:
            return True
        if not horizontal and col < boundary < col + col_span and row <= index < row + row_span:
            return True
    return False


def _runs(spec: TableSpec, *, boundary: int, count: int, horizontal: bool) -> list[tuple[int, int]]:
    """Maximal runs [start, end) of un-blocked pieces along one boundary."""
    runs: list[tuple[int, int]] = []
    start = None
    for index in range(count + 1):
        open_piece = index < count and not _blocked(spec, boundary=boundary, index=index, horizontal=horizontal)
        if open_piece and start is None:
            start = index
        if not open_piece and start is not None:
            runs.append((start, index))
            start = None
    if spec.split_segments:
        return [(i, i + 1) for s, e in runs for i in range(s, e)]
    return runs


def _draw_table(page: pdfspine.Page, fontname: str, spec: TableSpec = DEFAULT_TABLE) -> None:
    row_count, col_count = len(spec.rows) - 1, len(spec.cols) - 1
    if spec.ruled and spec.frame_only:
        page.draw_rect(spec.bbox, width=spec.line_width)
    elif spec.ruled:
        if spec.fill_header:
            for r in range(spec.header_rows):
                page.draw_rect((spec.cols[0], spec.rows[r], spec.cols[-1], spec.rows[r + 1]),
                               color=None, fill=(0.85, 0.85, 0.85), width=0)
        for i, y in enumerate(spec.rows):
            width = spec.header_rule_width if (spec.header_rule_width is not None and i == spec.header_rows) else spec.line_width
            for start, end in _runs(spec, boundary=i, count=col_count, horizontal=True):
                page.draw_line((spec.cols[start], y), (spec.cols[end], y), width=width)
        for j, x in enumerate(spec.cols):
            for start, end in _runs(spec, boundary=j, count=row_count, horizontal=False):
                page.draw_line((x, spec.rows[start]), (x, spec.rows[end]), width=spec.line_width)
    for (row, column), text in spec.cells.items():
        page.insert_text((spec.cols[column] + 6, spec.rows[row] + spec.text_dy), text, fontsize=11, fontname=fontname)


# 本方案只加 `table_page: bool | TableSpec`；三份方案合并后的统一终态签名（§10）还有
# diagram 方案的 `diagram_page` / `diagram_caption` 与 formula 方案的 `formula_page` /
# `formula_rule`，三种版面都画在最后一页、互斥（`assert sum(bool(v) for v in (...)) <= 1`）。
def authored_pdf(path: Path, *, page_count: int, label: str, embedded_font: bool = False,
                 table_page: bool | TableSpec = False) -> Path:
    spec = DEFAULT_TABLE if table_page is True else table_page
    with pdfspine.open() as document:
        for number in range(page_count):
            ...（:57-67 原样）
            if spec and number == page_count - 1:
                _draw_table(page, fontname, spec)
        path.write_bytes(document.tobytes())
    return path
```

`DEFAULT_TABLE` 下 `_runs` 每条边界只有一段 `(0, count)`，画线顺序与 `:37-40` 完全一致（先横后竖，同坐标同宽）→ 现有 `table_page=True` 的 PDF 字节不变，`generic_publication_helpers.py:34-35` 的 `TABLE_BBOX/TABLE_REGION` 常量照旧。`insert_text` 的 y 偏移经 `TableSpec.text_dy`（默认 `18.0` = `:43` 现值）保持 `DEFAULT_TABLE` 字节不变；`MULTI_HEADER_TABLE` 用 `16.0`（22pt 行高）。

`generic_publication_helpers.py`：`text_partition_sender(calls, *, table_caption=False, table_bbox=TABLE_BBOX)`（`:68-70`），`:86-93` 用 `table_bbox`；`TABLE_REGION` 改为函数 `table_region(bbox)`；`ingest_generic_semantics` / `publish_generic_document`（`:131-171`、`:174-212`）的 `table_page: bool | TableSpec` 透传并把 `spec.bbox` 交给 sender。diagram / formula 两份方案在**同一个** `text_partition_sender` 上追加 `diagram_page` / `diagram_caption` / `formula_page` 关键字与各自的 prompt 哨兵分支（`"Return diagram-observations-v1"` / `"Return formula-observations-v1"` / `"Return visual-description-v1"`），三份方案都不新建独立 sender；`max_live_calls` 的口径统一为 `page_count + 2 × 视觉对象数`（表格页无视觉对象，仍是 `page_count`，§10）。

### 6.2 单元测试（`processing/`，纯规则）

`tests/enterprise_pdf_rag/processing/test_geometry.py`（现有 2 例不动）新增：
- `test_covering_segments_stitches_collinear_pieces_and_rejects_gaps` —— 两段 `[0,50]`、`[50.3,100]` 覆盖 `[0,100]`；`[0,50]`、`[51,100]` 不覆盖；端点余量 0.5 内通过、0.6 拒。
- `test_segments_crossing_ignores_touching_ends` —— 恰好在开区间端点上的线段不算穿越。
- `test_ruling_digest_is_order_sensitive_and_stable`。

`tests/enterprise_pdf_rag/processing/test_table_grid.py`：`:232-266` 拆为 §2.2 两条；其余 10 例不动。

`tests/enterprise_pdf_rag/processing/test_table_grid_proof.py`（新，手工 `Segment` 元组 + 手工 pending `TableIR`，每条规则一正一反）：

| 用例 | 断言 |
|---|---|
| `test_fully_ruled_2x2_is_proved_with_four_borders_per_cell` | `GridProof`；每 cell 四边各 1 个 `SegmentRef`；`verified_table` 后 `verification is VERIFIED`、cell_id 不变、`strip_grid_evidence` 还原相等 |
| `test_boundary_without_ruling_is_rejected` | 删掉 y=rows[1] 的线 → `GridRejection("row boundary 1 …")` |
| `test_snapped_boundary_is_rejected` | rows 给 31.0，线在 30.0 与 32.0 → 拒（0.5pt 匹配不到吸附值） |
| `test_edge_not_continuously_ruled_is_rejected` | 顶边线段 `[20,100]`+`[101,220]`（缝 1pt）→ `"top edge is not continuously ruled"` |
| `test_edge_covered_by_stitched_pieces_passes` | 缝 0.3pt → 通过，`border.top` 含 2 个 ref |
| `test_row_col_index_misaligned_with_boundaries_is_rejected` | cell.row=1 但 bbox 在 rows[0..1] → `"not aligned"` |
| `test_merged_cell_requires_missing_interior_ruling` | 合并格内部边界无线 → 通过且 `merge_proof.interior_rows == (1,)`；加一条穿过的短线 → 拒；只有 ≤0.5pt 的触碰 → 通过 |
| `test_unknown_slot_is_rejected` | slots 含 UNKNOWN → 拒 |
| `test_thick_rule_header_is_proved_but_first_row_is_heuristic` | rows[1] 线 2.0pt、其余 1.0 → `RULING_THICK/PROVED rows=(0,)` 且 `FIRST_ROW_RULE/HEURISTIC` 同时存在；全 1.0 → 只有 heuristic；`proved_header_rows()` 只含前者 |
| `test_fill_header_band_is_proved` | fills 含 `(cols[0], rows[0], cols[-1], rows[1])` → `FILL/PROVED`；覆盖所有行 → 不算 |
| `test_bottom_left_coordinates_do_not_prove` | 把线段 y 全部翻转为 `page_height - y` 喂进去 → 拒（bottom-left 误用回归） |
| `test_check_grid_evidence_reproves_and_detects_tampering` | VERIFIED 表 + 同一线段集 → 通过；改 `rows`、删一条线、改 `ruling_digest` → 各 `ValueError` |

### 6.3 adapter 测试：`tests/enterprise_pdf_rag/adapters/test_pdfspine_tables.py`

- `:61-102` `test_native_typed_slots_preserve_merge_and_exact_source_occurrences`：`_table_pdf()`（6 条整线、首行合并）现在**全划线** → 断言改为 `table.verification is Verification.VERIFIED`，追加 `table.cells[0].border.merge_proof == MergeProof((), (1,))`、`table.grid_evidence.rows == (20.0, 70.0, 120.0)`、`cols == (20.0, 120.0, 220.0)`、`"Grid structure proved" in diagnostics[-1]`。
- 新增 `test_ruling_segments_use_top_left_like_spans`：`_table_pdf()` 的 `ruling_segments(page)` 6 条，`positions == {20,70,120}∪{20,120,220}`，与 `page.get_text("dict")` 的 span bbox 同系（span "Header" 的 y 落在 20..70 内）。
- 新增 `test_ruling_segments_accept_thin_filled_rectangles_and_stroked_rects`：E 例（细填充矩形）→ 3 横 3 竖 `edge="re-thin"`；D 例（逐格 `draw_rect`）→ 每格 4 条 `re-*`，`find_tables` 结果 VERIFIED。
- 新增 `test_snapped_or_doubled_boundaries_stay_pending`：F 例 → `verification is PENDING`，diagnostic 含 `"row boundary 0 at y=31.0"`；双线边框例 → PENDING，含 `y=30.666`。
- 新增 `test_frame_only_and_unruled_tables_report_no_grid`：`FRAME_ONLY_TABLE` / `UNRULED_TABLE` → `result.table is None`，diagnostics 为 `found 0 page table(s)`（检测阶段就没有表，证明阶段不参与）。
- 新增 `test_fill_header_band_is_proved_from_filled_rectangles`：`FILL_HEADER_TABLE` → VERIFIED；`headers` 含 `FILL/PROVED rows=(0,)` 且 `fills == ((20.0, 60.0, 220.0, 86.0),)`。
- 新增 `test_multi_header_table_proves_merges_and_thick_header`：`MULTI_HEADER_TABLE` → VERIFIED；`proved_header_rows() == {0, 1}`；`(2,2)` cell `row_span==2` 且 `merge_proof.interior_rows==(3,)`；`(0,0)` `col_span==2`、`interior_cols==(1,)`。
- 新增 `test_split_segments_are_stitched`：`SPLIT_TABLE` → VERIFIED，某 cell `border.top` 长度 == 1（每格只跨一段）而 `grid_evidence.segment_count == 4*2 + 3*3 == 17`（4 条横边界各 2 段、3 条竖边界各 3 段）。
- `:130-176` 真实 p20 用例不动（仍 `result.table is None`）。

### 6.4 e2e 与回答链

`tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py`
- `:221` `assert context.ir.verification is Verification.PENDING` → **VERIFIED**（`authored_pdf` 3×2 全划线）；`:228` 后加 `assert block.grid_verification is Verification.VERIFIED` 与 `f"cells.{value.cell_id} (1,1): 1,234 row=1 col=1 header=<NONE>" in block.prompt_text()`（全 1pt 线 → 无 proved 表头）；`:212` "cells." / "row" 不进 embedder 的断言保留。
- 新增 `test_generic_pdf_multi_header_table_publishes_with_proved_headers`：`table_page=MULTI_HEADER_TABLE` → publish → resolve → `ir.grid_evidence.proved_header_rows() == {0,1}`；block 里 cell (2,1) 行含 `header="Group" | "Value"`；`qualify_draft().kinds == {"Table": 1, "Text": 3}`。
- `:235-260` caption 反例不动（转写失败路径，网格照样 VERIFIED 但 description/qualification UNAVAILABLE；追加断言 `stages["ir"]` 的 TableIR `verification is VERIFIED` 以证明"两分支独立"）。

`tests/enterprise_pdf_rag/processing/test_context_builder.py`
- `:246-259` 手工 PENDING 表：追加 `"grid=pending" in rendered` 且行尾**没有** `row=`。
- `:305-337` 发布表：追加 `block.grid_verification is VERIFIED`、`cells` 每个 `verification is VERIFIED`、渲染含 `row=1 col=1 header=<NONE>`。

`tests/enterprise_pdf_rag/answers/test_verify.py`
- `_table_block()`（`:85-115`）保持 PENDING（`grid_verification` 默认）；`:224-238` 现有断言不动。
- 新增 `_verified_table_block()`：`grid_verification=VERIFIED`，cell `c-1` `verification=VERIFIED, headers=(HeaderRef("h-1","Value","row"),)`。
- 新增 `test_cell_claim_with_row_col_header_verifies_against_the_grid`：`_claim(..., row=0, col=0, header="Value")` → verified，citation `row==0, col==0, header=="Value", header_cell_id=="h-1"`，`evidence_ids == ("c-1","t-1","h-1")`。
- 新增 `test_cell_claim_relations_are_rejected_when_wrong_or_unverified`：行错 / 列错 / `header="value"`（大小写）/ `header="Metric"`（非该 cell 表头）→ `CLAIM_NOT_IN_EVIDENCE`；对 PENDING 块带 `row=0` → `CLAIM_NOT_IN_EVIDENCE` "not verified"；QUOTE claim 带 `row=0` → `MODEL_OUTPUT_INVALID`。
- `_claim()` helper（`:122-130`）加 `row/col/header` 关键字透传。

`tests/enterprise_pdf_rag/answers/test_answer_service.py`（e2e 骨架）

```python
_CELL_LINE = re.compile(r"^cells\.(\S+) \((\d+),(\d+)\): 1,234 row=(\d+) col=(\d+) header=(.*)$", re.MULTILINE)


def _cell_relation_script(header: str) -> Script:
    def script(prompt: str) -> ModelAnswer:
        match = _CELL_LINE.search(prompt)
        assert match is not None
        (table,) = _members(prompt, "table")
        return answered(
            "Revenue under Value is 1,234.",
            ModelClaim(claim_id="c1", member_id=table, kind="cell", field_path=f"cells.{match[1]}",
                       text="1,234", row=int(match[4]), col=int(match[5]), header=header),
        )
    return script


def test_table_cell_claim_with_proved_header_answers_and_cites_the_header(tmp_path, monkeypatch) -> None:
    published = publish_generic_document(tmp_path, monkeypatch, filename="meridian-semiannual.pdf",
                                         label=DOCUMENT_LABEL, page_count=3,
                                         embedder=OfflineDescriptionEmbedder(), table_page=MULTI_HEADER_TABLE)
    document = StoreMountedDocument(LocalDocumentStore(Path(published.source_store), activate_on_publish=False),
                                    ProcessingStore(Path(published.processing_store)),
                                    processing_id=published.published_processing_id,
                                    embedder=OfflineDescriptionEmbedder())
    service, prompts = _service(tmp_path, document, _cell_relation_script("Value"))
    result = service.answer(AnswerRequest("What is the value in the second row, first column under Value?"))
    assert result.status is AnswerStatus.ANSWERED
    (claim,) = result.claims
    citation = claim.citations[0]
    assert (citation.row, citation.col, citation.header) == (2, 1, "Value")
    assert citation.header_cell_id in citation.evidence_ids
    assert 'header="Group" | "Value"' in prompts[0]


def test_table_cell_claim_with_unproved_header_text_is_refused(tmp_path, monkeypatch) -> None:
    ...同上，_cell_relation_script("value")（非逐字）→ result.status is AnswerStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert result.rejected[0].detail.startswith("claimed header")
```

（`StoreMountedDocument` 构造参数见 `tests/.../answers/store_mounted_document.py:35-60`；`_service` 见 `test_answer_service.py:83-95`；`_members` 见 `:59-60`。）

### 6.5 真实样本只读 smoke（`SAMPLE`/`data/ingestion` 不存在则 `pytest.skip`，同 `:131-134` 模式）

在 `test_pdfspine_tables.py` 追加：

```python
INGESTION_SYNTHETIC = Path("data/ingestion/3f7233e3a7e40ad75f9579740b89bf7d88087528f3f9760fcd7b576d24c71813/source/source.pdf")
INGESTION_TABLE_REGION = (19.5, 119.8, 300.5, 224.3)   # 该快照 layout.json 里 LLM 给的表格区域


def test_synthetic_ingestion_table_reproves_verified() -> None:
    if not INGESTION_SYNTHETIC.is_file():
        pytest.skip("Local synthetic ingestion store is absent; nothing is downloaded.")
    pdf = INGESTION_SYNTHETIC.read_bytes()
    page = _page_input(pdf, page_index=2)
    spans = tuple(s.span_id for s in page.text.spans if _center_in(INGESTION_TABLE_REGION, s.bbox))
    item = LayoutObject("ingestion-table", ObjectKind.TABLE, INGESTION_TABLE_REGION, spans, "Ruled metrics table", Confidence(None, "layout inference pending"))
    result = PdfspineTableAdapter().extract(pdf, page=page, item=item)
    assert result.table is not None and result.table.verification is Verification.VERIFIED
    assert result.table.grid_evidence.rows == (120.0, 146.0, 172.0, 198.0, 224.0)
    assert result.table.grid_evidence.cols == (20.0, 150.0, 300.0)
    assert result.table.grid_evidence.segment_count == 8
    # 旧 ir.json（无 grid_evidence）仍可解析且保持 PENDING
    stored = TypeAdapter(TableIR).validate_json(Path(".../object-ba0576f0dd0a758eb2b1/ir.json").read_bytes())
    assert stored.verification is Verification.PENDING and stored.grid_evidence is None
    assert {c.cell_id for c in stored.cells} == {c.cell_id for c in result.table.cells}   # cell_id 稳定


def test_real_p20_sensitivity_region_reports_native_grid_unavailable_with_ruling_diagnosis() -> None:
    ...（:130-171 原样）...
    assert result.table is None and result.diagnostics == (...)   # :173-176 不变
    # 诊断（只读）：区域内有多少轴对齐线段、pdfspine 找到的那张表 bbox 与区域的关系
    document = pdfspine.open(stream=pdf, filetype="pdf")
    try:
        source_page = document.load_page(19)
        inside = [s for s in ruling_segments(source_page) if _segment_in(P20_SENSITIVITY_CANDIDATE, s)]
        (found,) = source_page.find_tables(strategy="lines").tables
        print(f"p20: rulings_in_region={len(inside)} found_table_bbox={tuple(found.bbox)}")
    finally:
        document.close()
```

期望：合成表 → VERIFIED；AIA p20 → `result.table is None`（检测阶段失败，与现状一致），打印诊断供 handoff 记录（`-s` 运行）。此外用 `.venv/bin/python -m enterprise_pdf_rag.cli qualify/index/publish` 对 `data/ingestion/3f72…` 重建一次到 tmp 输出目录（不覆盖 `data/`），核对新快照里 `ir.json` 的 `verification == "verified"` 且旧快照 `load()` 仍成功。

---

## 7. 分阶段实施清单与验证命令

| 阶段 | 内容 | 验证命令 | 人日 |
|---|---|---|---|
| **P1 纯规则**（TDD：先写 §6.2 红测） | `geometry.py` 扩展；`table_models.py` 证据类型 + 解除硬钉；`table_grid_proof.py`；`test_table_grid.py:232` 拆分 | `.venv/bin/python -m pytest tests/enterprise_pdf_rag/processing/test_geometry.py tests/enterprise_pdf_rag/processing/test_table_grid.py tests/enterprise_pdf_rag/processing/test_table_grid_proof.py tests/enterprise_pdf_rag/processing/test_table_transcription.py -q` → 全绿；`.venv/bin/python scripts/enterprise_pdf_rag/check_architecture.py` → 无 `forbidden domain import` | 2.0 |
| **P2 producer + 夹具** | `pdfspine_tables.py` `ruling_segments` / `fill_rectangles` / `_map_table` 装配；`test_pdf_ingestion.py` `TableSpec` 与四个夹具；`generic_publication_helpers.py` 透传；§6.3 用例 | `.venv/bin/python -m pytest tests/enterprise_pdf_rag/adapters/test_pdfspine_tables.py tests/enterprise_pdf_rag/adapters/test_pdf_ingestion.py -q`；确认 `DEFAULT_TABLE` PDF 字节不变：`python -c "..."` 对比改前改后 `authored_pdf(table_page=True)` 的 sha256 | 1.5 |
| **P3 validator + stage 回执** | `typed_ir.LiteralQualification` 两字段；`semantic_objects._table`；`literal_qualification.py` 重证；e2e `:221` 改断言 + 多级表头 e2e | `.venv/bin/python -m pytest tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py tests/enterprise_pdf_rag/adapters/test_draft_publication.py tests/enterprise_pdf_rag/processing/test_context_builder.py tests/enterprise_pdf_rag/adapters/test_document_catalog.py -q`；`.venv/bin/python scripts/enterprise_pdf_rag/check_conformance.py` | 1.0 |
| **P4 消费侧** | `context_builder.py`、`prompt.py`、`answers/models.py`、`verify.py`、`http/chat_schemas.py`；§6.4 answers 用例 | `.venv/bin/python -m pytest tests/enterprise_pdf_rag/answers tests/enterprise_pdf_rag/processing/test_context_builder.py tests/enterprise_pdf_rag/adapters/test_chat_http.py -q` | 1.5 |
| **P5 文档 + 真实样本 + 全门** | ADR 0014、ADR 0011 旁注、handoff / testing 文档、frontmatter；§6.5 smoke；policy v5 与 diagram/formula 方案对表 | `.venv/bin/python -m pytest tests/enterprise_pdf_rag -q -s -k "ingestion_table or p20"`；`.venv/bin/python -m pytest tests/ -q`（期望 1295+新增 passed，40 skipped 不变或 +2 skip）；`make lint`；`make drift`；`scripts/ci.sh` | 1.0 |
| 缓冲 | `chat_schemas` 映射、与另两份方案 rebase、真实样本诊断记录 | — | 1.0 |

**合计 ≈ 8 人日**（含 1 人日缓冲）。P1–P2 可与 diagram/formula 方案并行（无共享文件）；P3–P5 涉及 `context_builder.py` / `verify.py` / `prompt.py` / `answers/models.py` / `processing_retrieval.py` 常量区 / ADR 编号，需要与另两份方案排序合并（§8 末尾）。

---

## 8. 需要拍板的点（≤3，附推荐）

1. **表头证据等级是否影响 `VERIFIED`？** 推荐：**不影响**。VERIFIED 只陈述"行/列/合并结构来自真实线"；表头是另一个事实，用 `HeaderEvidence.strength` 单独分级——`proved`（`ruling_thick` / `fill`）才可被 `header=` 引用，`heuristic`（`font_bold` / `first_row_rule`）只进审阅产物。否则全 1pt 线的普通表（`authored_pdf` 默认表、`data/ingestion` 合成表）永远拿不到 VERIFIED，`row/col` 引用也跟着没了。
2. **部分划线表（外框有内线无、或部分内线缺失但 pdfspine 仍给出网格）是否 PENDING？** 推荐：**PENDING**。实测外框-only 在检测阶段就是 0 张表；剩下的"部分内线"情形（某条边界只在部分列有线、其余被吸附/推断出来）正是规则 3（边连续覆盖）与规则 4（合并处无内线）要拒的对象——放行就等于承认 pdfspine 的推断为证据，违反 fail closed。
3. **`row/col/header` 引用是否只对 VERIFIED 表开放？** 推荐：**是**；纯文本 `cells.<id>` 引用对 PENDING 表保持现状。`eligibility()` 不改、policy 不改；开放范围由 `ContextBlock.grid_verification`（来自 `ir.verification`）在 prompt 渲染与 `_verify_cell` 两处同时把关。

**与另两份方案的共享/冲突文件**（合并顺序，§10 已统一：**本方案的核心文件全部独占，可先行或与另两份并行**；另两份之间必须 diagram 先于 formula——formula 复用 diagram 打开的 `BlockKind`/`ClaimKind`/`verify` 分发/policy v5 座位。消费侧共享文件三份都只是**追加**带默认值的字段或 `elif` 分支，任何顺序都不产生语义冲突，只有行号漂移；常量区只由 diagram 一份编辑）：`processing/context_builder.py`（`ContextBlock` 字段与 `prompt_text` 分支）、`answers/verify.py`（`_PATH_PREFIX/_BLOCK_KINDS` vs 本方案只改 `_verify_cell`）、`answers/prompt.py`（`ModelClaim` / `SYSTEM_RULES`）、`answers/models.py`（`ClaimCitation` 追加字段）、`adapters/processing_retrieval.py:56-74`（policy 常量，本方案不编辑）、`adapters/http/chat_schemas.py` + `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json`（三份都要改，重生成只跑一次）、`tests/.../test_pdf_ingestion.py::authored_pdf` 与 `generic_publication_helpers.py::text_partition_sender`（统一签名见 §6.1）、`docs/enterprise-pdf-rag/adr/`（0014 = 本方案，0015 = diagram+formula 合写）、`CLAUDE_HANDOFF.md` / `testing-and-ingestion.md`。`adapters/draft_publication.py:25` **三份都不改**（§3.4）。本方案独占：`processing/table_models.py`、`processing/table_grid_proof.py`、`processing/geometry.py`、`adapters/pdfspine_tables.py`、`adapters/literal_qualification.py`（TABLE 分支）、`processing/typed_ir.py::LiteralQualification`、`tests/.../test_pdf_ingestion.py` 夹具、`test_pdfspine_tables.py`、`test_table_grid*.py`。

---

## 9. 非目标与风险

### 9.1 非目标
- 不改表格**检测**（仍 `strategy="lines"`，不引入 `text` / `vision`）；不做 strategy 白名单校验（拼错静默退化的坑只加注释）。
- 不改 span→cell 归属（仍 bbox 中心）、不改 `cell_id` 输入、不改 `check_table_transcription`。
- 不证明"表头语义正确"（`Value` 列的数是"值"），只证明"该 cell 位于证据为 proved 的表头行/列之下"。
- 不处理跨页表、旋转页（`extract:60-66` 已拒）、注释层线条。
- 不为 `data/ingestion` / AIA 现有快照做迁移；重建由用户按 `index → publish` 自行触发。

### 9.2 风险与处理

| 风险 | 表现 | 处理 |
|---|---|---|
| 细矩形当线 | 真实 PDF 常用 `re`+fill 画表格线；`ruling_segments` 取中线、厚度 = 短边；若同一条线由多个细矩形拼成，走覆盖拼接 | 已支持（E 例）；厚度 > 3.0 的"色带"不当线，与 pdfspine 同口径 |
| 双线边框 | 两条平行线相距 1–3pt → pdfspine 平均成一个边界，任一线都不在 0.5pt 内 → **PENDING** | 预期 fail closed；诊断串带 y 值；未来若要支持需 ADR 明确"双线取内侧线"规则 |
| 虚线 `dashes` | `ruling_segments` 跳过 → 该边界无线 → PENDING | 预期；夹具画不出虚线（`draw_line` 无 `dashes`），只能靠真实样本或手写 content stream 回归 |
| 线被裁剪（clip 路径） | `get_drawings()` 给的是路径几何，不含裁剪；被裁掉的线仍会被当作存在 | 与 pdfspine 检测同源（它也不看 clip）；不引入 ReplayDevice 的裁剪栈（那是 bottom-left 系，第二套翻转），列为已知放宽，写进 ADR 0014 |
| 旋转页面 / 非标准 page rect | `extract:60-66` 已拒绝 | 不变 |
| 表格跨页 | 每页一张独立 TableIR；跨页的"续表"不合并 | 非目标 |
| `snap_tolerance=3.0` 吸附 vs 0.5pt 匹配 | 吸附出来的边界（F 例 31.0、双线 30.67）过不了规则 1 → PENDING；顺序是**先让 pdfspine 用默认 3.0 吸附出网格（不改检测、cell_id 不变），再对吸附结果做 0.5pt 证明** | 不要反过来把 `snap_tolerance` 调到 0.5：F 例实测会多出一行、改变 cell_id，也改变检测结果 |
| `find_tables` 未知 strategy 静默退化 | 只影响检测；证明阶段与之无关 | 注释提醒 |
| 填充带盖住文字（z-order） | `filled_rectangles()` 不含绘制顺序，灰底画在文字之后会遮住表头字，本方案仍会记 `fill/proved` | 文字可见性属 ADR 0009 `source_paint` 的范畴；ADR 0014 写明 `fill` 证据不断言可见性，表头文本仍由 span 逐字来 |
| 回答路径重证成本 | `validate_literal_member` 对 VERIFIED 表每次 resolve 都 `pdfspine.open` + `get_drawings()` | 表格成员少（当前样本 ≤1/文档）；若成为瓶颈，把 `ruling_digest` 比对留在 resolve、把完整重证留在 `build`（两处调用点分开传 `reprove: bool`） |
| `TableIR` 相等比较依赖 pdfspine 浮点输出稳定 | 升级 pdfspine 后 `check_grid_evidence` 的 `verified_table(pending, proof) != table` 可能因浮点尾巴不同而失败 → 该成员 `_qualified` 抛错 → `build` 失败 | 与 ADR 0011 转写重证的既有行为一致（pin 版本）；`GridEvidence.producer` 串留了升级口 |
| `SYSTEM_RULES` 变更 | `request_fingerprint` 变 → 旧回答缓存全部 miss | 预期；handoff 记录 |
| 与另两份方案的合并冲突 | §8 末尾列表 | 常量区单点编辑；`ContextBlock` 新字段都带默认值，追加而非重排 |

---

## 10. 交叉修订记录

三份方案（`diagram-retrievable.md` / `formula-retrievable.md` / `table-grid-verification.md`）写完后做了一次交叉一致性核对，本节逐条记录**本文件**被改了什么、为什么。总览与合并顺序见同目录 `README.md`。

### 10.1 已修订（逐条）

| # | 位置 | 改了什么 | 为什么 |
|---|---|---|---|
| 1 | §3.4 开头 | ①把"三份方案只能有一份实际编辑这些常量（推荐 diagram）"改成**已拍板**：`_POLICY` / `PROJECTED_CHART_POLICIES` / `CONTEXTUAL_POLICIES` / `VISUAL_PROJECTION_POLICIES` 由 diagram 方案唯一编辑；②`DraftQualification.qualification_policy` 从"要升 v3"改为**不升 v3**，并给出理由（那串描述的是"按 kind + stage 完整性判资格"这个方法而非 kind 集合；ADR 0013 加 metadata stage 时也没升；diagram / formula 两份方案都把 `draft_publication.py` 列在"不改"清单里） | 原文的"若升 v3……由承担常量编辑的那份方案改"是悬而未决项，而另两份方案明确写了不改 `draft_publication.py`，实施时会互相等待 |
| 2 | §3.4 第 1 步 | `_POLICY` 的注释文案 `(ADR 0014/0015/0016)` → `v5 embeds the qualified-IR projection of Diagram and Formula members (ADR 0015)`，并注明逐字以 diagram 方案 §4.2 为准、本方案的网格证据不进这条注释 | ADR 编号统一；且本方案的网格证据在 `ir` 资产里、不是 policy 语义，写进 policy 注释会误导 |
| 3 | §3.4 第 4 步 + 末尾 | 第 4 步改为写明新门控常量 `VISUAL_PROJECTION_POLICIES` 与两支投影门**全在 diagram 方案里**、本方案零读取侧 policy 分支；末尾把 `test_generic_publication_e2e.py:182` 的断言改成"不需要改" | 同 1 |
| 4 | §3.5 「ADR 编号协调」 | "推荐 0014 表格网格、0015 diagram、0016 formula，谁先合并谁用小号"→ **固定为 ADR 0014 = 本方案（含 ADR 0011 旁注）、ADR 0015 = Diagram + Formula 合写一份；不存在 ADR 0016；编号与合并先后无关** | 与公式方案 §5 原文"与图表方案合写一份 ADR 0014"直接矛盾；且"谁先合并谁用小号"会让三份方案的交叉引用在合并期间反复改名 |
| 5 | §5 改动清单 | ①`chat_schemas.py` 行细化为 `ClaimCitationOut` 逐字段映射（`:66-76`，与 ADR 0013 的 `page_title` 同处同法）；②新增一行 `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json`，说明三份方案都要改它、合并后只跑一次重生成脚本（脚本在公式方案 §7 阶段 3） | 原文用"若为显式映射……`git grep` 定位"留了悬念；另两份方案已核实 `check_schema.py` 全等比对且无 `--write`，必须手工重生成 |
| 6 | §6.1 `authored_pdf` | 在签名上方加注：本方案只加 `table_page: bool \| TableSpec`，三份合并后的统一终态签名还有 `diagram_page` / `diagram_caption` / `formula_page` / `formula_rule`，三种版面互斥（`assert sum(bool(v) for v in (...)) <= 1`） | 三份方案各给了一种 `authored_pdf` 签名（本方案把 `table_page` 从 `bool` 扩成 `bool \| TableSpec`），需要一个终态 |
| 7 | §6.1 `generic_publication_helpers.py` 段 | 补一句：diagram / formula 的哨兵分支都追加在**同一个** `text_partition_sender` 上，三份都不新建独立 sender；`max_live_calls` 口径统一为 `page_count + 2 × 视觉对象数`（表格页无视觉对象，仍是 `page_count`） | diagram 方案原本要新建 `diagram_sender`，而 `ingest_generic_semantics` 只 monkeypatch 一个 `_send_once`，多个 sender 无法共存 |
| 8 | §8 末尾「与另两份方案的共享/冲突文件」 | 合并顺序建议从"diagram → 本方案 → formula"改为：**本方案核心文件全部独占，可先行或与另两份并行；diagram 必须先于 formula**；并补齐共享文件清单（`chat_schemas.py` + `rag-chat-v1.json`、两个测试 helper、ADR 编号），注明 `draft_publication.py:25` 三份都不改 | 原顺序把本方案夹在中间，与"表格方案与另两份无共享核心文件、可先行"的事实不符；formula 复用 diagram 打开的 `BlockKind` / `ClaimKind` / `verify` 分发 / policy v5 座位，两者顺序不可交换 |

### 10.2 核对过、确认无冲突（未改动）

- **`processing/` 的 import 约束**：用 `git show c15525b:scripts/enterprise_pdf_rag/check_architecture.py` 复核，本方案 §0.2 / §1.1 引的规则准确（`processing/` 只允许标准库 + 四个纯域包，pydantic 只开给 `answers/`，另禁 `os/pathlib/io/...` 与 `open/eval/exec/__import__`）。**本方案放进 `processing/` 的新内容（`geometry.py` 的 `Segment` 等、`table_models.py` 的证据类型、`table_grid_proof.py`）全部是 dataclass + `dataclasses` / `enum` / `hashlib` / `math` / `collections.abc`，合规，无需改写**；diagram / formula 两份方案放进 `processing/` 的新模块同样没有 pydantic。
- **本方案不碰 policy**：`eligibility` 不改、`member_index_text` 不改、无读取侧 policy 分支 —— 与另两份方案在 `processing_retrieval.py` 的改动零重叠（仅 `semantic_objects.py` 与 `literal_qualification.py` 的 TABLE 分支是本方案独占）。
- **枚举**：本方案不给 `ClaimKind` / `BlockKind` 加值，不与另两份的新值重名。
- **`ContextBlock` / `CellEvidence` / `ClaimCitation` / `ModelClaim`**：本方案全部是"追加带默认值的字段"，与 diagram 的 `nodes`/`edges`、formula 的 `formula_*` 互不重名。
- **`prompt_text()`**：本方案改的是既有 `BlockKind.TABLE` 分支体，另两份是新增 `elif`，互不覆盖；现有 `"cells.c-1 (0,0): Revenue" in rendered` 等子串断言仍成立。
- **`_verify_cell`**：本方案只改这一个函数体；`_PATH_PREFIX` / `_BLOCK_KINDS` / `verify_claims` 的分发由另两份改，无重叠（唯一交叉是"非 CELL claim 带 row/col/header → `MODEL_OUTPUT_INVALID`"那两行落在 `:320` 之前，与另两份新增的 `elif` 分支不冲突）。

### 10.3 发现但**未**修改的遗留问题

1. **`verify.py` 里三种文本比对口径并存**：本方案的 `header` 比对用裸 `==`（逐字，不 `_norm`），`cell.text` 仍用 `_norm`（含 casefold）；公式方案新增 `_exact`（不 casefold）；diagram 方案沿用 `_norm`。同一个文件三种口径，各有理由，建议在 ADR 0014 / 0015 各写明，本次不统一。
2. **回答路径重证成本**：本方案让 `validate_literal_member` 对 VERIFIED 表在每次 `resolve` 都 `pdfspine.open` + `get_drawings()`（§9.2 已登记缓解方案）。另两份方案的 replay 也各自重开 PDF / 重算 crop，三者叠加后单次 resolve 的 I/O 成本未做整体评估，未改。
3. **`data/ingestion` 两个 v2 快照的迁移**：本方案 §9.1 明确不做迁移；但三份方案合并后 policy 升到 v5、`ir.json` 字节也变，旧快照要吃到新能力必须重跑 `semantics → metadata → index → publish`。谁在什么时候触发这次重建没有归属，未定（README §5 已登记为未决项）。
