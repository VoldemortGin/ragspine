> 本文件是 Diagram / Formula / Table 三份方案的**总览原文**（不是 `plans/` 目录的索引），只在顶部加了这行注记。文中 `../notes/…` 指向的调研笔记未随仓库分发；三份方案的最终口径与偏离以 [ADR 0014](../adr/0014-ruled-table-grid-proof.md)、[ADR 0015](../adr/0015-diagram-and-formula-retrievable.md) 与 [交接文档](../CLAUDE_HANDOFF.md) 为准。

# Diagram / Formula / Table 三份可检索方案 —— 总览

本目录三份设计方案都是**只读设计产物**，未改动仓库 `/Users/linhan/startup/spine/ragspine` 任何文件。

| 文件 | 主题 | 人日 | ADR |
|---|---|---|---|
| [`diagram-retrievable.md`](diagram-retrievable.md) | `ObjectKind.DIAGRAM` 可检索 | ≈7 | 0015（与 formula 合写） |
| [`formula-retrievable.md`](formula-retrievable.md) | `ObjectKind.FORMULA` 可检索 | ≈7.25 | 0015（与 diagram 合写） |
| [`table-grid-verification.md`](table-grid-verification.md) | `TableIR.verification` 可为 VERIFIED | ≈8 | 0014（独立） |

三份方案写完后做过一次交叉一致性核对，每份文件末尾的 **§10 交叉修订记录** 逐条列了改动与理由。

---

## 1. 基线：`main = c15525b`，ADR 0013 已在 main 里

- `main` == `origin/main` == **`c15525b`**。`feat/page-metadata` 已 **fast-forward 合并进 main**，`git diff main..feat/page-metadata` 现在是空的。
- 也就是说：**ADR 0013 / policy v4（`source-transcription-and-scoped-chart-qualification-v4`）/ `contextual_index_text` / `AnswerRequest.filters` / `MemberText` 的 5 个新字段 / `ClaimCitation.page_title` 全部已在 `main` 上**。
- 因此调研期一度担心的"与 `feat/page-metadata` 冲突"，实际上是 **"与已合入 main 的 ADR 0013 改动叠加"**：不是分支冲突，是同一文件不同区段的行号漂移 + 语义衔接。
- **建议：三份方案都从 `c15525b` 起新分支，不要从更早的 commit 起，也不要对着旧 `main` rebase。**（`answers/models.py` 的四处改动分布在文件四个位置，是最容易产生行号漂移的文件。）

### 1.1 ADR 0013 改过、且本三份方案也要改的文件与函数

取自 [`../notes/page-metadata-branch.md`](../notes/page-metadata-branch.md) 第 5 节。

| 文件 | ADR 0013 改了什么 | 本三份方案还要改什么 | 叠加风险 |
|---|---|---|---|
| `adapters/processing_retrieval.py` | `_POLICY` v3→v4、`PROJECTED_CHART_POLICIES` 扩集、新增 `CONTEXTUAL_POLICIES`；`member_text()` 加 `context` 形参并重写；`ProcessingRetrieval.publish()` 加 `contexts` 形参；查询方法的 `limit` 校验放宽 | diagram：`_POLICY` 升 v5 + 新增 `VISUAL_PROJECTION_POLICIES` + `member_text` 加 DIAGRAM 门 + `eligibility` / `build` / `_qualified` / `resolve_processing_context`；formula：`member_text` 加 FORMULA 门 + 同样四处；table：**不改** | **高**。常量区与 `member_text` 是 ADR 0013 刚改过的同一段；policy 常量必须单点编辑（见 §3） |
| `processing/index_text.py` | 新增 `PageIndexContext` 类与 `contextual_index_text()`（L8-42 整块） | diagram / formula 各在 `member_index_text`（85-89）加一支 `isinstance` 分支并新增自己的投影函数；table 不改 | 低（新增块在文件头，`member_index_text` 在文件尾） |
| `answers/models.py` | 新增 `MemberFilters`；`AnswerRequest.filters`；**`ClaimCitation.page_title`**；`AnswerResult.filters_applied/filters_relaxed` —— 四处分散 | diagram `ClaimKind` +2 值、formula +1 值（L18-22）；table 在 `ClaimCitation` 的 `page_title` 之后追加 `row/col/header/header_cell_id` | **高**（行号漂移最严重的文件） |
| `adapters/http/chat_schemas.py` | 新增 `MemberFiltersIn/Out`；`ClaimCitationOut.page_title`；`AnswerEnvelope` 两个新字段 | table 在 `ClaimCitationOut`（`:66-76`）照 `page_title` 同法追加 4 个可选字段；diagram / formula 靠枚举类型自动序列化、不改代码 | 中 |
| `adapters/processing_store.py` | `document_metadata` 一致性校验；`load_page_metadata()` / `index_contexts()`；`processing_assets()` 多收一类 stage | **三份都不改**（`load:115-126` 的 `qualified_ir`/`qualified_description` 优先规则天然兼容新 stage；`processing_assets` 收所有 object stage） | 无 |
| `processing/models.py` / `cli.py` / `adapters/answer_service.py` / `hybrid_search.py` / `pdf_ingestion.py` / `page_metadata_extraction.py` | metadata stage、CLI `metadata` 子命令、filter 派生与 starve 兜底、`allowed` 白名单、`_with_page_titles` 等 | **三份都不改** | 无 |
| `answers/verify.py`、`processing/context_builder.py`、`adapters/semantic_objects.py` | **ADR 0013 一行都没碰** | 三份方案的主战场（`_PATH_PREFIX`/`_BLOCK_KINDS`/分发、`BlockKind`/`prompt_text`/`build_context_block`、`:217-222` 与 `_table`） | 与 ADR 0013 **无文本冲突**；冲突只在三份方案彼此之间（见 §3 矩阵） |

---

## 2. 三份方案一句话摘要 + §8 拍板点

### 2.1 `diagram-retrievable.md`

> 对模型产出的 `DiagramIR` 做**无模型、可重放**的 SVG 几何 + 逐字 span 校验（node label 必须逐字来自 span 且 span 落在 node bbox 内、node bbox 必须对上一个真实的矩形类填充/描边路径、边必须有"连线 + 填充三角尖端"的几何链、对象 bbox 内每个 span 都必须被引用），整体通过才产 `qualified_ir` / `qualified_description` / `qualification` 三个 stage，进 `eligibility`、policy v5 索引投影、context block 与 `nodes.<id>.label` / `edges.<index>` 两条可引用路径；任一规则失败整对象 fail closed。

**§8 拍板点（照抄）**：

1. **`qualified_description` 用确定性模板（A）还是 VLM 描述 + 校验（B）？** 推荐 **A**：零模型、可重放、每个词要么是 label 逐字要么是模板词；B 的自由散文无法机械对齐到 `edges.<i>`，且改描述 DTO 会作废缓存。原模型描述保留为 `raw_description` lineage，随时可做 B 的离线实验。
2. **nodes-only 图（p6 型，`edges == ()`）是否放行？** 推荐 **放行**，但描述/投影/context block 不产生任何 `edges.` 行，`SYSTEM_RULES` 明令不得推断顺序。理由：节点 label 逐字可证、bbox 有真实形状、覆盖完整；不放行则 AIA 样本里唯一能证明的 Diagram 也进不了索引。
3. **无箭头连线（只有线段、没有填充三角）记不记为无向边？** 推荐 **v1 不记**：模型报 `A→B` 而几何只有一条线 → E3 失败、整对象 fail closed。理由：`DiagramEdge` 没有 `directed` 字段，"无向边"没有承载位置。

### 2.2 `formula-retrievable.md`

> **完全不调模型**：把 partition 判为 `Formula` 的对象拆成逐字来自 span 子串的 token 序列（tiling 闭合规则保证拼接 == 去空白的 span 全文），分数线/根号来自 `get_cdrawings()` 的真实路径，上下标用 `text_matrix` 与 `origin` 之差（= PDF `Ts`）逐字证明、证不了的排版式脚本标 `derived`；全部成立才产同样三个 stage，并开放 `formula.linear` / `formula.readable` / `tokens.<i>` 三条可引用路径。VLM 两路只进 lineage 与信息性一致性记录。

**§8 拍板点（照抄）**：

1. **derived 上下标是否放行？** —— **推荐放行为 `literal` 级**（`proof_level="literal"`，`qualified_ir.verification=PENDING`，token 带 `script_proof="derived"` + 三项数值，`readable` 用"上标"而非"次方"）。理由：(a) pdfTeX / InDesign / Word 导出的 PDF 几乎都用 `Tm/Td` 移基线而非 `Ts`，只认 `text_rise` 会让功能在真实文档上死掉；(b) 逐字不变量仍成立；(c) 与 `TableIR` 恒 PENDING 但成员可检索的既有判例同构。
2. **`linear` 用 LaTeX 子集还是自定义线性语法？** —— **推荐 LaTeX 子集**（`\frac{}{}`、`\sqrt{}`、`^{}`、`_{}`），符号一律保留 Unicode 原文（不做 `\alpha`/`\times` 映射，否则"映射"本身成了非逐字改写）。
3. **token 是否允许引用 span 子串？** —— **推荐允许**，以 `check_tiling` 闭合规则为硬约束（有序、不重叠、拼接 == 去空白 span 全文、跳过的只能是空白），并要求 `rawdict` 字符数 == 文本长度以给子串精确 bbox。理由：pdfspine 会把 `"ROE ="` 合成一个 span，不允许子串则几乎所有公式都会 fail closed。

### 2.3 `table-grid-verification.md`

> ADR 0011 把 "VERIFIED `TableIR`" 列为 Rejected alternative，理由是"a new qualification with no source rule"。本方案补这条 source rule：网格的每条行/列边界、每个 cell 的四边、每个合并格的"缺失内线"都必须在 `page.get_drawings()` 的真实 ruling 线段里找到逐段证据（0.5pt 匹配容差，远小于 pdfspine 的 `snap_tolerance=3.0`），通过才 `TableIR.verification = VERIFIED`；无线表 / 外框有内线无 / 吸附边界 / 双线边框 / 虚线全部 fail closed。VERIFIED 之后才开放 `row/col/header` 引用。

**§8 拍板点（照抄）**：

1. **表头证据等级是否影响 `VERIFIED`？** 推荐：**不影响**。VERIFIED 只陈述"行/列/合并结构来自真实线"；表头是另一个事实，用 `HeaderEvidence.strength` 单独分级——`proved`（`ruling_thick` / `fill`）才可被 `header=` 引用，`heuristic`（`font_bold` / `first_row_rule`）只进审阅产物。
2. **部分划线表（外框有内线无、或部分内线缺失但 pdfspine 仍给出网格）是否 PENDING？** 推荐：**PENDING**。实测外框-only 在检测阶段就是 0 张表；"部分内线"正是规则 3（边连续覆盖）与规则 4（合并处无内线）要拒的对象——放行就等于承认 pdfspine 的推断为证据。
3. **`row/col/header` 引用是否只对 VERIFIED 表开放？** 推荐：**是**；纯文本 `cells.<id>` 引用对 PENDING 表保持现状。`eligibility()` 不改、policy 不改。

---

## 3. 依赖顺序、合并顺序与"文件 × 方案"矩阵

### 3.1 推荐顺序

```
c15525b (main)
  ├── feat/table-grid-proof      ← 表格方案：核心文件全部独占，可先行，也可与右侧并行
  └── feat/diagram-retrievable   ← diagram 方案：升 policy v5、开 BlockKind/ClaimKind/verify 座位
        └── feat/formula-retrievable  ← formula 方案：必须在 diagram 之后，复用它打开的所有座位
```

- **表格方案独立**：它不碰 `processing_retrieval.py` 的任何 policy 常量、不碰 `eligibility`、不碰 `member_index_text`、不加枚举值。与另两份的交集只有 `context_builder.py` / `prompt.py` / `answers/models.py` / `verify.py` 四个消费侧文件，而三份在这四个文件里都是**追加带默认值的字段 / 追加 `elif` 分支**，任何顺序都只产生行号漂移，不产生语义冲突。→ **可先行，也可并行。**
- **diagram 必须先于 formula**：formula 复用 diagram 打开的四个座位 —— ① `BlockKind` / `ClaimKind` 的扩展方式；② `verify_claims` 的分发结构；③ `member_index_text` 的多分支形态；④ **policy v5 的四个常量**（`_POLICY` / `PROJECTED_CHART_POLICIES` / `CONTEXTUAL_POLICIES` / `VISUAL_PROJECTION_POLICIES`）。
- **policy v5 只升一次，在 diagram 分支**。串值三份一致：`"source-transcription-and-scoped-chart-qualification-v5"`。formula 只在 `member_text` 的 `else` 里追加一支门并复用 `VISUAL_PROJECTION_POLICIES`；table 零读取侧 policy 分支。
- **ADR 编号已固定**：**0014 = 表格网格来源证明**（含 ADR 0011 Rejected alternative 的旁注 "Superseded for ruled tables by ADR 0014"）；**0015 = Diagram + Formula 可检索**（合写一份，因为是同一次 policy v5 升级）。**不存在 ADR 0016。**
- `DraftQualification.qualification_policy`（`draft_publication.py:25-27`）**三份都不升 v3**，保持 `retrieval-eligibility-kind-and-stage-completeness-v2`。

### 3.2 文件 × 方案矩阵

**共享文件**（✅ = 该方案要改；同一格里写明改哪一部分）

| 文件 / 位置 | diagram | formula | table |
|---|---|---|---|
| `adapters/processing_retrieval.py` 常量区（`_POLICY` / `PROJECTED_CHART_POLICIES` / `CONTEXTUAL_POLICIES` / `VISUAL_PROJECTION_POLICIES`） | ✅ **唯一编辑者** | 复用，不编辑 | — |
| `adapters/processing_retrieval.py::member_text` | ✅ +DIAGRAM 投影门 | ✅ +FORMULA 投影门 | — |
| `adapters/processing_retrieval.py::eligibility` / `build` | ✅ kind 白名单 + `required` + 拒绝串 + lineage | ✅ 同左（终态 `in (CHART, DIAGRAM, FORMULA)`） | — |
| `adapters/processing_retrieval.py::_qualified` / `resolve_processing_context` | ✅ +DIAGRAM 分支 | ✅ +FORMULA 分支 | — |
| `adapters/source_publication.py:79-83` | ✅ | ✅ | — |
| `processing/retrieval.py::RetrievalContext.qualification` | ✅ 联合类型 +`DiagramQualification` | ✅ +`FormulaQualification` | — （`.scope` 三份都不改） |
| `processing/index_text.py::member_index_text` | ✅ +`DiagramIR` 分支 | ✅ +`FormulaIR` 分支 | — |
| `processing/context_builder.py` | ✅ `BlockKind.DIAGRAM`、`_KIND_OF_OBJECT`、`nodes`/`edges` 字段、`prompt_text` 分支、`build_context_block` 分支 | ✅ `BlockKind.FORMULA`、`formula_*` 四字段、同样两处分支 | ✅ `HeaderRef`、`CellEvidence` 两字段、`ContextBlock.grid_verification`、改 TABLE 渲染与 TableIR 分支 |
| `answers/models.py::ClaimKind` | ✅ `diagram_node` / `diagram_edge` | ✅ `formula` | — |
| `answers/models.py::ClaimCitation` | —（复用 `evidence_ids`/`bbox`/`quote`） | — | ✅ +`row`/`col`/`header`/`header_cell_id` |
| `answers/prompt.py::ModelClaim` | ✅ `kind` Literal +2 | ✅ `kind` Literal +1 | ✅ +`row`/`col`/`header` 可选字段 |
| `answers/prompt.py::SYSTEM_RULES` 规则 1 | ✅ 追加一句 | ✅ 追加一句 | ✅ 追加一句 |
| `answers/verify.py::_PATH_PREFIX` / `_BLOCK_KINDS` / `verify_claims` 分发 | ✅ +2 kind | ✅ +1 kind，且**把 `_PATH_PREFIX` 的值改成 tuple**（FORMULA 要两个前缀），届时把 diagram 的两行一并转 tuple | —（只在 `:320` 前加两行"非 CELL claim 带 row/col/header → `MODEL_OUTPUT_INVALID`"） |
| `answers/verify.py::_verify_cell` | — | — | ✅ 行列 / 表头校验 |
| `adapters/semantic_objects.py:217-222` | ✅ DIAGRAM 分支 | ✅ FORMULA → `self._formula` | — |
| `adapters/semantic_objects.py::_table` | — | — | ✅ 回执两字段 + diagnostic |
| `processing/typed_ir.py` | — | ✅ `FormulaIR`（`:310-318`）加 5 字段 | ✅ `LiteralQualification`（`:121-132`）加 2 字段 |
| `adapters/http/chat_schemas.py` | —（枚举自动） | —（枚举自动） | ✅ `ClaimCitationOut` +4 字段 |
| `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` | ✅ 两个 enum | ✅ 两个 enum | ✅ citation 属性 |
| `tests/.../test_pdf_ingestion.py::authored_pdf` | ✅ `diagram_page` / `diagram_caption` | ✅ `formula_page` / `formula_rule` | ✅ `table_page: bool \| TableSpec` |
| `tests/.../generic_publication_helpers.py::text_partition_sender` | ✅ diagram 哨兵分支 | ✅ formula 哨兵分支 | ✅ `table_bbox` / `table_region()` |
| `tests/.../test_context_builder.py::test_kind_mismatch_and_unsupported_ir_are_refused` | ✅ 反例 `DiagramIR` → **`ImageIR`** | 不得改回 `FormulaIR` | — |
| `docs/enterprise-pdf-rag/adr/` | ADR 0015（合写） | ADR 0015（合写） | ADR 0014 + ADR 0011 旁注 |
| `CLAUDE_HANDOFF.md` / `CHANGELOG.md` / `src/enterprise_pdf_rag/CLAUDE.md` | ✅ | ✅ | ✅（另加 `testing-and-ingestion.md`） |

**各方案独占文件**

| 方案 | 独占新增 / 修改 |
|---|---|
| diagram | `processing/diagram_models.py`、`processing/diagram_description.py`、`adapters/diagram_geometry.py`、`adapters/diagram_qualification.py`、`adapters/diagram_publication.py`、`adapters/processing_export.py`（可选 coverage 列）；测试 `test_diagram_geometry.py`、`test_diagram_qualification.py`、`test_diagram_description.py`、`test_diagram_publication.py`、`test_diagram_real_samples.py` |
| formula | `processing/formula_models.py`、`processing/formula_rules.py`、`adapters/pdfspine_formula.py`、`adapters/formula_qualification.py`、`scripts/enterprise_pdf_rag/formula_smoke.py`；测试 `test_formula_rules.py`、`test_pdfspine_formula.py`、`test_formula_qualification.py`、`test_formula_publication_e2e.py`、`test_formula_aia_smoke.py`、`formula_fixture.py`（reportlab） |
| table | `processing/geometry.py`（扩展）、`processing/table_models.py`、`processing/table_grid_proof.py`、`adapters/pdfspine_tables.py`、`adapters/literal_qualification.py`（TABLE 分支）；测试 `test_table_grid_proof.py`、`test_table_grid.py`（拆一条冻结测试）、`test_geometry.py`、`test_pdfspine_tables.py` |

---

## 4. 工作量汇总与建议实施顺序

| 方案 | 阶段拆分 | 人日 |
|---|---|---|
| diagram | P0 夹具 0.5 / P1 纯规则 + 几何 2.0 / P2 回执 + 接线 1.5 / P3 policy v5 + 投影 1.0 / P4 消费侧 1.5 / P5 文档 + smoke 0.5 | **7.0** |
| formula | 阶段 0 探针 0.25 / 1 纯规则 2.0 / 2 adapter 2.0 / 3 消费侧 + policy 1.5 / 4 e2e 1.0 / 5 文档 0.5 | **7.25**（diagram 先合入可省约 0.5 → **6.75**） |
| table | P1 纯规则 2.0 / P2 producer + 夹具 1.5 / P3 validator 1.0 / P4 消费侧 1.5 / P5 文档 + 全门 1.0 / 缓冲 1.0 | **8.0** |
| 合计 | | **≈22.25 人日**（diagram 先行后 **≈21.75**） |

- **单人串行**：≈21.75 人日 ≈ **4.4 周**（按 5 人日/周）。顺序 `table → diagram → formula` 或 `diagram → formula → table` 都行，但 diagram 必须在 formula 之前。
- **两人并行**（线程 A = 表格；线程 B = diagram → formula）：墙钟 = max(8.0, 7.0 + 6.75) = **13.75 人日 ≈ 2.8 周**。瓶颈在线程 B。
- **三人并行没有意义**：formula 强依赖 diagram，拆不开。若要再压缩，只能把 diagram 的 P4（消费侧）与 formula 的阶段 1（纯规则，零共享文件）重叠，可再省约 1 人日。

### 建议实施顺序（含每步验证命令，全部从仓库根运行）

> `PY=.venv/bin/python`。基线全量期望：`$PY -m pytest tests/ -q` → **1295 passed, 40 skipped**。

| 步 | 动作 | 验证命令 |
|---|---|---|
| 0 | 从 `c15525b` 起分支（`feat/table-grid-proof`、`feat/diagram-retrievable`） | `git log -1 --oneline` 应是 `c15525b`；`$PY -m pytest tests/enterprise_pdf_rag -q` 基线全绿 |
| 1 | **表格 P1–P2**（纯规则 + producer + 夹具） | `$PY -m pytest tests/enterprise_pdf_rag/processing/test_geometry.py tests/enterprise_pdf_rag/processing/test_table_grid.py tests/enterprise_pdf_rag/processing/test_table_grid_proof.py tests/enterprise_pdf_rag/adapters/test_pdfspine_tables.py tests/enterprise_pdf_rag/adapters/test_pdf_ingestion.py -q`；`$PY scripts/enterprise_pdf_rag/check_architecture.py` |
| 2 | **diagram P0–P2**（夹具 + 几何 + 资格 + 回执 + 接线） | `$PY -m pytest tests/enterprise_pdf_rag/adapters/test_diagram_geometry.py tests/enterprise_pdf_rag/adapters/test_diagram_qualification.py tests/enterprise_pdf_rag/adapters/test_diagram_publication.py -q`；`$PY scripts/enterprise_pdf_rag/check_conformance.py .` |
| 3 | **diagram P3**（policy v5 四步 —— 全项目唯一一次） | `$PY -m pytest tests/enterprise_pdf_rag/processing/test_index_text.py tests/enterprise_pdf_rag/processing/test_retrieval_snapshot.py tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py tests/enterprise_pdf_rag/adapters/test_document_catalog.py -q`；`git grep -n "qualification-v4" src/ tests/` 应只剩兼容集合里的字面量 |
| 4 | **diagram P4–P5**（消费侧 + ADR 0015 草稿 + 真实样本 smoke） | `$PY -m pytest tests/enterprise_pdf_rag/processing/test_context_builder.py tests/enterprise_pdf_rag/answers -q`；`$PY -m pytest tests/enterprise_pdf_rag -q` |
| 5 | **表格 P3–P5**（validator + 消费侧 + ADR 0014 + smoke）——可与步 2–4 并行 | `$PY -m pytest tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py tests/enterprise_pdf_rag/adapters/test_draft_publication.py tests/enterprise_pdf_rag/answers tests/enterprise_pdf_rag/adapters/test_chat_http.py -q -s -k "not gpu"` |
| 6 | **formula 阶段 0–2**（探针 + 纯规则 + adapter） | `$PY -m pytest tests/enterprise_pdf_rag/processing/test_formula_rules.py tests/enterprise_pdf_rag/adapters/test_pdfspine_formula.py tests/enterprise_pdf_rag/adapters/test_formula_qualification.py -q`；`$PY scripts/enterprise_pdf_rag/check_architecture.py`；`$PY scripts/enterprise_pdf_rag/check_conformance.py .` |
| 7 | **formula 阶段 3–5**（消费侧 + e2e + ADR 0015 合写定稿） | `$PY -m pytest tests/enterprise_pdf_rag/adapters/test_formula_publication_e2e.py tests/enterprise_pdf_rag/answers -q` |
| 8 | **三份合并后一次性收尾**：重生成 `rag-chat-v1.json`（只跑一次）、`CHANGELOG` / handoff / frontmatter | `$PY scripts/enterprise_pdf_rag/check_schema.py`；`$PY scripts/enterprise_pdf_rag/check_architecture.py`；`$PY scripts/enterprise_pdf_rag/check_conformance.py .`；`$PY -m pytest tests/enterprise_pdf_rag -q`；`$PY -m pytest tests/ -q`（= 1295 + 新增用例数 passed）；`make drift`；`make lint`；**`scripts/ci.sh`**（最终门） |

---

## 5. 共同风险与未决项

| # | 风险 / 未决项 | 说明 | 现状 |
|---|---|---|---|
| 1 | **pdfspine 版本钉 0.11.0** | 三份方案里的坐标系（`get_drawings()` top-left vs `get_cdrawings()` bottom-left）、`Table.rows/cols` 语义、span 的 `text_matrix`/`ctm`/`chars`、drawing dict 的 9 个键、`filled_rectangles()` 的坐标系 —— 全部是 0.11.0 的实测结论。升级会让三处 replay 的逐字节 / 逐值比对直接拒绝挂载（**不会静默错读**），但需要重跑 `semantics → metadata → index → publish` | 未决：建议在 `pyproject.toml` 把 pdfspine 钉死到 0.11.0 并在 ADR 0014 / 0015 里写明升级流程 |
| 2 | **`strategy="lines"` 的检测质量** | FinTabNet.c 150 页实测召回 21.5% / 精确率 20.6%（`src/ragspine/extraction/tables/structure.py:7-8`）。表格方案只对"检测到且全划线"的表做证明，**不改检测**；检测不到的表根本进不了证明阶段 | 已知放宽，写进 ADR 0014；改检测是独立课题 |
| 3 | **真实样本里 Formula 为零** | AIA 1–20 页 `layout.json` / `layout.raw.json` 都是 0 个 `"kind":"Formula"`，全 71 页 `text.json` 里 `=`、希腊字母、上标数字、`×÷√≈` 命中数全为 0。公式方案只能靠合成夹具（pdfspine 版 derived 上标 + reportlab 版真 `Ts`）验证 | 未决：公式方案的真实收益无法在现有样本上证明；§6.4 只能给"若出现就跑"的 smoke 骨架 |
| 4 | **Diagram 在真实样本上的收益只有 1 个对象** | AIA 1–20 页 2 个 Diagram：p6（三阶段堆叠图）可证但是 nodes-only；p5 因 partition 没把 6 个 span 归属给对象、5 个 node label 全空，必然在 N2 处被拒 | 已知；p5 的正确修法在 partition，不在资格校验器 |
| 5 | **IMAGE 仍不可检索** | AIA 1–20 页的 7 个 Image 全是 logo / 图标，三份方案都不覆盖 IMAGE，`semantic_objects.py:217-222` 的原诊断对它保持不变 | 明确非目标 |
| 6 | **policy v5 让所有已发布快照需要重建** | `_POLICY` 进 `snapshot_id`；旧快照仍可 `load`（永不按 policy 拒收），但要吃到新能力必须 `index + publish`（Diagram/Formula 还要重跑 `semantics` 才有 `qualified_*`）。`data/ingestion/` 两个 v2 快照的重建**没有归属** | 未决 |
| 7 | **`rag-chat-v1.json` 是公开契约，三份都改** | 两个 enum 各 +3 值 + `ClaimCitationOut` +4 字段；`check_schema.py` 全等比对且**没有 `--write`**，必须手工重生成，且三份合并后只跑一次 | 已在三份 §5 / §10 登记 |
| 8 | **`SYSTEM_RULES` 三份各追加一句** | `request_fingerprint` 会变 → 已有回答缓存全部 miss | 预期行为，写进 handoff |
| 9 | **`verify.py` 里三种文本比对口径并存** | diagram 用 `_norm`（含 casefold）、formula 新增 `_exact`（不 casefold）、table 的 `header` 用裸 `==`。各有理由但同文件三种口径；且 diagram 的 verify 侧比它自己的资格侧宽松 | **未统一**，三份 §10.3 都登记了，建议在 ADR 里各写明口径 |
| 10 | **回答路径的重证成本叠加** | 表格方案让每次 `resolve` 对 VERIFIED 表重开 PDF + `get_drawings()`；diagram / formula 的 replay 各自重算 crop / 重新观测 PDF。三者叠加后的单次 resolve I/O 未做整体评估 | 未决；表格方案 §9.2 给了"`build` 全证、`resolve` 只比 digest"的缓解方案 |
| 11 | **`adapters/` 内私有助手的复用风格不一致** | diagram 直接 import `donut_geometry._matrix/_polygon/_compose`；formula 明确选择复制 `pdfspine_figure._top_left` 等而不跨模块 import 私有名 | 未统一；建议实施时二选一 |

---

## 6. 调研笔记索引（`../notes/`）

| 文件 | 行数 | 一句话 |
|---|---|---|
| [`pdfspine-capabilities.md`](../notes/pdfspine-capabilities.md) | 1088 | pdfspine 0.11.0 的逐项实测：span 的 17 个键（`text_matrix` 与 `origin` 之差 == PDF `Ts`）、`flags` superscript 位不可靠且无 subscript 位、`get_drawings()` top-left vs `get_cdrawings()` bottom-left、`find_tables(clip=)` 在 lines 策略下被静默丢弃、`Table` 有 `rows/cols/spans/slots` 但**没有** lines/edges、SVG 输出与作图 API 的真实签名 |
| [`real-samples.md`](../notes/real-samples.md) | 992 | AIA 1–20 页实况：2 个 Diagram（p6 三阶段图无箭头可证、p5 人工补的流程图 5 个 label 全空）、7 个 Image 全是 logo/图标、**0 个 Table**、**0 个 Formula**，9 个视觉对象的 qualification 全是策略性 `unavailable`；以及 `data/ingestion/` 两个合成表快照（TableIR 全 PENDING、无 ruling 字段、线只在 `svg.svg` 里）与当前 policy v4 / v2 的分布 |
| [`codebase-facts-objects.md`](../notes/codebase-facts-objects.md) | 2155 | 视觉对象链的函数级事实：`ObjectKind` → `typed_ir` → `semantic_objects` 分发 → `visual_semantics` 的 Diagram nodes/edges 映射（证据绑定已很严格、verification 恒 PENDING、`description_index_eligible` 是空挂钩）→ `eligibility` 双闸 → `index_text` → `context_builder`（Diagram/Formula IR 直接 raise）→ `verify` / `answers/models` / `prompt.py` 六处联动，以及 policy 升级四步模板与离线夹具能力 |
| [`codebase-facts-tables.md`](../notes/codebase-facts-tables.md) | 1528 | 表格链的函数级事实：`table_models` 的 `__post_init__` 无条件钉 PENDING、`table_transcription` 只验文字、各处容差口径（1e-6 vs 0.5）、`pdfspine_tables` 的检测与映射、`eligibility`/`context_builder`/`verify` 对 TABLE 的处理，以及 **ADR 0011 明确把 "VERIFIED TableIR（无 source rule）" 列为 Rejected** 的原文 |
| [`page-metadata-branch.md`](../notes/page-metadata-branch.md) | 428 | ADR 0013（页级自动 metadata + contextual index text + period/region 预过滤）改了哪些文件哪些函数的逐条摘录、policy v4 的定义与旧快照兼容机制，以及第 5 节的"与后续 Diagram/Formula/Table 方案潜在冲突的文件清单"（本 README §1.1 的来源） |

另有 [`WRITER-BRIEF.md`](../notes/WRITER-BRIEF.md)：三份方案写作时的共同简报（环境、基线、不变量逐条、固定章节 0–9、风格范本）。
