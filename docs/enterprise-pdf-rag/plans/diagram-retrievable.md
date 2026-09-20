> 实施分支 `feat/visual-objects`（2026-09-21 落地）。本文件是**方案原文**，只在顶部加了这行注记；实现与方案的偏离、最终口径与遗留以 [ADR 0015](../adr/0015-diagram-and-formula-retrievable.md) 与 [交接文档](../CLAUDE_HANDOFF.md) 为准。

# 方案：让 `ObjectKind.DIAGRAM` 对象可检索（same-SVG two branches + 无模型几何资格校验）

仓库 `/Users/linhan/startup/spine/ragspine`，基线 `main` = **`c15525b`**（`feat/page-metadata` 已 fast-forward 合并；ADR 0013 / policy v4 / `contextual_index_text` / `AnswerRequest.filters` 均在）。只读设计，不改仓库。
下文 `path:行号` 均以 `git show c15525b:<path> | cat -n` 为准；相对路径默认前缀 `src/enterprise_pdf_rag/`。
本文中的 pdfspine 行为与真实样本几何均由本次调研脚本实测（§1.3、§1.4），不是推测。

---

## 0. 目标与不变量

**目标（可验证）**：一个 Diagram 对象在 `ingest --stage semantics` 后，若其模型产出的 `DiagramIR`（节点 / 边）能被**无模型、可重放**的几何与逐字校验整体证明，则：
1. 产出 `qualified_ir` / `qualified_description` / `qualification` 三个新 stage（与图表命名一致），`eligibility()` 放行；
2. 索引文本是其 IR 的确定性投影（policy v5），两条检索通道打同一串；
3. 回答链能把它渲染成 context block，模型可引用 `nodes.<id>.label` 与 `edges.<index>`，`verify.py` 逐字回读；
4. 任一规则失败 → 整对象 `qualification` 停在 `UNAVAILABLE` 并带逐字诊断，行为与今天完全一致（不进索引）。

**不变量逐条对照**（简报 §不变量）：

| 不变量 | 本方案落点 |
|---|---|
| 逐字证据 | 每个 node label 必须 == 其引用 span 文本（whitespace-fold 后相等或按 id 序拼接相等），span 必须几何上落在 node bbox 内；边不含任何自由文本，值只是 `from_label -> to_label`（§3.2 N2/N3、§4.4） |
| fail closed | 任一 node/edge 规则失败 → `DiagramQualificationError` → 整对象 `qualification=UNAVAILABLE`，不做部分放行（§3.3）；replay 不等 → `ValueError` 拒绝挂载（§2.5） |
| 零人工 | 校验器输入只有 pinned SVG crop + TextSidecar + 两路模型产物；无人工凭据 |
| LLM 只在构建阶段 | 校验器、投影、描述模板、replay、verify 全部纯函数，回答链不调模型做校验 |
| same-SVG two branches | `visual_semantics.infer` 的两路（`ir` / `description`）不动；资格校验只读 `svg` stage（同一 crop）与 span；确定性描述（§3.4 方案 A）是**第三个纯函数产物**，不是模型分支，且原 `description` 资产原样保留并进 lineage |
| 快照不可变 | 不改任何已落盘 `description` / `ir` 资产；新增 `qualified_*` stage 是新内容寻址资产（图表先例 `semantic_objects.py:600-604`） |
| 旧快照可挂载 | policy v5 只在 `member_text` 读取侧分支（§4.2 四步模板）；`processing_store.load` 的 `stages.get("qualified_ir", stages.get("ir"))`（`processing_store.py:115-126`）天然兼容；无任何按 policy 拒收 |
| import 白名单 | 数据模型/模板/投影放 `processing/`；SVG 几何与 replay 放 `adapters/`。**`scripts/enterprise_pdf_rag/check_architecture.py:8-27` 规定 `processing/` 只允许标准库 + 四个纯域包（`EXTRA_ALLOWED` 只给 `answers/` 开 pydantic），因此 `processing/diagram_models.py`、`processing/diagram_description.py` 全部是 `dataclass(frozen=True, slots=True)`，不出现 pydantic；`DiagramPublicationReceipt`（pydantic）只在 `adapters/diagram_publication.py`。** `scripts/enterprise_pdf_rag/check_conformance.py`（`scripts/ci.sh:69` 调用）不需改 |

---

## 1. 现状事实（函数 / 行号级）

### 1.1 处理链与缺口

| 路径:行号 | 签名 / 要点 |
|---|---|
| `adapters/semantic_objects.py:139-141` | `SemanticObjectAdapter.process`：TEXT/LIST/GROUP → `ProcessingObjectAdapter` |
| `adapters/semantic_objects.py:176-183` | 非文字对象先落 `native_crop`、`source_text`（TextSidecar，只含 `item.source_span_ids`）；CHART → `_chart`，TABLE → `_table` |
| `adapters/semantic_objects.py:184-223` | IMAGE/DIAGRAM/FORMULA 唯一路径：`VisualSemanticAdapter(self.client).infer(...)`；落 `svg`(=`result.crop_svg`)、`model_render`、`model_view`、`ir_raw`/`ir`、`description_raw`/`description` |
| **`adapters/semantic_objects.py:217-222`** | **`qualification` 恒 `UNAVAILABLE`**，诊断逐字 `"Visual semantics are source-bound model inferences; an independent field/relationship verifier is not available for this object."` ← 本方案替换点 |
| `adapters/semantic_objects.py:47-98` | `_Writer.save(stage, payload, media_type)` / `_Writer.diagnostic(stage, reason, *, failed=False)`；stage 指纹含 `producer` 与 `item` |
| `adapters/semantic_objects.py:510-571` | 图表 `_qualify`：`qualified_ir` + `qualified_description` + `qualification`(pydantic 回执) + `qualification_exclusions` 的落盘模板 |
| `adapters/semantic_objects.py:224-304` | `_table`：**确定性资格不受 `qualification_policy` 门控**（`source_table_description` 总是运行）← Diagram 校验沿用此先例 |
| `adapters/visual_semantics.py:113-226` | `_prepare`：校验 span 归属、`crop_native_svg`、注入 `<metadata><observation id="obs-…">`、`render_svg_png`；`_PreparedVisual(svg: SvgArtifact, crop_svg, model_png, model_view_json, full_span_ids, excluded_partial_span_ids, unowned_full_span_ids, renderer_fingerprint)` |
| `adapters/visual_semantics.py:136-142` | IR 的 `SourceAnchor.bbox == item.bbox`（replay 时可从 IR 取回对象 bbox） |
| `adapters/visual_semantics.py:283-291` | `_normal(value)` = `" ".join(value.split())`；`_label_matches(label, elements)`：label 为 None ↔ 无证据；否则 `_normal(label)` ∈ {各 observation 文本, 它们的空格拼接} |
| `adapters/visual_semantics.py:402-448` | Diagram 映射：node 去重、`_label_matches`、`_inside(node.bbox, item.bbox)`；edge 必须指向已返回 node；`DiagramNode(node_id, "" if label is None else label, bbox, source_span_ids)`；全部 `Verification.PENDING` |
| `adapters/visual_semantic_schemas.py:31-45` | `DiagramNodeDTO.node_id: str(1..80)`（**无字符集限制**）、`label: str|None(≤300)`、`bbox`、`evidence`；`DiagramEdgeDTO(source_node_id, target_node_id, label, relationship(1..300), evidence)` |
| `processing/typed_ir.py:49-75` | `DiagramNode(node_id, label, bbox, source_span_ids)`；`DiagramEdge(source_node_id, target_node_id, label, relationship, verification=PENDING, source_span_ids=())`（**无 edge id**）；`DiagramIR(object_id, source, nodes, edges, diagnostics, verification=PENDING)` |
| `processing/typed_ir.py:101-109` | `ObjectDescription(object_id, source, source_span_ids, text, producer, confidence, verification)` —— Diagram 的 description 类型 |
| `adapters/object_processing.py:86-133` / `adapters/literal_qualification.py:28-152` | TEXT/LIST/GROUP/TABLE 的 `LiteralQualification` 写入与 replay；`literal_qualification.py:131-132` 对其它 kind `raise ValueError("Unsupported qualification for this object kind")` |
| `adapters/literal_qualification.py:99-107` | replay 时用 `crop_native_svg(...)` 逐字节重算 crop 与 `member.source_svg` 比对 —— Diagram replay 照抄 |
| `adapters/chart_publication.py:39-70` | `_ChartReceipt(BaseModel, strict/frozen/forbid)`：`object_id, source_manifest_id, region_id, ir, description, source_svg, raw_chart, raw_description, view, qualification`；`schema_version: Literal[...]` —— Diagram 回执模板 |
| `adapters/chart_publication.py:81-159` | `resolve_chart_member`：读回执 → 重建输入 → 重跑同一纯函数 → `!= expected` 则 raise；lineage 闭包 `{raw_chart, raw_description, view}` 精确相等（116-127） |
| `adapters/figure_label_qualification.py:69-156` | `qualify_source_labels(svg, chart, description) -> QualifiedLabelProjection`：无模型资格校验范本（`_fail(reason)` → `FigureError`，通过后 `replace(..., verification=VERIFIED)` + `FigureQualification` 回执） |
| `adapters/source_publication.py:79-83` | 挂载校验 per member：`CHART → validate_retrieval_chart_member`，**否则 `validate_literal_member`** ← 需加 DIAGRAM 分支 |

### 1.2 检索 / 回答链

| 路径:行号 | 签名 / 要点 |
|---|---|
| `adapters/processing_retrieval.py:60-74` | `_POLICY = "source-transcription-and-scoped-chart-qualification-v4"`；`PROJECTED_CHART_POLICIES = {v4, v3, "source-transcription-donut-and-displayed-bar-v2"}`；`CONTEXTUAL_POLICIES = {v4}` |
| `adapters/processing_retrieval.py:77-99` | `member_text(assets, plan, member, context=None)`：非 CHART 用 `ObjectDescription` 解析（Diagram 天然兼容）；CHART 在 `PROJECTED_CHART_POLICIES` 下走 `member_index_text`；最后 `CONTEXTUAL_POLICIES` 门 |
| **`adapters/processing_retrieval.py:101-130`** | `eligibility(record)`：kind 白名单（TEXT/LIST/GROUP/TABLE/CHART，107-113）+ 4 个 stage 全 SUCCEEDED（116-129）；拒绝串 `f"{record.kind.value} objects are not retrievable"`（114） |
| `adapters/processing_retrieval.py:144-256` | `ProcessingRetrieval.build`：158-162 必需 stage 元组（与 eligibility 重复一份）；173-203 CHART lineage；217-221 `contextual_index_text(member_index_text(checked_ir, checked_description.text), contexts.get(page_index))`；258-273 embedding 指纹含 `sha256(text)` |
| `adapters/processing_retrieval.py:347-356` | `_qualified(scope, member)`：CHART → `validate_retrieval_chart_member`，否则 `validate_literal_member` |
| `adapters/processing_retrieval.py:359-374` | `resolve_processing_context(...)`：同样二分派 |
| `processing/retrieval.py:143-155` | `RetrievalContext(snapshot_id, member, ir: TypedIR, description: ObjectDescription \| TextDescription, qualification: LiteralQualification \| FigureQualification)`；`.scope` 属性 |
| `processing/index_text.py:18-42` | `PageIndexContext` / `contextual_index_text(body, context)` |
| `processing/index_text.py:61-89` | `chart_index_text(chart, *, fallback)`；`member_index_text(ir, description_text)`：`isinstance(ir, ChartIR)` 才投影 |
| `processing/context_builder.py:21-35` | `BlockKind` 5 值；`_KIND_OF_OBJECT` 5 项 |
| `processing/context_builder.py:70-122` | `ContextBlock(... spans, list_items, cells, row_count, col_count, grammar, chart_fields)` + `prompt_text()`（可引用路径唯一渲染处：`fragments.<span>`:118、`items.<index>`:119-121、`cells.<id>`:116、`points.<pid>.value`:99-108） |
| `processing/context_builder.py:163-224` | `build_context_block`；224 `raise ValueError(f"{type(ir).__name__} members are not supported as answer context")` |
| `answers/models.py:18-22` | `ClaimKind`：QUOTE / CELL / CHART_VALUE |
| `answers/models.py:109-119` | `ClaimCitation(member_id, kind: BlockKind, page_index, field_path, evidence_ids, bbox, quote, chart_citation=None, page_title=None)` |
| `answers/prompt.py:17-23` | `ModelClaim.kind: Literal["quote", "cell", "chart_value"]` |
| `answers/prompt.py:41-56` | `SYSTEM_RULES` 第 1 条列出三种路径句式 |
| `answers/verify.py:61-72` | `_PATH_PREFIX` / `_BLOCK_KINDS` / `_POINT_VALUE_RE` |
| `answers/verify.py:107-134` | `_verify_quote`：`_norm(claim.text) in _norm(span.text)`，`ClaimCitation(block.member_id, block.kind, span.page_index, claim.field_path, (span.source_span_id,), span.bbox, span.text)` |
| `answers/verify.py:137-165` | `_verify_cell`：`_norm(claim.text) != _norm(cell.text)` → `CLAIM_NOT_IN_EVIDENCE` |
| `answers/verify.py:293-342` | `verify_claims` 总分发：320-326 kind/prefix 门；327-337 三路 |
| `adapters/answer_service.py:209-218` | `chart_evidence(member_id)` 闭包只按 `DISPLAYED_BAR_SCOPE` 分派；`verify_claims(model, by_member, chart_evidence=...)` |
| `adapters/http/chat_schemas.py:66-110` | `ClaimCitationOut.kind: BlockKind`、`ClaimOut.kind: ClaimKind` —— 枚举直接作为 pydantic 类型，新增成员自动可序列化 |
| `adapters/draft_publication.py:58-69` | `qualify_draft` 用 `eligibility()` 统计 `kinds` / `skipped_reasons` |
| `adapters/processing_export.py:193-210` | coverage 行：非 CHART 且有 qualification artifact → `source_transcription_qualified` |
| `tests/enterprise_pdf_rag/processing/test_context_builder.py:273-281` | 现有回归用 `DiagramIR("diagram-1", _ANCHOR, (), (), ())` 作"不支持"反例 → 必须换成 **`ImageIR`**（不可换成 `FormulaIR`：同一次 ADR 0015 升级里的公式方案会放行 `FormulaIR`，反例只能落在 `ImageIR` 上，见 §10） |

### 1.3 pdfspine 0.11.0 能力核实（本次实测，脚本 `scratchpad/probe_diagram.py` / `probe_shape.py`）

| 事实 | 实测结果 |
|---|---|
| 作图 API | `Page.draw_rect(rect, *, color, fill, width)`、`draw_line(p1, p2, *, color, width)`、`draw_polyline(points, *, color, width)`（**无 fill/closePath 形参**，传了被 `**_ignored` 吞掉，实测出来是开放 stroke）；填充闭合多边形须走 `Shape`：`page.new_shape()` → `shape.draw_polyline(pts)` → `shape.finish(color=, fill=, width=, closePath=True)` → `shape.commit()`（`document.pyi:353-385, 748-766, 801-808, 869`） |
| 内嵌字体页的 SVG | `get_svg_image(text_as_path=False)` 输出 **0 个 `<text>`**，字形为 `<path d="M0.05 0L…Z" fill="#000000" transform="matrix(12,0,0,12,20,120)"/>`（0..1 字体空间 + 缩放平移矩阵）→ `render_svg_png` 的 `_safe_svg`（`figure_reasoning.py:258-266` 拒绝 `<text>`）不会拒绝；这正是 `authored_pdf(embedded_font=True)` 已被 e2e 依赖的事实 |
| 坐标系 | 原生 SVG 首个子元素 `<g transform="matrix(1,0,0,-1,0,H)">` 包住全部内容；`<path d>` 内坐标是 PDF bottom-left。**沿树合成 transform 后即得 page-top-left**（与 span、IR bbox 同框）。实测：`draw_rect((20,70,90,100))`（top-left）→ SVG `M20 60L90 60L90 90L20 90Z`，H=160，合成后 (20,70)-(90,100) ✓。`crop_native_svg`（`pdfspine_svg.py:35-51`）只加外壳 `viewBox`，不改内层坐标 |
| 矩形 | `draw_rect` → `get_cdrawings()` `('re', (x0,y0,x1,y1))`，SVG `M…L…L…L…Z fill="none" stroke=…`；带 fill 时 SVG 产两条 path（先 fill 后 stroke），cdrawings `type='fs'` |
| 连线 | `draw_line` → `('l', p0, p1)`，SVG `M90 75L142 75 fill="none" stroke=…` |
| 箭头 | pdfspine **无 arrowhead 语义**；Shape 画的填充三角 → cdrawings `type='fs' closePath=True items=[('l',…)×3]`，SVG `M142 79L142 71L150 75L142 79Z fill="#000000" fill-rule="nonzero"` + 同形 stroke path。尖端要自己推 |
| span | `get_text("dict")` span bbox 为 top-left：`'PLAN' [28.0, 81.0, 52.0, 91.0]`，落在矩形 (20,70,90,100) 内 |
| 现有解析器 | `adapters/source_paint.py:288-359 _native_paths(svg)`：合成 transform、收集 `<path>`，但对 `svg/g/path` 之外的标签（如 `<image>`）与未列出的属性 **raise `unsupported_native_source_paint`**（AIA p5 含 2 个 `<image>` → 不能直接复用）；可复用的纯几何助手在 `adapters/donut_geometry.py`：`_matrix(str)->Matrix`(183)、`_compose(parent, child)`(234)、`_transform(matrix, point)`(178)、`_polygon(d)->tuple[Point,...]`(135，M/L/C/Z，C 用 `_curve` 扁平化到 0.025pt)、`_path_controls(d)`(199)、`_bounds(points)`(247)、`_intersects`(228)、`_unit_matrix`(194) |

### 1.4 真实样本（`data/output/aia-2026-interim/pages-001-020/runs/00d5c714…/`）

**p6 Diagram**（`page-006/objects/object-b7c9e77d6cf0ea933193/`，`object_id = layout-object-v1:c2145a…`，bbox `[670,281,915,383]`）：
- IR：3 node，label 逐字来自 3 个 span（`Foundation: 100% Digitalised Agency` / `Growth: Data-Driven Lead Generation` / `Intelligence: Sales & Management Copilots`），`edges: []`；span bbox 均在各自 node bbox 内（如 Foundation span `[701.93,289.51,897.60,301.82]` ⊂ node `[670,281,915,309]`）。
- SVG（`svg.svg`，802 `<path>`，0 `<text>`，0 `<image>`）：三个圆角矩形填充路径（`C` 段圆角 4.8pt），本次用 `_polygon` 扁平化 + 合成 transform 实测其 top-left 边界与 IR node bbox 的对照（脚本 `scratchpad/probe_p6.py`）：

| node | IR bbox | 路径边界（合成后） | 与对象 bbox 求交后 | 每边最大偏差 |
|---|---|---|---|---|
| node-foundation | `[670,281,915,309]` | `[670.01,280.96,929.21,309.76]` | `[670.01,281.0,915.0,309.76]` | 0.76 |
| node-growth | `[670,317,915,345]` | `[670.01,316.70,929.21,345.50]` | `[670.01,316.70,915.0,345.50]` | 0.50 |
| node-intelligence | `[670,353,915,383]` | `[670.01,352.44,929.21,381.24]` | `[670.01,352.44,915.0,381.24]` | **1.76** |

  面积/外包比 0.997（圆角），另有一个整页白底 `#ffffff` 路径 `[0,0,960,540]`（须按"背景"排除）。→ 节点框容差取 **2.0pt**、矩形判据取"扁平多边形面积 ≥ 0.85 × 外包面积"即可放行 p6 节点。
- **p6 无任何连线 / 箭头**，IR 诊断逐字 `"Three stacked rounded rectangular visual nodes are present; no connecting edges are visually shown."` → nodes-only 图（§8 拍板点 2）。

**p5 Diagram**（`page-005/objects/object-0bf4bbbb1af632182d06/`，人工 source-review 补入，`object_id = aia-p005-technology-flow-v1`）：
- `source_span_ids = []`；IR 5 node **label 全为 `""`、`source_span_ids` 全空**；2 条 edge（`node-aia-plus ↔ node-aia-one`，双向）；SVG 含 2 个 `<image>`（两个 tile 是位图，无矢量矩形）与两支一体成型的曲线箭头填充多边形（非"直线 + 三角"）。
- `model_view.json` 记录 6 个 `unowned_full_span_ids`（`Industry-`/`Leading`/`Technology`/`Customer `/`Super App`/`Agency `）。
- → 本方案校验器在规则 N2（空 label）处即拒绝，诊断串 `node node-industry-leading-technology: empty_label_without_source_occurrence`（§6.4 smoke 期望）。

**其它**：AIA 1–20 页共 2 个 Diagram、7 个 Image；Image 不在本方案范围。

---

## 2. 数据模型与模块

新增 5 个文件，改 11 个既有文件（§5）。布局：数据模型与模板/投影在 `processing/`，SVG 几何证明与 replay 在 `adapters/`。

### 2.1 `processing/diagram_models.py`（新，stdlib + 本包）

```python
"""Deterministic proof records for a diagram's nodes and edges; nothing here infers."""

from dataclasses import dataclass
from typing import Literal

from enterprise_pdf_rag.documents.models import Bounds
from enterprise_pdf_rag.figures.models import SourceAnchor

DIAGRAM_SCOPE = "diagram-structure-source-geometry-v1"
DIAGRAM_METHOD = (
    "exact-span-label + native-shape-bbox + connector-endpoint + filled-arrowhead-tip; "
    "no relationship semantics, no financial relations"
)
# 容差口径（pt，page-top-left）；全部是常量，不可注入
NODE_BBOX_TOLERANCE = 2.0      # node bbox 每边 vs 真实形状边界（∩ 对象 bbox）；p6 最大偏差 1.76
SPAN_INSIDE_TOLERANCE = 0.5    # 引用 span bbox 必须落在 node bbox 内（processing.geometry.contains 的 tolerance）
CONNECT_TOLERANCE = 2.0        # 连线端点 / 箭头尖端 落在 node bbox 外扩多少以内算"接触"
ARROW_JOIN_TOLERANCE = 3.0     # 箭头底边中点 与 连线端点 的最大距离
ARROWHEAD_MAX_AREA = 400.0     # 三角外包面积上限（pt²），排除大块填充
SHAPE_MIN_FILL_RATIO = 0.85    # 扁平多边形面积 / 外包面积 ≥ 此值算"矩形类"节点框（圆角矩形 0.997）
READING_ROW_QUANTUM = 4.0      # 阅读序：y0 按 4pt 量化后再按 x0 排序
NODE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,80}$"   # 可引用路径 nodes.<id>.label 的安全字符集

type Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class PathEvidence:
    """One native SVG <path>, located by its document order; points are page-top-left."""

    path_index: int                       # 在 crop SVG 中 <path> 的出现序（0 起）
    kind: Literal["shape", "line", "arrowhead"]
    points: tuple[Point, ...]             # shape: 扁平多边形顶点；line: 折线端点序列；arrowhead: 3 顶点
    bounds: Bounds


@dataclass(frozen=True, slots=True)
class NodeEvidence:
    node_id: str
    label_span_ids: tuple[str, ...]       # 逐字证明 label 的 span（按 IR 顺序）
    shape: PathEvidence                   # 证明 bbox 的真实形状
    clipped_bounds: Bounds                # shape.bounds ∩ 对象 bbox（与 node bbox 比较用的值）


@dataclass(frozen=True, slots=True)
class EdgeEvidence:
    edge_index: int                       # 在 DiagramIR.edges 中的下标（= 可引用路径 edges.<index>）
    source_node_id: str
    target_node_id: str
    line: PathEvidence                    # 连线（可与反向边共享同一条）
    arrowhead: PathEvidence               # 专属，不可复用
    tip: Point                            # 尖端（离另两点中点最远的顶点）
    base_mid: Point                       # 另两点中点
    line_end_at_target: Point             # 连线上接触 target 的那个端点


@dataclass(frozen=True, slots=True)
class DiagramQualification:
    """Independent geometry/verbatim proof of a DiagramIR; produced and replayed by one pure function."""

    object_id: str
    source: SourceAnchor                  # == DiagramIR.source（bbox = 对象 bbox）
    source_manifest_id: str
    source_span_ids: tuple[str, ...]      # 全部被引用的 span（node 序 + edge label 序，去重）
    nodes: tuple[NodeEvidence, ...]
    edges: tuple[EdgeEvidence, ...]
    method: str = DIAGRAM_METHOD
    scope: str = DIAGRAM_SCOPE            # 字段名必须叫 scope：RetrievalContext.scope 走 else 分支（retrieval.py:151-155）

    def __post_init__(self) -> None:
        if not self.nodes:
            raise ValueError("diagram qualification requires at least one proven node")
        ids = tuple(node.node_id for node in self.nodes)
        if len(set(ids)) != len(ids):
            raise ValueError("diagram qualification requires unique node ids")
```

**哪些字段来自模型推断、哪些来自几何证明**：

| 字段 | 来源 | 证明方式 |
|---|---|---|
| `DiagramNode.node_id` | 模型 | 仅要求 `NODE_ID_PATTERN`（可引用路径安全） |
| `DiagramNode.label` | 模型抄写 | == span 文本（N2） |
| `DiagramNode.bbox` | 模型 | == 真实矩形类形状边界（N5），且包含引用 span（N3） |
| `DiagramNode.source_span_ids` | 模型引用 | span 存在、在对象 bbox 内、在 node bbox 内、每个 span 只被引用一次（N3/C1） |
| `DiagramEdge.source_node_id/target_node_id` | 模型 | 连线端点 + 箭头尖端几何链（E2–E4） |
| `DiagramEdge.relationship` | 模型自由文本 | **不证明、不索引、不可引用**（原样保留在 IR，只进 lineage） |
| `DiagramEdge.label` | 模型抄写 | 非 None 时 == span 文本（E5），v1 不可引用 |
| 分组 / 泳道 | — | **v1 不做**（§9）：包含关系能确定性推出"形状 X 包住节点 A、B"，但没有任何 IR 字段承载，也无真实样本，留待有 `DiagramGroup` 需求时再加 |

### 2.2 `adapters/diagram_geometry.py`（新；xml.etree + `donut_geometry` 助手）

```python
"""Native SVG shapes of one object crop, in page-top-left coordinates.

Tolerant where ``source_paint._native_paths`` is strict: unknown tags (<image>, <text>,
<clipPath>) are skipped, not refused, because a diagram proof only needs the stroked
lines and filled polygons; glyph outlines (non-unit scale transforms) are skipped too.
"""

from dataclasses import dataclass
from math import hypot
from typing import Literal
from xml.etree import ElementTree

from enterprise_pdf_rag.adapters.donut_geometry import (
    Matrix, Point, _IDENTITY, _bounds, _compose, _matrix, _path_controls, _polygon, _transform,
)
from enterprise_pdf_rag.documents.models import Bounds
from enterprise_pdf_rag.processing.diagram_models import PathEvidence, ARROWHEAD_MAX_AREA, SHAPE_MIN_FILL_RATIO


@dataclass(frozen=True, slots=True)
class NativeShape:
    path_index: int
    d: str
    matrix: Matrix              # 合成后的矩阵（含页翻转）
    fill: str                   # "none" 或颜色
    stroke: str                 # "none" 或颜色
    closed: bool                # d 含 Z


def native_shapes(svg: str) -> tuple[NativeShape, ...]:
    """Walk the crop; compose every ``transform``; keep <path> under a unit-scale matrix only."""
    # 规则：tag ∈ {svg, g, path} 才递归/收集；defs/metadata/clipPath/image/text 跳过（不 raise）；
    # |a|==|d|==1 且 b==c==0（donut_geometry._unit_matrix 的判据）才收集——字形 path 的
    # matrix(size,0,0,size,x,y) 被排除；path_index 为收集序。


def _flatten(shape: NativeShape) -> tuple[Point, ...]:
    """闭合路径用 donut_geometry._polygon（M/L/C/Z，C 扁平化）；折线用 _path_controls（M/L 序列）。"""


def _area(points: tuple[Point, ...]) -> float:  # shoelace，绝对值


def rectangle_like(shape: NativeShape, object_bbox: Bounds) -> PathEvidence | None:
    """填充或描边的闭合路径，扁平后面积 ≥ SHAPE_MIN_FILL_RATIO × 外包面积；排除背景
    （外包 ⊇ object_bbox 且每边超出 > NODE_BBOX_TOLERANCE）。返回 kind="shape"。"""


def straight_lines(shape: NativeShape) -> PathEvidence | None:
    """stroke != none 且 fill == none，命令仅 M/L（≥1 段，不闭合）→ kind="line"，points 为折线顶点。
    含 C/Q 的连线返回 None（贝塞尔连线 v1 不支持，§9）。"""


def arrowhead(shape: NativeShape) -> PathEvidence | None:
    """fill != none 且闭合，扁平去重后恰 3 个顶点，外包面积 ≤ ARROWHEAD_MAX_AREA → kind="arrowhead"。"""


def tip_and_base(head: PathEvidence) -> tuple[Point, Point]:
    """尖端 = 离另两点中点最远的顶点；返回 (tip, base_mid)。三点共线 → ValueError("degenerate_arrowhead")。"""


def touches(bbox: Bounds, point: Point, *, tolerance: float) -> bool:
    """point 落在 bbox 外扩 tolerance 的框内。"""


def segment_crosses(bbox: Bounds, first: Point, last: Point, *, shrink: float) -> bool:
    """线段是否穿过 bbox 内缩 shrink 后的矩形（Liang–Barsky）；用于 E6"连线穿过第三个节点"。"""
```

坐标：`native_shapes` 从 crop 根开始以 `_IDENTITY` 走树，遇 `transform` 用 `_compose`；原生 `<g transform="matrix(1,0,0,-1,0,H)">` 被合成进去，所以 `_transform(matrix, p)` 直接得到 page-top-left（§1.3 实测）。**全程不用 `get_cdrawings()`**（bottom-left、且需要 PDF 本体），SVG crop 就是 `member.source_svg`，replay 可逐字节复算。

### 2.3 `adapters/diagram_qualification.py`（新；纯函数，不读 store、不调模型）

```python
"""Prove a DiagramIR against its own SVG crop and source spans; fail closed as a whole."""

from dataclasses import dataclass, replace

from enterprise_pdf_rag.adapters.diagram_geometry import (...)
from enterprise_pdf_rag.documents.models import Bounds, TextSpan
from enterprise_pdf_rag.figures.models import Confidence, Verification
from enterprise_pdf_rag.processing.diagram_description import describe_diagram
from enterprise_pdf_rag.processing.diagram_models import (...)
from enterprise_pdf_rag.processing.geometry import contains
from enterprise_pdf_rag.processing.typed_ir import DiagramIR, ObjectDescription


class DiagramQualificationError(ValueError):
    """``str(error)`` is the verbatim stage diagnostic: ``<subject>: <reason>``."""


@dataclass(frozen=True, slots=True)
class QualifiedDiagram:
    ir: DiagramIR                    # 与输入同内容，nodes 不变，edges 逐条 verification=VERIFIED，
                                     # verification=VERIFIED，diagnostics 追加一条 "Structure proven by <method>"
    description: ObjectDescription   # §3.4 方案 A 的确定性描述，producer="deterministic-diagram-description-v1"
    qualification: DiagramQualification


def qualify_diagram(
    *,
    svg: bytes,                              # stage "svg" 的字节（= crop_native_svg 输出）
    spans: tuple[TextSpan, ...],             # 整页 TextSidecar.spans（不是只有 owned 的）
    ir: DiagramIR,                           # stage "ir"
    source_manifest_id: str,
) -> QualifiedDiagram:
    """Raise DiagramQualificationError on the first failed rule; never partial."""
```

规则与诊断串见 §3.2。内部辅助（均私有）：`_label_matches_spans(label, spans) -> bool`（与 `visual_semantics._label_matches` 同语义：`_normal(label)` ∈ {各 span 文本, 空格拼接}；不 import 它，因它接收 `SvgElement`）、`_reading_order(nodes)`、`_prove_node(...)`、`_prove_edge(...)`。

### 2.4 `processing/diagram_description.py`（新；stdlib + 本包）

```python
"""Deterministic natural-language projection of a proven DiagramIR (option A, §3.4)."""

from typing import Literal

from enterprise_pdf_rag.figures.models import Confidence, SourceAnchor, Verification
from enterprise_pdf_rag.processing.diagram_models import READING_ROW_QUANTUM
from enterprise_pdf_rag.processing.typed_ir import DiagramIR, DiagramNode, ObjectDescription

DESCRIPTION_PRODUCER = "deterministic-diagram-description-v1"
DESCRIPTION_CONFIDENCE = Confidence(None, "deterministic projection of proven diagram structure; no semantic inference")
EDGE_ARROW = " -> "          # 与 context block / verify 的 edges.<index> 值共用同一分隔符


def reading_order(nodes: tuple[DiagramNode, ...]) -> tuple[DiagramNode, ...]:
    """(round(y0 / READING_ROW_QUANTUM), x0, node_id) 升序；确定性。"""


def diagram_description_text(ir: DiagramIR, *, language: Literal["en", "zh"] = "en") -> str:
    """Template words are the only non-verbatim tokens; every label is copied unchanged."""
    nodes = reading_order(ir.nodes)
    labels = [node.label for node in nodes]
    by_id = {node.node_id: node.label for node in ir.nodes}
    edges = [f"{by_id[e.source_node_id]}{EDGE_ARROW}{by_id[e.target_node_id]}" for e in ir.edges]
    if language == "zh":
        head = f"流程图，{len(labels)} 个节点：" + "；".join(labels) + "。"
        return head + ("".join(f"{edge}。" for edge in edges) if edges else "无连线。")
    count = f"{len(labels)} node{'s' if len(labels) != 1 else ''}"
    if not edges:
        return f"Diagram with {count}: " + "; ".join(labels) + ". No connecting edges."
    return (
        f"Diagram with {count} and {len(edges)} edge{'s' if len(edges) != 1 else ''}: "
        + "; ".join(labels) + ". " + " ".join(f"{edge}." for edge in edges)
    )


def describe_diagram(ir: DiagramIR, *, language: Literal["en", "zh"] = "en") -> ObjectDescription:
    return ObjectDescription(
        ir.object_id, ir.source,
        tuple(dict.fromkeys(s for n in ir.nodes for s in n.source_span_ids)),
        diagram_description_text(ir, language=language),
        DESCRIPTION_PRODUCER, DESCRIPTION_CONFIDENCE, Verification.VERIFIED,
    )
```

示例输出：
- p6（nodes-only，en）：`Diagram with 3 nodes: Foundation: 100% Digitalised Agency; Growth: Data-Driven Lead Generation; Intelligence: Sales & Management Copilots. No connecting edges.`
- 夹具（§6.1，en）：`Diagram with 2 nodes and 1 edge: PLAN; BUILD. PLAN -> BUILD.`
- 夹具（zh）：`流程图，2 个节点：PLAN；BUILD。PLAN -> BUILD。`

`language` 由 `SemanticObjectAdapter` 固定传 `"en"`（v1 不做语言探测；`zh` 模板先落地给测试与后续开关）。

### 2.5 `adapters/diagram_publication.py`（新；回执 + replay，仿 `chart_publication.py`）

```python
class DiagramPublicationReceipt(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["source-diagram-structure-qualification-v1"] = "source-diagram-structure-qualification-v1"
    object_id: str
    source_manifest_id: str
    ir: AssetRef                 # qualified_ir
    description: AssetRef        # qualified_description
    source_svg: AssetRef         # svg
    raw_ir: AssetRef             # ir
    raw_description: AssetRef    # description（模型描述，原样保留）
    view: AssetRef               # model_view
    qualification: DiagramQualification


def parse_diagram_receipt(data: bytes) -> DiagramPublicationReceipt: ...


@dataclass(frozen=True, slots=True)
class ValidatedDiagramMember:
    ir: DiagramIR
    description: ObjectDescription
    qualification: DiagramQualification


def validate_diagram_member(
    sources: LocalDocumentStore, assets: LocalDocumentStore, scope: ProcessingScope, member: RetrievalMember,
) -> tuple[DiagramIR, ObjectDescription, DiagramQualification]:
    """No model/network calls: rebuild the crop from the pinned page, replay qualify_diagram, compare."""
    receipt = parse_diagram_receipt(assets.get(member.qualification))
    if member.kind is not ObjectKind.DIAGRAM or member.page_index not in scope.selected_page_indices \
       or (receipt.object_id, receipt.source_manifest_id, receipt.ir, receipt.description, receipt.source_svg) \
          != (member.object_id, scope.source_manifest_id, member.ir, member.description, member.source_svg):
        raise ValueError("Diagram receipt does not match its retrieval member")
    if set(member.lineage_refs) != {receipt.raw_ir, receipt.raw_description, receipt.view} or len(member.lineage_refs) != 3:
        raise ValueError("Diagram publication requires the complete raw branch and model-view closure")
    source = sources.load(scope.source_manifest_id)
    anchor = receipt.qualification.source
    if source.manifest.source.sha256 != scope.source_sha256 or \
       (anchor.document_sha256, anchor.page_index) != (scope.source_sha256, member.page_index):
        raise ValueError("Diagram qualification is outside the pinned source scope")
    page = source.manifest.pages[member.page_index]
    expected_crop = crop_native_svg(sources.get(page.svg).decode(), width=page.width, height=page.height, bbox=anchor.bbox).encode()
    if assets.get(member.source_svg) != expected_crop:
        raise ValueError("Diagram SVG crop does not derive from the pinned source page and anchor")
    raw_ir = TypeAdapter(DiagramIR).validate_json(assets.get(receipt.raw_ir), strict=True)
    if raw_ir.source != anchor or raw_ir.object_id != member.object_id:
        raise ValueError("Raw diagram IR is not bound to the qualified anchor")
    spans = read_text_sidecar(sources, source, member.page_index).spans
    expected = qualify_diagram(svg=expected_crop, spans=spans, ir=raw_ir, source_manifest_id=scope.source_manifest_id)
    ir = TypeAdapter(DiagramIR).validate_json(assets.get(member.ir), strict=True)
    description = TypeAdapter(ObjectDescription).validate_json(assets.get(member.description), strict=True)
    if (ir, description, receipt.qualification) != (expected.ir, expected.description, expected.qualification):
        raise ValueError("Diagram projection or receipt differs from independent source qualification")
    return ir, description, receipt.qualification
```

`DiagramQualificationError` 在 replay 中原样冒泡（它是 `ValueError` 子类）→ 挂载拒绝，fail closed。

### 2.6 `processing/index_text.py` 增量（投影）

```python
from enterprise_pdf_rag.processing.diagram_description import EDGE_ARROW, reading_order
from enterprise_pdf_rag.processing.typed_ir import DiagramIR, TypedIR


def has_citable_structure(diagram: DiagramIR) -> bool:
    """At least one node whose label is verbatim source text (non-empty label with span evidence)."""
    return any(node.label.strip() and node.source_span_ids for node in diagram.nodes)


def diagram_index_text(diagram: DiagramIR, *, fallback: str) -> str:
    """``diagram figure`` + labels in reading order + ``A -> B`` per edge; otherwise ``fallback``."""
    if not has_citable_structure(diagram):
        return fallback
    by_id = {node.node_id: node.label for node in diagram.nodes}
    parts = ["diagram figure", *(node.label for node in reading_order(diagram.nodes))]
    parts.extend(f"{by_id[e.source_node_id]}{EDGE_ARROW}{by_id[e.target_node_id]}" for e in diagram.edges)
    return " ".join(part.strip() for part in parts if part.strip())


def member_index_text(ir: TypedIR, description_text: str) -> str:
    """Text / list / group / table members index their description; charts and diagrams are projected."""
    if isinstance(ir, ChartIR):
        return chart_index_text(ir, fallback=description_text)
    if isinstance(ir, DiagramIR):
        return diagram_index_text(ir, fallback=description_text)
    return description_text
```

示例：夹具 → `diagram figure PLAN BUILD PLAN -> BUILD`；p6 → `diagram figure Foundation: 100% Digitalised Agency Growth: Data-Driven Lead Generation Intelligence: Sales & Management Copilots`。与 v4 header 叠加后（`processing_retrieval.py:217-221` 不变）：`<display_title> | <page_title> | <section>\ndiagram figure …`。

"可引用判据是内容属性不是 `Verification`"（ADR 0012:139-142）：`has_citable_structure` 只看 label/证据是否存在，与 `DiagramIR.verification` 无关。

---

## 3. stage 产出与资格判定

### 3.1 DIAGRAM 的 stage 全集（改动后）

| stage | 产出者 | 内容 | 状态 |
|---|---|---|---|
| `native_crop`, `source_text`, `svg`, `model_render`, `model_view`, `ir_raw`, `ir`, `description_raw`, `description` | 不变（`semantic_objects.py:176-216`） | 同今天 | 同今天 |
| `qualified_ir` | 新 | `QualifiedDiagram.ir`（DiagramIR，VERIFIED） | 通过时 SUCCEEDED；否则**不写** |
| `qualified_description` | 新 | `QualifiedDiagram.description`（ObjectDescription，方案 A） | 同上 |
| `qualification` | 改 | 通过：`DiagramPublicationReceipt` JSON（SUCCEEDED）；失败：`writer.diagnostic("qualification", str(error))`（UNAVAILABLE，诊断逐字）；`ir`/`description` 任一缺失：`writer.diagnostic("qualification", "Both actual source-bound branches are required; no description-only fallback is admitted.")`（与图表 `semantic_objects.py:567-575` 同串） | |

`qualified_claim_count = len(nodes) + len(edges)`（通过时），否则 0。IMAGE 仍写原来的 UNAVAILABLE 诊断（`semantic_objects.py:217-222` 串不变，只是加了 `if item.kind is not ObjectKind.DIAGRAM` 分支）。**FORMULA**：本方案单独落地时与 IMAGE 相同；公式方案（同属 ADR 0015）在同一处把 FORMULA 接到它自己的 `self._formula(...)` 分支，合入后该处是"DIAGRAM → 本方案 / FORMULA → 公式方案 / 其余 → 原诊断"的三路分派（§10）。

### 3.2 资格规则（无模型；每条给通过条件与失败诊断，`str(DiagramQualificationError)` = 诊断串）

记 `O` = `ir.source.bbox`（对象 bbox），`S_in` = 页内 `contains(O, span.bbox, tolerance=SPAN_INSIDE_TOLERANCE)` 的 span 集合，`shapes = native_shapes(svg)`。

**节点（对每个 node，按 IR 顺序）**

| # | 通过条件 | 失败诊断（逐字） |
|---|---|---|
| N0 | `re.fullmatch(NODE_ID_PATTERN, node.node_id)` | `node <id>: node_id_is_not_path_safe` |
| N1 | `contains(O, node.bbox)`（默认 1e-6） | `node <id>: bbox_outside_object` |
| N2 | `node.label.strip()` 非空 **且** `node.source_span_ids` 非空 **且** `_label_matches_spans(node.label, cited_spans)`（`_normal` 后等于某个 span 文本或按引用序空格拼接） | 空 label 或无证据：`node <id>: empty_label_without_source_occurrence`；不逐字：`node <id>: label_is_not_verbatim_source_text` |
| N3 | 每个引用 span 存在于页、∈ `S_in`、且 `contains(node.bbox, span.bbox, tolerance=SPAN_INSIDE_TOLERANCE)` | `node <id>: cited_span_missing:<span_id>` / `node <id>: cited_span_outside_node_bbox:<span_id>` |
| N4 | 该 span 未被其它 node / edge label 引用过 | `node <id>: span_cited_twice:<span_id>` |
| N5 | 存在**恰一个**未被占用的 `rectangle_like` 形状，使 `clipped = shape.bounds ∩ O` 与 `node.bbox` 每边 `abs(Δ) <= NODE_BBOX_TOLERANCE`；占用之 | 0 个：`node <id>: no_native_shape_matches_bbox`；≥2 个：`node <id>: ambiguous_native_shape` |
| N6 | 与其它 node bbox 内部不相交（`_intersects` 为假） | `node <id>: overlaps_node:<other_id>` |

**边（对每条 edge，按 IR 下标 i）**

| # | 通过条件 | 失败诊断 |
|---|---|---|
| E0 | `source != target`；`(source, target)` 对不重复 | `edge <i>: self_loop` / `edge <i>: duplicate_edge` |
| E1 | 两端 node 存在（`visual_semantics` 已保证，重检） | `edge <i>: unknown_node:<id>` |
| E2 | 存在 `straight_lines` 折线 L，其一端 `touches(A.bbox, p, tolerance=CONNECT_TOLERANCE)`、另一端 `touches(B.bbox, q, …)`（A=source, B=target；任一方向）；多条候选取 `path_index` 最小；**可与其它边共享**（双向箭头共用一条线） | `edge <i>: no_connector_between_nodes` |
| E3 | 存在未被占用的 `arrowhead` H：`tip(H)` `touches(B.bbox)` 且 **不** `touches(A.bbox)`，且 `hypot(base_mid(H) - q) <= ARROW_JOIN_TOLERANCE`（q = L 上接触 B 的端点）；占用之 | `edge <i>: no_arrowhead_pointing_to_target`（无箭头连线的处理见 §8 拍板点 3） |
| E4 | L 的每条线段不 `segment_crosses(C.bbox, …, shrink=CONNECT_TOLERANCE)` 任何第三个 node C | `edge <i>: connector_crosses_node:<id>` |
| E5 | `edge.label` 为 None ↔ `edge.source_span_ids` 为空；非 None 时逐字匹配（同 N2/N3/N4，span 须 ∈ `S_in`） | `edge <i>: label_is_not_verbatim_source_text` 等 |

**覆盖（对象级，防"模型漏节点"）**

| # | 通过条件 | 失败诊断 |
|---|---|---|
| C1 | `S_in` 中每个 span 都被恰一个 node 或 edge label 引用 | `object: uncited_source_span:<span_id>` |
| C2 | `svg` 能被 `ElementTree` 解析且 `native_shapes` 非空 | `object: unparseable_svg` / `object: no_native_shapes` |

C1 是"召回不可证"的唯一确定性护栏：模型漏掉的节点必然留下一条未被引用的 span（前提是 partition 没把标题/图注塞进 Diagram 区域——那是 partition 的问题，同样 fail closed）。

**通过后的产物**：`qualification = DiagramQualification(object_id, ir.source, source_manifest_id, cited_span_ids, nodes=..., edges=...)`；`ir' = replace(ir, edges=tuple(replace(e, verification=VERIFIED) for e in ir.edges), verification=VERIFIED, diagnostics=(*ir.diagnostics, f"Structure proven independently: {DIAGRAM_METHOD}"))`；`description' = describe_diagram(ir')`。

### 3.3 两个真实边界的处理

- **空 label 节点（p5）**：N2 直接拒整对象。理由：空 label 节点在检索/回答里无可引用文本，放行只会让 `edges.<i>` 出现 ` -> ` 这样的空值；且 p5 的根因是 partition 没把 6 个 span 归属给对象（`unowned_full_span_ids`），正确修法在 partition，不在资格校验里放水。
- **无连线堆叠图（p6）**：`edges == ()` 时允许 nodes-only 放行（N*/C* 全过即可），描述/投影只写节点序列（`No connecting edges.`）。理由：三个节点的 label 逐字可证、bbox 有真实形状、覆盖完整，检索价值真实存在（"Growth 阶段是什么"可答）；而"先后关系"没有任何几何证据，所以**不生成任何 `edges.`**，模型无法引用顺序，`SYSTEM_RULES` 明说不得推断（§4.5）。

### 3.4 description 分支：两种方案与推荐

**(A) 零模型确定性描述（推荐，v1 实施）**：`processing/diagram_description.py::describe_diagram`（§2.4），落盘为新 stage `qualified_description`，producer `deterministic-diagram-description-v1`，`Verification.VERIFIED`。
- 与 ADR 0012/0013 的关系：原 `description` 资产**一字不改**，仍在盘上并进 lineage（`raw_description`）；`qualified_description` 是新内容寻址资产，图表 `_qualify` 正是这样做的（`semantic_objects.py:600-604`）。
- 为什么落盘为 stage 而不是"检索期投影"：(1) `eligibility`/`build` 必需 4 个 stage 中就有 `description`（或 `qualified_description`），它决定 `member.description` = embedding 的缓存键（`processing_retrieval.py:222,258-273`）；(2) `member_text` 在旧 policy 下要能原样返回它（§4.2）；(3) `RetrievalContext.description.text` 是 context block 的 `description_text`，回答链要一个 VERIFIED 的可读描述。检索期投影只解决索引文本（那是 `member_index_text` 的事），解决不了成员绑定。
- 与"two branches"的关系：A 不是模型分支，它是 IR 的模板投影——与 `source_table_description(page, item, result.table)`（`semantic_objects.py:265`）从 TableIR 生成文字描述完全同构。

**(B) VLM 描述 + 资格校验（可选增强，不在 v1）**：复用 `description` stage 的 `ObjectDescription.text`，校验通过后作为 `qualified_description`。校验只能做到：文本 whitespace-fold 后**包含每个 node label 的逐字子串**、不含 label 之外的数字（`_NUMBER_RE`）、以 `(obs-…)` 引用的 id 均映射到本对象 span、无 `{ [ <` 开头。做不到"逐 claim 对应 edge"：`VisualDescriptionDTO` 没有 claims 结构（对比图表 `FigureDescriptionDTO.claims`），自由散文里的"A 指向 B"无法机械对齐到 `edges.<i>`。→ 只有把描述 DTO 改成 claims 列表（改 prompt/任务名会作废缓存，`json_completion.py:269-279`）才值得做。**结论：v1 用 A；B 作为 ADR 里的 Rejected/Deferred 记录。**

---

## 4. 索引 / 检索 / 回答链接入点

### 4.1 eligibility 双闸（`adapters/processing_retrieval.py:101-130`）

```python
    if record.kind not in (
        ObjectKind.TEXT, ObjectKind.LIST, ObjectKind.GROUP, ObjectKind.TABLE,
        ObjectKind.CHART, ObjectKind.DIAGRAM,                      # +DIAGRAM
    ):
        return False, f"{record.kind.value} objects are not retrievable"
    stages = {stage.stage: stage for stage in record.stages}
    required = (
        ("qualified_ir", "qualified_description", "qualification", "svg")
        if record.kind in (ObjectKind.CHART, ObjectKind.DIAGRAM)  # CHART → in (CHART, DIAGRAM)
        else ("ir", "description", "qualification", "svg")
    )
    ...
        if record.kind is ObjectKind.DIAGRAM:
            return False, "Diagram structure is not proven; only geometry-qualified diagrams are retrievable"
        return False, "required qualification stages are incomplete"
```

`build`（158-162）同样把 `if record.kind is ObjectKind.CHART` 改为 `in (CHART, DIAGRAM)`；lineage（173-203）加 `elif record.kind is ObjectKind.DIAGRAM: lineage_stages = ("ir", "description", "model_view")`（三个都必须 SUCCEEDED，否则同样 `raise ValueError`）。

**与公式方案叠加**：两份都合入后 kind 白名单是 `(TEXT, LIST, GROUP, TABLE, CHART, DIAGRAM, FORMULA)`，两处 `required` 的条件是 `record.kind in (CHART, DIAGRAM, FORMULA)`，kind 特定拒绝串三条并列（TABLE / DIAGRAM / FORMULA）。表格方案**不改** `eligibility`（PENDING 与 VERIFIED 网格都照旧放行），与本方案无重叠（§10）。

`_qualified`（347-356）与 `resolve_processing_context`（359-374）：`elif member.kind is ObjectKind.DIAGRAM: return validate_diagram_member(...)`。返回类型联合加 `DiagramIR` / `DiagramQualification`。`source_publication.py:79-83` 同样加 `elif`。

### 4.2 索引文本与 policy v5（四步模板，`processing_retrieval.py:60-99`）

```python
# v5 embeds the qualified-IR projection of Diagram members (proven nodes in reading order,
# ``A -> B`` per proven edge) and of Formula members (readable + linear + token texts),
# both admitted by ADR 0015.
_POLICY = "source-transcription-and-scoped-chart-qualification-v5"          # 1. 改串
PROJECTED_CHART_POLICIES = frozenset({
    _POLICY,
    "source-transcription-and-scoped-chart-qualification-v4",               # 2. 旧值入集合
    "source-transcription-and-scoped-chart-qualification-v3",
    "source-transcription-donut-and-displayed-bar-v2",
})
CONTEXTUAL_POLICIES = frozenset({_POLICY, "source-transcription-and-scoped-chart-qualification-v4"})  # v4 必须保留，否则 v4 快照丢 header
# 3. 新门（本方案是这三个常量的唯一编辑者；公式方案复用同一个集合，不再新建常量）
VISUAL_PROJECTION_POLICIES = frozenset({_POLICY})


def member_text(assets, plan, member, context=None) -> str:
    payload = assets.get(member.description)
    if member.kind is ObjectKind.CHART:
        body = TypeAdapter(TextDescription).validate_json(payload).text
        if plan.qualification_policy in PROJECTED_CHART_POLICIES:
            body = member_index_text(TypeAdapter(ChartIR).validate_json(assets.get(member.ir)), body)
    else:
        body = TypeAdapter(ObjectDescription).validate_json(payload).text
        if member.kind is ObjectKind.DIAGRAM and plan.qualification_policy in VISUAL_PROJECTION_POLICIES:   # 4. 门
            body = member_index_text(TypeAdapter(DiagramIR).validate_json(assets.get(member.ir)), body)
    if plan.qualification_policy not in CONTEXTUAL_POLICIES:
        return body
    return contextual_index_text(body, context)
```

- 旧快照：v1–v4 快照里根本没有 DIAGRAM 成员（当时 eligibility 拒绝），所以第 4 步的门在旧快照上不可达；保留它是为了"每个 policy 只打它当年嵌入的串"这条规则保持机械可查。任何代码不按 policy 拒收（ADR 0012:143-146 / 0013:117-118）。
- `_POLICY` 进 `RetrievalPlan.snapshot_id`（`processing/retrieval.py:56-80`）→ 新 snapshot id，旧的仍可 `load`；embedding 缓存指纹含 `sha256(text)`，投影文本自动换向量。
- 迁移：已发布文档 `index + publish` 即可（ADR 0012:172-174）；`ingest --stage semantics` 需重跑才会产出 `qualified_*`（stage 指纹含 producer，未变 → 走缓存；Diagram 的 qualification 是新 stage 名，不会命中旧缓存）。
- **协调（已拍板，见 §10）**：`_POLICY` / `PROJECTED_CHART_POLICIES` / `CONTEXTUAL_POLICIES` / `VISUAL_PROJECTION_POLICIES` 这四个常量**只由本方案编辑一次**。表格方案零读取侧 policy 分支（它的网格证据在 `ir` 资产里，snapshot id 随字节自然换新），不碰这四个常量；公式方案只在 `member_text` 的 `else` 里追加 `member.kind is ObjectKind.FORMULA and plan.qualification_policy in VISUAL_PROJECTION_POLICIES` 一支，**复用**同一个 `VISUAL_PROJECTION_POLICIES`，不新建 `FORMULA_PROJECTION_POLICIES`。三份方案的 policy 串取值一律是 `"source-transcription-and-scoped-chart-qualification-v5"`。

### 4.3 context block（`processing/context_builder.py`）

```python
class BlockKind(StrEnum):
    ...
    CHART = "chart"
    DIAGRAM = "diagram"                                             # +

_KIND_OF_OBJECT = {..., ObjectKind.DIAGRAM: BlockKind.DIAGRAM}     # +


@dataclass(frozen=True, slots=True)
class DiagramNodeEvidence:
    node_id: str
    label: str
    bbox: Bounds
    source_span_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiagramEdgeEvidence:
    edge_index: int
    source_node_id: str
    target_node_id: str
    source_label: str
    target_label: str
    bbox: Bounds                       # 两端 node bbox 的并集（引用定位用）

    @property
    def value(self) -> str:            # 可引用值，与 index_text / description 用同一 EDGE_ARROW
        return f"{self.source_label}{EDGE_ARROW}{self.target_label}"


@dataclass(frozen=True, slots=True)
class ContextBlock:
    ...
    chart_fields: tuple[ChartFieldEvidence, ...] = ()
    nodes: tuple[DiagramNodeEvidence, ...] = ()                     # +
    edges: tuple[DiagramEdgeEvidence, ...] = ()                     # +

    def prompt_text(self) -> str:
        ...
        elif self.kind is BlockKind.DIAGRAM:                        # + 放在 TABLE 分支之后、else 之前
            lines.append(f"diagram nodes={len(self.nodes)} edges={len(self.edges)}")
            lines.extend(f"nodes.{node.node_id}.label: {node.label}" for node in self.nodes)
            lines.extend(f"edges.{edge.edge_index}: {edge.value}" for edge in self.edges)
        else: ...
```

渲染示例（夹具）：
```
[member <id>] kind=diagram page_index=2 scope=diagram-structure-source-geometry-v1 verification=verified
diagram nodes=2 edges=1
nodes.n1.label: PLAN
nodes.n2.label: BUILD
edges.0: PLAN -> BUILD
```

`build_context_block`（在 `ChartIR` 分支后、224 行 raise 前）：
```python
    if isinstance(ir, DiagramIR):
        if member.kind is not ObjectKind.DIAGRAM:
            raise ValueError("Retrieval member kind does not match its typed IR")
        by_id = {node.node_id: node for node in ir.nodes}
        return ContextBlock(
            *common, BlockKind.DIAGRAM, member.page_index, context.scope, ir.verification,
            context.description.text,
            nodes=tuple(DiagramNodeEvidence(n.node_id, n.label, n.bbox, n.source_span_ids) for n in ir.nodes),
            edges=tuple(
                DiagramEdgeEvidence(i, e.source_node_id, e.target_node_id,
                                    by_id[e.source_node_id].label, by_id[e.target_node_id].label,
                                    _union(by_id[e.source_node_id].bbox, by_id[e.target_node_id].bbox))
                for i, e in enumerate(ir.edges)
            ),
        )
```
`ImageIR` / `FormulaIR` 仍撞 224 行的 raise。可引用路径：`nodes.<node_id>.label`（值 = label 逐字）、`edges.<index>`（值 = `from_label -> to_label`）。用下标而非 id，因为 `DiagramEdge` 没有 id 字段且不想改 frozen dataclass（`items.<index>` 先例，`context_builder.py:119-121`）。

### 4.4 verify（`answers/verify.py`）与六处联动

| # | 位置 | 改动 |
|---|---|---|
| 1 | `answers/models.py:18-22` `ClaimKind` | `+ DIAGRAM_NODE = "diagram_node"`, `+ DIAGRAM_EDGE = "diagram_edge"` |
| 2 | `answers/prompt.py:21` `ModelClaim.kind` | `Literal["quote", "cell", "chart_value", "diagram_node", "diagram_edge"]` |
| 3 | `answers/prompt.py:45-49` `SYSTEM_RULES` 第 1 条 | 追加：`kind \`diagram_node\` uses \`nodes.<node_id>.label\` and \`text\` is exactly that label; kind \`diagram_edge\` uses \`edges.<index>\` and \`text\` is exactly the printed \`<from> -> <to>\` pair. A diagram's edges are its drawn arrows only: never infer an order, a next step or a relationship that is not printed as an \`edges.\` line.` |
| 4 | `answers/verify.py:61-65` `_PATH_PREFIX` | `+ ClaimKind.DIAGRAM_NODE: "nodes."`, `+ ClaimKind.DIAGRAM_EDGE: "edges."`（本方案保持"一 kind 一串"原形；**公式方案把整张表的值改成 tuple**——FORMULA 需要 `("formula.", "tokens.")` 两个前缀——届时这两行一并写成 `("nodes.",)` / `("edges.",)`，`:320-322` 的 `str.startswith` 原生接受 tuple，语义不变，见 §10） |
| 5 | `answers/verify.py:66-70` `_BLOCK_KINDS` | `+ ClaimKind.DIAGRAM_NODE: {BlockKind.DIAGRAM}`, `+ ClaimKind.DIAGRAM_EDGE: {BlockKind.DIAGRAM}` |
| 6 | `answers/verify.py:327-337` `verify_claims` 分发 | `elif kind is ClaimKind.DIAGRAM_NODE: outcome = _verify_diagram_node(claim, block)` / `elif kind is ClaimKind.DIAGRAM_EDGE: outcome = _verify_diagram_edge(claim, block)`（放在 `else:` 图表分支之前） |

新增（`verify.py`，`_verify_cell` 之后）：
```python
_NODE_LABEL_RE = re.compile(r"nodes\.(?P<node>[A-Za-z0-9_-]+)\.label")
_EDGE_RE = re.compile(r"edges\.(?P<index>\d+)")


def _verify_diagram_node(claim: ModelClaim, block: ContextBlock) -> VerifiedClaim | RejectedClaim:
    match = _NODE_LABEL_RE.fullmatch(claim.field_path)
    if match is None:
        return _reject(claim, AbstainReason.MODEL_OUTPUT_INVALID, "diagram node claims cite nodes.<id>.label")
    node = next((item for item in block.nodes if item.node_id == match.group("node")), None)
    if node is None:
        return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "cited node is not in the block")
    if _norm(claim.text) != _norm(node.label):
        return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claim text differs from node label")
    return VerifiedClaim(claim.claim_id, ClaimKind.DIAGRAM_NODE, claim.text, None, None, (
        ClaimCitation(block.member_id, block.kind, block.page_index, claim.field_path,
                      (node.node_id, *node.source_span_ids), node.bbox, node.label),
    ))


def _verify_diagram_edge(claim: ModelClaim, block: ContextBlock) -> VerifiedClaim | RejectedClaim:
    match = _EDGE_RE.fullmatch(claim.field_path)
    if match is None:
        return _reject(claim, AbstainReason.MODEL_OUTPUT_INVALID, "diagram edge claims cite edges.<index>")
    edge = next((item for item in block.edges if item.edge_index == int(match.group("index"))), None)
    if edge is None:
        return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "cited edge is not in the block")
    if _norm(claim.text) != _norm(edge.value):
        return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claim text differs from the printed edge")
    return VerifiedClaim(claim.claim_id, ClaimKind.DIAGRAM_EDGE, claim.text, None, None, (
        ClaimCitation(block.member_id, block.kind, block.page_index, claim.field_path,
                      (claim.field_path, edge.source_node_id, edge.target_node_id), edge.bbox, edge.value),
    ))
```

- `ClaimCitation` **不需要新字段**：`evidence_ids` 装 node id + span id（或 edge 路径 + 两端 node id），`bbox` 装 node bbox / 并集 bbox，`quote` 装 label / `A -> B`，`chart_citation=None`，`page_title` 由 `_with_page_titles`（`answer_service.py:274+`）照常补。`chat_schemas.py:66-110` 用枚举做类型，新增成员自动序列化；但 `scripts/enterprise_pdf_rag/check_schema.py:103,107-119` 把 `rag-chat-v1` 的四个契约模型与 `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` **全等**比对且**没有 `--write`**，`ClaimKind` / `BlockKind` 各多两个值会让它变红 → 必须手工重生成该 JSON（重生成片段见公式方案 §7 阶段 3，三份方案共用同一段；合并时只跑一次）。
- `prose_grounded`（345-366）：label 里的数字（`100%`）通过 `_numbers(claim.text)` 与 `cited.quote` 自动进入 `allowed`，无需改。
- `answer_service.chart_evidence`（209-215）只在图表分支被调用，Diagram 不触发，无需改。`select_context` 的图表座位不给 Diagram（§9 非目标）。

### 4.5 semantic_objects 接线（`adapters/semantic_objects.py:197-223` 替换）

```python
        branch: dict[str, StageOutcome] = {}
        for name, value, raw, diagnostic in (("ir", ...), ("description", ...)):
            if raw is not None:
                stages.append(writer.save(name + "_raw", raw))
            outcome = (writer.diagnostic(name, diagnostic or "No source-bound result was returned", failed=True)
                       if value is None else writer.save(name, TypeAdapter[object](type(value)).dump_json(value)))
            stages.append(outcome)
            branch[name] = outcome
        if item.kind is not ObjectKind.DIAGRAM:
            stages.append(writer.diagnostic("qualification", "Visual semantics are source-bound model inferences; an independent field/relationship verifier is not available for this object."))
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        if not isinstance(result.ir, DiagramIR) or result.description is None:
            stages.append(writer.diagnostic("qualification", "Both actual source-bound branches are required; no description-only fallback is admitted."))
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        try:
            qualified = qualify_diagram(svg=result.crop_svg, spans=page.text.spans, ir=result.ir,
                                        source_manifest_id=page.source_manifest_id)
        except DiagramQualificationError as error:
            stages.append(writer.diagnostic("qualification", str(error)))
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        svg_stage = next(stage for stage in stages if stage.stage == "svg")
        view_stage = next(stage for stage in stages if stage.stage == "model_view")
        ir_stage = writer.save("qualified_ir", TypeAdapter(DiagramIR).dump_json(qualified.ir))
        desc_stage = writer.save("qualified_description", TypeAdapter(ObjectDescription).dump_json(qualified.description))
        receipt = DiagramPublicationReceipt(
            object_id=item.object_id, source_manifest_id=page.source_manifest_id,
            ir=_ref(ir_stage), description=_ref(desc_stage), source_svg=_ref(svg_stage),
            raw_ir=_ref(branch["ir"]), raw_description=_ref(branch["description"]), view=_ref(view_stage),
            qualification=qualified.qualification,
        )
        stages.extend((ir_stage, desc_stage, writer.save("qualification", receipt.model_dump_json().encode())))
        return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages),
                                      len(qualified.ir.nodes) + len(qualified.ir.edges))
```

与 `_table` 一样**不看 `qualification_policy`**（确定性校验总是运行；真实入口 `pdf_ingestion.py:174` 固定 `"none"`，若门控则永远不会放行）。`result.crop_svg` 与 `svg` stage 字节相同（`semantic_objects.py:192`）。

**与公式方案的叠加**：上面第一行 `if item.kind is not ObjectKind.DIAGRAM:` 在公式方案合入后改为先分派 FORMULA（`if item.kind is ObjectKind.FORMULA: return self._formula(...)`）、再 `if item.kind is not ObjectKind.DIAGRAM:` 写原诊断，最后走 DIAGRAM 分支；IMAGE 仍落在原诊断上，那句诊断串一字不改（§10）。

---

## 5. 既有文件最小改动清单

| 文件 | 函数 / 常量 | 改动 | 行号（c15525b） |
|---|---|---|---|
| `adapters/semantic_objects.py` | `process` 视觉分支 | §4.5：捕获 `ir`/`description` outcome；DIAGRAM 走 `qualify_diagram`，其余 kind 保留原诊断；新 import `qualify_diagram`, `DiagramQualificationError`, `DiagramPublicationReceipt`, `DiagramIR` | 197-223 |
| `adapters/processing_retrieval.py` | `_POLICY` / `PROJECTED_CHART_POLICIES` / `CONTEXTUAL_POLICIES` / 新 `VISUAL_PROJECTION_POLICIES` | §4.2 四步（**三份方案里唯一编辑这四个常量的地方**） | 60-74 |
| 同上 | `member_text` | DIAGRAM 投影门 | 77-99 |
| 同上 | `eligibility` | 白名单 + 必需 stage 元组 + 新拒绝串 | 101-130 |
| 同上 | `ProcessingRetrieval.build` | 必需 stage 元组、DIAGRAM lineage | 158-162, 173-203 |
| 同上 | `_qualified` / `resolve_processing_context` | DIAGRAM → `validate_diagram_member`；返回类型联合 | 347-356, 359-374 |
| `adapters/source_publication.py` | `validate_processing_source` | `elif member.kind is ObjectKind.DIAGRAM: validate_diagram_member(...)` | 79-83 |
| `processing/retrieval.py` | `RetrievalContext.qualification` | 联合加 `DiagramQualification`（`.scope` 不改：走 else 分支读 `.scope`） | 143-155 |
| `processing/index_text.py` | `member_index_text` + 新 `has_citable_structure` / `diagram_index_text` | §2.6 | 85-89 |
| `processing/context_builder.py` | `BlockKind`, `_KIND_OF_OBJECT`, 新 evidence 类, `ContextBlock` 两字段, `prompt_text`, `build_context_block` | §4.3 | 21-35, 70-122, 211-224 |
| `answers/models.py` | `ClaimKind` | 两个成员 | 18-22 |
| `answers/prompt.py` | `ModelClaim.kind`, `SYSTEM_RULES` | §4.4 | 21, 45-49 |
| `answers/verify.py` | `_PATH_PREFIX`, `_BLOCK_KINDS`, 新 `_verify_diagram_node/_edge`, `verify_claims` | §4.4 | 61-70, 137 之后, 327-337 |
| `adapters/processing_export.py`（可选） | coverage 行 | `elif kind is ObjectKind.DIAGRAM: key = "diagram_structure_qualified"`（新增列，否则 Diagram 会被计成 `source_transcription_qualified`） | 193-210 |
| `tests/enterprise_pdf_rag/processing/test_context_builder.py` | `test_kind_mismatch_and_unsupported_ir_are_refused` | `DiagramIR` 反例改为 `ImageIR("diagram-1", _ANCHOR, (), (), ())` + kind IMAGE | 273-281 |
| `tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py` | policy 断言 | `-v4` → `-v5`（全仓 `git grep "qualification-v4" tests/` 逐个更新，含 `test_retrieval_snapshot.py`） | 205 |
| `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` | `ClaimKind` / `BlockKind` 两处 enum | 手工重生成（`check_schema.py` 无 `--write`；与公式方案共用同一段重生成脚本，合并后只跑一次） | — |
| 文档 | `docs/enterprise-pdf-rag/adr/0015-diagram-and-formula-retrievable.md`（新，**与公式方案合写同一份**：同一次 policy v5 升级 + 视觉对象的无模型资格；表格网格证明是独立的 ADR 0014）、`src/enterprise_pdf_rag/CLAUDE.md` 不变量段（Diagram 入 same-SVG 列表）、`CHANGELOG.md` | | |

不改：`visual_semantics.py`（两路模型分支原封不动，含 `description_index_eligible` 空钩子——不复用它，避免"模型出口自授资格"的歧义）、`visual_semantic_schemas.py`、`typed_ir.py`（`DiagramEdge` 不加 id）、`processing_store.py`（`qualified_*` 已兼容）、`draft_publication.py`、`answer_service.py`、`hybrid_search.py`、`document_catalog.py`、HTTP 层。

---

## 6. 测试计划

### 6.1 离线夹具：扩展 `tests/enterprise_pdf_rag/adapters/test_pdf_ingestion.py::authored_pdf`

pdfspine API 已实测（§1.3）。页 240×160，top-left 坐标；文字用内嵌字体（`embedded_font=True`），否则 SVG 出 `<text>` 会被 `render_svg_png` 拒绝。

```python
# tests/enterprise_pdf_rag/adapters/test_pdf_ingestion.py（新增常量与函数；authored_pdf 加 diagram_page 形参）
DIAGRAM_NODES = {  # node_id -> (rect top-left, label, label origin)
    "n1": ((20.0, 70.0, 90.0, 100.0), "PLAN", (28.0, 89.0)),
    "n2": ((150.0, 70.0, 220.0, 100.0), "BUILD", (158.0, 89.0)),
}
DIAGRAM_LINE = ((90.0, 85.0), (142.0, 85.0))                       # n1 右边中点 → 箭头底边
DIAGRAM_ARROWHEAD = ((142.0, 81.0), (142.0, 89.0), (150.0, 85.0))  # 底边两点 + 尖端落在 n2 左边 x=150
DIAGRAM_REGION = (15.0, 65.0, 225.0, 105.0)                        # 桩 partition 提议的 Diagram 区域


def _draw_diagram(page: pdfspine.Page, fontname: str) -> None:
    for rect, label, origin in DIAGRAM_NODES.values():
        page.draw_rect(rect, color=(0, 0, 0), width=1)
        page.insert_text(origin, label, fontsize=10, fontname=fontname)
    page.draw_line(*DIAGRAM_LINE, width=1)
    shape = page.new_shape()                       # draw_polyline 无 fill/closePath 形参（实测被 **_ignored 吞掉）
    shape.draw_polyline(list(DIAGRAM_ARROWHEAD))
    shape.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.5, closePath=True)
    shape.commit()


# 三份方案共用的最终签名（§10 统一）：表格方案把 table_page 扩成 bool | TableSpec，
# 公式方案加 formula_page / formula_rule；三种版面都画在最后一页，互斥。
def authored_pdf(path, *, page_count, label, embedded_font=False,
                 table_page: "bool | TableSpec" = False,
                 diagram_page: bool = False, diagram_caption: bool = False,
                 formula_page: bool = False, formula_rule: bool = True) -> Path:
    assert sum(bool(v) for v in (table_page, diagram_page, formula_page)) <= 1
    with pdfspine.open() as document:
        for number in range(page_count):
            page = document.new_page(width=240, height=160)
            ...（不变）
            if table_page and number == page_count - 1:
                _draw_table(page, fontname, DEFAULT_TABLE if table_page is True else table_page)
            if diagram_page and number == page_count - 1:
                _draw_diagram(page, fontname)
            if formula_page and number == page_count - 1:
                _draw_formula(page, fontname, rule=formula_rule)
        path.write_bytes(document.tobytes())
    return path
```

> 本方案单独落地时只需要 `diagram_page` / `diagram_caption` 两个形参；上面写的是三份方案合并后的终态，后合入的方案只追加自己的形参与 `if`，不改别人的分支（`DEFAULT_TABLE` / `TableSpec` 来自表格方案，`_draw_formula` 来自公式方案）。

实测该页产物：span `'PLAN' [28,81,52,91]`、`'BUILD' [158,81,188,91]`；SVG 路径 `M20 60L90 60L90 90L20 90Z`（stroke）、`M150 60L…Z`、`M90 75L142 75`（line）、`M142 79L142 71L150 75L142 79Z fill="#000000"`（arrowhead，另有同形 stroke 副本，`arrowhead()` 只认 fill≠none 的那条）；合成 flip 后全部回到上表 top-left 坐标。

`generic_publication_helpers.py`：**不新建独立 sender**（§10 统一），而是在既有 `text_partition_sender(calls, *, table_caption=False, …)` 上追加两个关键字 `diagram_page=False, diagram_caption=False` 与两条 prompt 哨兵分支（`"Return diagram-observations-v1"` / `"Return visual-description-v1"`，与公式方案的哨兵分支并列，layout 分支仍是 `"Source text observations:"`）；`ingest_generic_semantics(..., diagram_page=False)` 透传，`max_live_calls = page_count + 2 × 视觉对象数`（Diagram 页 1 个对象 → `page_count + 2`）。分支内容：
- 布局调用（prompt 含 `"Source text observations:\n"`）：spans 落在 `DIAGRAM_REGION` 内的归 `_region("diagram", "Diagram", list(DIAGRAM_REGION), ids)`，其余归 Text（复用现有 `_extent`/`_region`）；
- `visual-ir-diagram-v1` 调用（prompt 含 `"Return diagram-observations-v1"`）：从 prompt 末行 JSON（`_prompt(prepared)`，`visual_semantics.py:229-253`）读 `observations`，按 `text` 找到 PLAN/BUILD 的 `id`，回 `{"schema_version":"diagram-observations-v1","svg_digest":<抄>,"nodes":[{"node_id":"n1","label":label_variant or "PLAN","bbox":[20,70,90,100],"evidence":{"element_ids":[id_plan],"confidence":"high"}}, {... "n2","BUILD",[150,70,220,100] ...}],"edges":[{"source_node_id":"n1","target_node_id":"n2","label":null,"relationship":"leads to","evidence":{"element_ids":[],"confidence":"high"}}],"confidence":"high","diagnostics":[]}`；
- `visual-description-diagram-v1` 调用：`{"schema_version":"visual-description-v1","svg_digest":<抄>,"text":"Two boxes joined by an arrow.","evidence":{"element_ids":[所有 id],"confidence":"0.5"},"diagnostics":[]}`。
`label_variant="Plan"` 时 `visual_semantics._label_matches` 在模型出口就会拒绝（`invalid_diagram_node`）→ `ir` stage FAILED —— 这测的是模型出口，不是资格校验器；资格校验器的反例要绕过它：用 `label_variant=None` 但让 partition 把首行 `"<label> page 3"` 也塞进 Diagram 区域（`diagram_caption=True`，与 `table_caption` 同构）→ C1 `uncited_source_span` 拒绝。

### 6.2 单元测试

`tests/enterprise_pdf_rag/adapters/test_diagram_geometry.py`（新，手写 SVG 字符串，无 PDF）：
- `test_native_shapes_compose_page_flip_and_skip_glyph_paths`：`<g transform="matrix(1,0,0,-1,0,160)">` 下的 `M20 60L90 60L90 90L20 90Z` → bounds `(20,70,90,100)`；`transform="matrix(12,0,0,12,20,120)"` 的字形 path 被跳过；`<image>`/`<text>` 不 raise。
- `test_rectangle_like_accepts_rounded_rect_and_rejects_background`：圆角矩形（含 `C`）比值 ≥0.85；覆盖整个对象 bbox 的白底被排除。
- `test_arrowhead_tip_and_base`：三角 `(142,79),(142,71),(150,75)` → tip `(150,75)`, base_mid `(142,75)`；共线三点 raise。
- `test_straight_lines_reject_bezier`。
- `test_segment_crosses_uses_shrunken_box`。

`tests/enterprise_pdf_rag/adapters/test_diagram_qualification.py`（新，同样手写 SVG + `TextSpan` + `DiagramIR`；每条规则正反例各一）：
- `test_two_nodes_one_arrow_qualify`（正例；断言 `qualification.edges[0].tip == (150.0, 85.0)`, 描述文本 == `Diagram with 2 nodes and 1 edge: PLAN; BUILD. PLAN -> BUILD.`, `ir.edges[0].verification is VERIFIED`）；
- `test_nodes_only_diagram_qualifies_without_edges`（p6 型：三个圆角矩形、无线；描述以 `No connecting edges.` 结尾）；
- `test_empty_label_node_rejects_whole_object`（诊断 == `node n1: empty_label_without_source_occurrence`）；
- `test_label_must_be_verbatim_span_text`（`Plan` vs span `PLAN` → `label_is_not_verbatim_source_text`）；
- `test_node_bbox_needs_a_native_shape_within_tolerance`（bbox 偏 3pt → `no_native_shape_matches_bbox`；两个重叠形状 → `ambiguous_native_shape`）；
- `test_cited_span_must_lie_inside_node_bbox`；`test_span_cited_twice_rejects`；`test_uncited_span_inside_object_rejects`；
- `test_edge_needs_connector_touching_both_nodes`（线短 5pt → `no_connector_between_nodes`）；
- `test_edge_needs_arrowhead_pointing_to_target`（去掉三角 → `no_arrowhead_pointing_to_target`；三角反向 → 同串）；
- `test_bidirectional_edges_share_line_but_not_arrowheads`；
- `test_connector_crossing_third_node_rejects`；`test_self_loop_and_duplicate_edge_reject`；
- `test_node_id_must_be_path_safe`（`"node a.b"` → `node_id_is_not_path_safe`）；
- `test_qualification_is_deterministic_and_replayable`（同输入两次结果 `==`，序列化再反序列化 `==`）。

`tests/enterprise_pdf_rag/processing/test_diagram_description.py`（新）：阅读序（同行按 x、换行按 y 量化）、en/zh 模板、label 逐字不变。
`tests/enterprise_pdf_rag/processing/test_index_text.py`（增 3 个）：`test_proven_diagram_projects_labels_and_edges`、`test_diagram_without_source_labels_keeps_description`、`test_literal_members_index_their_description_unchanged` 不变。
`tests/enterprise_pdf_rag/processing/test_context_builder.py`（增 2 个，改 1 个）：`test_diagram_block_prints_node_labels_and_edges`（逐行断言 §4.3 示例）、`test_diagram_member_kind_mismatch_is_refused`；改 `test_kind_mismatch_and_unsupported_ir_are_refused` 用 `ImageIR`。
`tests/enterprise_pdf_rag/answers/test_verify.py`（增 3 个；`fake_document.py` 增 `diagram_member()` builder 返回 `RetrievalContext` 含 `DiagramIR` + `DiagramQualification`）：`test_diagram_node_claim_requires_exact_label`（含 `100%` 数字 → `prose_grounded` 放行）、`test_diagram_edge_claim_requires_printed_pair`（`BUILD -> PLAN` 反向 → `CLAIM_NOT_IN_EVIDENCE`）、`test_diagram_claim_kind_path_mismatch_is_invalid_output`（`quote` + `nodes.` → `MODEL_OUTPUT_INVALID`）。
`tests/enterprise_pdf_rag/adapters/test_diagram_publication.py`（新）：replay 通过；篡改 `qualified_description` 字节 → `ValueError("Diagram projection or receipt differs…")`；lineage 少一项 → raise。
`tests/enterprise_pdf_rag/processing/test_retrieval_snapshot.py`：policy 串 v5，snapshot id 期望值随之更新（记录旧值以证明旧快照 id 不变）。

### 6.3 e2e（`tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py` 增 2 个，骨架同 169-232 行的 Table 用例）

`test_generic_pdf_diagram_is_proven_indexed_and_cited_offline`：
1. `ingest, calls = ingest_generic_semantics(tmp_path, monkeypatch, diagram_page=True)`；`assert ingest.failed_stage_count == 0 and len(calls) == 5`（3 布局 + 2 视觉）；
2. `manifest = ProcessingStore(...).load(ingest.processing_id)`；Diagram record 的 `stages` 含 `qualified_ir`/`qualified_description`/`qualification` 全 SUCCEEDED，`qualified_claim_count == 3`；
3. `qualify_draft` → `kinds == {"Diagram": 1, "Text": 3}`，`skipped_reasons == {}`；
4. `index_draft(embedder=_RecordingOfflineEmbedder())` → `member_count == 4`；`"diagram figure PLAN BUILD PLAN -> BUILD"` 是某条嵌入文本的最后一行（v4/v5 header 在前）；
5. `publish_draft`；`plan.qualification_policy == "...-v5"`；`retrieval.search(publication, "PLAN BUILD", limit=4)[0]` 是 Diagram 成员；`resolve` → `isinstance(context.ir, DiagramIR)`、`context.scope == "diagram-structure-source-geometry-v1"`、`context.description.text == "Diagram with 2 nodes and 1 edge: PLAN; BUILD. PLAN -> BUILD."`；
6. `block = build_context_block(context)`；`"edges.0: PLAN -> BUILD" in block.prompt_text()`；
7. 回答链（不调模型，直接 `verify_claims`）：`ModelAnswer(abstain=False, answer="After PLAN comes BUILD.", claims=(ModelClaim("c1", block.member_id, "diagram_edge", "edges.0", "PLAN -> BUILD"),))` → `verified` 1 条，`decide(...) == (ANSWERED, None, None)`；反向 `"BUILD -> PLAN"` → `CLAIM_NOT_IN_EVIDENCE`。
   （若要走完整 `AnswerService`，用 `tests/enterprise_pdf_rag/answers/store_mounted_document.py` + 桩 `complete_text_json` 回同一 JSON；`chat` HTTP 层可用现有 `test_chat*.py` 模式补一条 `kind == "diagram_edge"` 的序列化断言。）

`test_generic_pdf_diagram_with_uncited_caption_is_skipped`：`diagram_page=True, diagram_caption=True` → Diagram record `qualification.state is UNAVAILABLE` 且 `diagnostic == "object: uncited_source_span:<span_id>"` 前缀匹配；`qualify_draft().skipped_reasons == {"Diagram structure is not proven; only geometry-qualified diagrams are retrievable": 1}`；`index_draft().member_count == 3`。

### 6.4 真实样本只读 smoke（`tests/enterprise_pdf_rag/adapters/test_diagram_real_samples.py`，新）

```python
_RUN = ROOT_DIR / "data/output/aia-2026-interim/pages-001-020/runs/00d5c714c8059e9c74da32b030ec56813eb3affe6a8414af85ff78af63ae2076"
_P6 = _RUN / "page-006/objects/object-b7c9e77d6cf0ea933193"
_P5 = _RUN / "page-005/objects/object-0bf4bbbb1af632182d06"
pytestmark = pytest.mark.skipif(not _P6.is_dir() or not _P5.is_dir(), reason="AIA run dump is not present")


def _inputs(folder: Path) -> tuple[bytes, tuple[TextSpan, ...], DiagramIR]:
    svg = (folder / "svg.svg").read_bytes()
    sidecar = TypeAdapter(TextSidecar).validate_json((folder / "source_text.json").read_bytes())
    ir = TypeAdapter(DiagramIR).validate_json((folder / "ir.json").read_bytes())
    return svg, sidecar.spans, ir


def test_p6_three_stage_pathway_qualifies_nodes_only() -> None:
    svg, spans, ir = _inputs(_P6)
    qualified = qualify_diagram(svg=svg, spans=spans, ir=ir, source_manifest_id="x" * 64)
    assert tuple(n.node_id for n in qualified.qualification.nodes) == ("node-foundation", "node-growth", "node-intelligence")
    assert qualified.qualification.edges == ()
    assert qualified.description.text.startswith("Diagram with 3 nodes: Foundation: 100% Digitalised Agency; ")
    assert qualified.description.text.endswith("No connecting edges.")


def test_p5_technology_flow_is_rejected_for_empty_labels() -> None:
    svg, spans, ir = _inputs(_P5)
    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=svg, spans=spans, ir=ir, source_manifest_id="x" * 64)
    assert str(failure.value) == "node node-industry-leading-technology: empty_label_without_source_occurrence"
```

注意 p6 的 `source_text.json` 只含 owned 的 3 个 span（`semantic_objects.py:180-183` 的 sidecar 是过滤过的）；C1 在此 smoke 中用的是这 3 个 span，与 replay 时的整页 sidecar 语义一致（对象 bbox 内的其它 span 若存在会在 replay 时被 C1 抓到——正是 fail closed 想要的）。**只读**：测试不写 `data/`。

---

## 7. 分阶段实施清单与验证命令

| 阶段 | 内容 | 验证 | 人日 |
|---|---|---|---|
| P0 | 夹具：`authored_pdf(diagram_page=)` + `_draw_diagram`；`test_pdf_ingestion.py` 增一条断言（Diagram 页 SVG 无 `<text>`、含 4 条几何 path、2 个 label span） | `.venv/bin/python -m pytest tests/enterprise_pdf_rag/adapters/test_pdf_ingestion.py -q` | 0.5 |
| P1 | `processing/diagram_models.py`、`adapters/diagram_geometry.py`、`adapters/diagram_qualification.py`、`processing/diagram_description.py` + §6.2 前三组单测（TDD：先写 `test_diagram_qualification.py` 正反例） | `.venv/bin/python -m pytest tests/enterprise_pdf_rag/adapters/test_diagram_geometry.py tests/enterprise_pdf_rag/adapters/test_diagram_qualification.py tests/enterprise_pdf_rag/processing/test_diagram_description.py -q`；`.venv/bin/python scripts/enterprise_pdf_rag/check_conformance.py .` | 2.0 |
| P2 | `adapters/diagram_publication.py`（回执 + replay）；`semantic_objects.py` 接线；`processing_retrieval.py`（eligibility / build / _qualified / resolve）、`source_publication.py`、`retrieval.py` 联合；`generic_publication_helpers.text_partition_sender` 的 diagram 哨兵分支（§6.1） | `.venv/bin/python -m pytest tests/enterprise_pdf_rag/adapters/test_diagram_publication.py tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py -q -k diagram`（此时 e2e 只跑到第 3 步：qualify 放行） | 1.5 |
| P3 | policy v5 四步 + `index_text.py` 投影 + `member_text` 门；更新 `test_retrieval_snapshot.py` / e2e 的 policy 断言 | `.venv/bin/python -m pytest tests/enterprise_pdf_rag/processing/test_index_text.py tests/enterprise_pdf_rag/processing/test_retrieval_snapshot.py tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py tests/enterprise_pdf_rag/adapters/test_document_catalog.py -q` | 1.0 |
| P4 | `context_builder.py`、`answers/models.py`、`prompt.py`、`verify.py`；`fake_document.diagram_member`；e2e 第 6–7 步；`check_schema.py` | `.venv/bin/python -m pytest tests/enterprise_pdf_rag/processing/test_context_builder.py tests/enterprise_pdf_rag/answers -q`；`.venv/bin/python scripts/enterprise_pdf_rag/check_schema.py`（按其用法） | 1.5 |
| P5 | 真实样本 smoke（§6.4）；ADR **0015**（与公式方案合写；表格方案的 ADR 0014 独立）；`CLAUDE.md` 不变量段；`CHANGELOG.md`；`processing_export.py` coverage 列（可选） | `.venv/bin/python -m pytest tests/enterprise_pdf_rag -q`（全绿）；`scripts/ci.sh`；`.venv/bin/python scripts/enterprise_pdf_rag/check_drift.py`（若文档带 `verified-against`） | 0.5 |

合计 **≈ 7 人日**（含测试）。每阶段结束跑一次 `.venv/bin/python -m pytest tests/enterprise_pdf_rag -q` 确认无回归；P3 之后全仓 `git grep -n "qualification-v4" tests/ src/` 应只剩兼容集合里的字面量。

---

## 8. 需要拍板的点（≤3，附推荐）

1. **`qualified_description` 用确定性模板（A）还是 VLM 描述 + 校验（B）？** 推荐 **A**：零模型、可重放、每个词要么是 label 逐字要么是模板词；B 的自由散文无法机械对齐到 `edges.<i>`，且改描述 DTO 会作废缓存。原模型描述保留为 `raw_description` lineage，随时可做 B 的离线实验。
2. **nodes-only 图（p6 型，`edges == ()`）是否放行？** 推荐 **放行**，但描述/投影/context block 不产生任何 `edges.` 行，`SYSTEM_RULES` 明令不得推断顺序。理由：节点 label 逐字可证、bbox 有真实形状、覆盖完整；不放行则 AIA 样本里唯一能证明的 Diagram 也进不了索引。
3. **无箭头连线（只有线段、没有填充三角）记不记为无向边？** 推荐 **v1 不记**：模型报 `A→B` 而几何只有一条线 → E3 失败、整对象 fail closed。理由：`DiagramEdge` 没有 `directed` 字段，"无向边"没有承载位置；开口箭头（两条短斜线）、一体成型曲线箭头（p5）都是后续扩展点，先用最严格的"直线 + 填充三角"证据链跑通。若拍板要支持，改动是：`DiagramEdge.directed: bool = True`（带默认，旧资产兼容）+ E3 在无箭头时降级为 `directed=False` + 投影用 ` -- ` 分隔。

---

## 9. 非目标 / 风险

**非目标**：Image / Formula 可检索（各自方案）；Diagram 的图表座位（`select_context`）；分组/泳道；边 `relationship` 文本的任何验证或索引；语言探测（`language` 固定 `"en"`）；修改 partition 让 p5 归属 span。

**风险与应对**：

| 风险 | 影响 | 应对 |
|---|---|---|
| 复杂图（多节点、交叉线、分组框） | N5 `ambiguous_native_shape`、E4 `connector_crosses_node` 触发 → 整对象不放行 | fail closed 是设计；诊断串逐字进 `status.json`，可统计哪条规则最常挡人再放宽 |
| 贝塞尔连线（`C`/`Q`） | `straight_lines` 返回 None → E2 失败 | v1 不支持；扩展点：用 `_polygon`/`_curve` 扁平化后取端点 |
| 无箭头连线 / 开口箭头 / 一体成型箭头（p5） | E3 失败 | §8 拍板点 3；开口箭头可加"两条短斜线共端点、与主线夹角 20°–40°"规则 |
| 文字压在线上 / 边 label | E5 只证 label 逐字，不证它靠近连线 | 可加"label span 中心到连线距离 ≤ 8pt"规则，v1 不做 |
| 多段折线 | `straight_lines` 已按折线处理，E4 对每段检查 | 覆盖 |
| 连线穿过第三个节点 | E4 用内缩 `CONNECT_TOLERANCE` 的框做线段相交 | 覆盖；相切不算穿过 |
| 模型 nodes 漏节点（召回不可证） | 索引/描述缺节点但不会有错的内容 | C1（未引用 span → 拒）是唯一护栏；无文字的节点（纯图标）本来就不可引用 |
| 模型多报节点（无 span 的"节点"） | N2 拒 | 覆盖 |
| `figure_reasoning.prepare_figure` 无文字时 `raise`（211-212）vs `visual_semantics._prepare` 不 raise | 本方案**不复用 `prepare_figure`**（它是图表口径）；校验器直接吃 `svg` stage + 页 span | 无文字 Diagram 在 N2 处按 fail closed 拒绝，与 `_prepare` 允许无文字视觉不冲突（那只影响模型能否被调用） |
| `native_shapes` 对 `clip-path` 视而不见 | 被裁掉的形状可能被当成节点框 | 与 N5"恰一个形状"叠加后误放行概率低；如需严格，复用 `source_paint._native_paths` 的 clip 解析（但要改它对 `<image>` 的 raise） |
| 圆角/椭圆节点、带阴影的双层形状 | 阴影与本体各一条 path → `ambiguous_native_shape` | 可加"取 `path_index` 最大（最上层）"规则，v1 先不加 |
| 字形 path 被误当箭头 | 已按"非单位缩放矩阵跳过"排除；再叠加 E3 的几何链 | 覆盖 |
| policy 升级与另两份方案冲突 | 三方都可能想改 `_POLICY` | §4.2 已拍板：**只有本方案编辑**这四个常量；表格方案零 policy 分支，公式方案复用 `VISUAL_PROJECTION_POLICIES` |
| 缓存：视觉任务名不变，旧 `ir`/`description` 走 `requests/` 缓存；`qualification` 是新 stage | 重跑 `ingest --stage semantics` 不产生新的模型调用 | 符合预期，零成本回填 |

---

## 10. 交叉修订记录

三份方案（`diagram-retrievable.md` / `formula-retrievable.md` / `table-grid-verification.md`）写完后做了一次交叉一致性核对，本节逐条记录**本文件**被改了什么、为什么。总览与合并顺序见同目录 `README.md`。

### 10.1 已修订（逐条）

| # | 位置 | 改了什么 | 为什么 |
|---|---|---|---|
| 1 | §0 不变量表「import 白名单」行 | 补上 `scripts/enterprise_pdf_rag/check_architecture.py:8-27` 的真实规则：`processing/` 只允许标准库 + 四个纯域包，`EXTRA_ALLOWED` 只给 `answers/` 开 pydantic；并写明本方案 `processing/` 的两个新模块全是 dataclass、pydantic 回执只在 `adapters/` | 原文只提了 `check_conformance.py`，漏了真正约束 `processing/` 的那个门；公式方案 §1.5 已按真实规则纠偏过，三份口径需一致。核实结论：**本方案的 `processing/diagram_models.py`、`processing/diagram_description.py` 本来就没有 pydantic，无需改成 dataclass** |
| 2 | §1.1 最后一行（`test_context_builder.py:273-281`） | "必须换成 `ImageIR`/`FormulaIR`" → **只能是 `ImageIR`** | 公式方案让 `build_context_block` 放行 `FormulaIR`，用它当"不支持"反例会在公式方案合入后失效 |
| 3 | §3.1 stage 表下的说明 | "IMAGE / FORMULA 仍写原来的 UNAVAILABLE 诊断" → IMAGE 仍写原诊断；FORMULA 由公式方案接管为它自己的 `_formula` 分支 | 与公式方案 §3.1 直接矛盾（那份把 FORMULA 接到 `self._formula`） |
| 4 | §4.2 policy 注释 | `ADR 0014` → `ADR 0015`，并把注释从"diagram 投影"扩成"Diagram 与 Formula 的 qualified-IR 投影" | ADR 编号统一：**0014 = 表格网格证明（含 ADR 0011 旁注），0015 = Diagram + Formula（合写一份，因为是同一次 policy v5 升级）** |
| 5 | §4.2 / §5 | 门控常量 `DIAGRAM_PROJECTION_POLICIES` → **`VISUAL_PROJECTION_POLICIES`**（定义处、`member_text` 里的引用、§5 改动清单共 3 处） | 公式方案用的名字是 `VISUAL_PROJECTION_POLICIES`，两份若各建一个同值常量就是死重复；统一成一个、由本方案定义、公式方案复用。policy 串取值两份本来就一致（`source-transcription-and-scoped-chart-qualification-v5`），未改 |
| 6 | §4.2 末尾「协调」条 | 明确写成：`_POLICY` / `PROJECTED_CHART_POLICIES` / `CONTEXTUAL_POLICIES` / `VISUAL_PROJECTION_POLICIES` **只由本方案编辑一次**；表格方案零读取侧 policy 分支，公式方案只追加 `member_text` 的一支门 | 原文是"若也要升 policy……各自加投影门"的开放式表述，三份读下来会各自去改常量区 |
| 7 | §4.1 `build` 段末 | 新增「与公式方案叠加」：合并后 kind 白名单 7 项、两处 `required` 条件是 `in (CHART, DIAGRAM, FORMULA)`、kind 特定拒绝串三条并列；表格方案不改 `eligibility` | 三份对同一个函数各写各的条件，需要给出终态 |
| 8 | §4.4 表第 4 行（`_PATH_PREFIX`） | 注明公式方案会把整张表的值改成 tuple，届时本方案两行写成 `("nodes.",)` / `("edges.",)`，`startswith` 语义不变 | 公式方案的 `FORMULA` 需要两个前缀，必须改表结构；本方案原文按"一 kind 一串"写，合并时会冲突 |
| 9 | §4.4 `ClaimCitation` 那条 bullet | "`check_schema.py` 若比对 OpenAPI 快照，需重生成一次" → 确定性表述（`check_schema.py:103,107-119` 全等比对、无 `--write`，必须手工重生成 `rag-chat-v1.json`，脚本见公式方案 §7 阶段 3） | 公式方案已核实该脚本行为；"若"字会让实施者漏掉这一步 |
| 10 | §4.5 末尾 | 新增「与公式方案的叠加」：`semantic_objects.py:217-222` 终态是 FORMULA → `self._formula` / DIAGRAM → 本方案 / 其余 → 原诊断三路 | 同 3 |
| 11 | §5 改动清单 | ①`DIAGRAM_PROJECTION_POLICIES` → `VISUAL_PROJECTION_POLICIES` 并标注"三份里唯一编辑这四个常量的地方"；②新增 `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` 一行；③ADR 文件名 `0014-diagram-structure-qualification.md` → `0015-diagram-and-formula-retrievable.md`（与公式方案合写） | 同 4、5、9 |
| 12 | §6.1 夹具 | `authored_pdf` 写成三份方案的统一终态签名：`table_page: bool \| TableSpec`、`diagram_page`、`diagram_caption`、`formula_page`、`formula_rule`，互斥断言改为 `sum(bool(v) for v in (...)) <= 1`；并把"新建 `diagram_sender`"改为"在既有 `text_partition_sender` 上追加哨兵分支"，`max_live_calls` 口径统一为 `page_count + 2 × 视觉对象数` | 三份方案对同一个测试 helper 给了三种互不兼容的签名/结构（表格把 `table_page` 扩成 `bool \| TableSpec`，公式加 `formula_page`，本方案原本另起一个 sender）；`ingest_generic_semantics` 只 monkeypatch 一个 `_send_once`，多个 sender 无法共存 |
| 13 | §7 P5 行 | ADR 0014 → ADR **0015**（与公式方案合写） | 同 4 |
| 14 | §9 风险表「policy 升级与另两份方案冲突」行 | 改为"已拍板：只有本方案编辑这四个常量" | 同 6 |

### 10.2 核对过、确认无冲突（未改动）

- **policy 串取值**：三份都是 `"source-transcription-and-scoped-chart-qualification-v5"`，一致。
- **`ClaimKind` 新值**：本方案 `diagram_node` / `diagram_edge`，公式方案 `formula`，表格方案不新增 —— 互不重名。
- **`BlockKind` 新值**：本方案 `diagram`，公式方案 `formula`，表格方案不新增 —— 互不重名。
- **`ContextBlock` 新字段**：本方案 `nodes` / `edges`，公式方案 `formula_*` 四个，表格方案 `grid_verification` —— 全部带默认值、纯追加，无重名。
- **`prompt_text()` / `build_context_block` 插入点**：三份分别落在不同的 `elif` / `isinstance` 分支上，elif 链顺序无语义影响。
- **`processing/retrieval.py::RetrievalContext`**：终态联合类型 `LiteralQualification | FigureQualification | DiagramQualification | FormulaQualification`；`.scope`（151-155）**三份都不改**（只有 `FigureQualification` 走 `semantic_scope` 特判，其余都命中末尾的 `return self.qualification.scope`）—— 公式方案原文说"加 isinstance 分支"，已在那份里改成与本方案一致。
- **`member_index_text`**：终态 `ChartIR` → `DiagramIR` → `FormulaIR` 三支 + 兜底，各方案只追加一支。
- **`answers/models.py::ClaimCitation`**：本方案不加字段，表格方案在 `page_title` 之后追加 4 个带默认值的字段，纯追加。
- **`answers/prompt.py`**：`kind` 的 `Literal` 由本方案 +2 值、公式方案 +1 值；表格方案给 `ModelClaim` 加 `row/col/header` 三个可选字段；`SYSTEM_RULES` 规则 1 由三份各追加一句。互不覆盖。
- **`draft_publication.py`**：三份都不改（`DraftQualification.qualification_policy` 保持 `...-v2`，见表格方案 §3.4）。

### 10.3 发现但**未**修改的遗留问题

1. **`verify.py` 里三种文本比对口径并存**：本方案的 `_verify_diagram_node/_edge` 用现有 `_norm`（whitespace-fold + **casefold**，与 QUOTE/CELL 一致），公式方案新增 `_exact`（不 casefold，符号大小写敏感），表格方案的 `header` 比对用裸 `==`。三者各有理由，但同一个文件里会出现三种口径 —— 建议在 ADR 0014 / 0015 各写明本 kind 的口径；本次不擅自统一。注意口径差会让 verify 侧比资格侧宽松（本方案 N2 要求 label 逐字等于 span，而 verify 侧 casefold 后相等即放行）。
2. **`adapters/` 内私有助手的复用风格不一致**：本方案 §2.2 直接从 `donut_geometry` import `_matrix` / `_polygon` / `_compose` 等私有名；公式方案明确选择"复制 `pdfspine_figure._top_left` 等私有函数，不跨模块 import"。两种都过得了门（都在 `adapters/` 下），未统一。实施时建议二选一：要么把 `donut_geometry` 里被复用的几何助手提升为公开名，要么照公式方案复制。
3. **`processing_export.py` 的 coverage 列**：本方案给 DIAGRAM 留了可选的 `diagram_structure_qualified` 列；公式方案与表格方案都没提，合并后 Formula 对象会被计进 `source_transcription_qualified`。属审阅产物口径问题，不影响资格/检索/回答链，未改。
