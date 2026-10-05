# ADR 0028: 无图文本页的确定性版面切分

Status: Accepted, 2026-10-05, as an **explicit choice only**: no library preset selects it
(both `ingest_mode` presets keep `IngestPlan.layout="model"`), because it has been measured on
a 20-page slide deck and **not on a long financial report**. The run-folder notebook selects it
by default (`LAYOUT_POLICY = "deterministic-text-pages"`, see Integration below). Amends the layout stage of
[ADR 0005](0005-first-twenty-pages-processing.md)（选中页处理：每页一次模型版面）and
[ADR 0010](0010-generic-pdf-ingestion-entry.md)（通用入库入口与调用预算）; follows the
principle of [ADR 0013](0013-page-metadata-and-prefilters.md) Amendment 1（读不懂的版面必须
零代价，而不是去猜）。It changes **no schema, no fingerprint of the model layout, no default
behavior**: `layout_policy="model"`（两个库预设）逐字节保持既有产物与调用。

## Context

用户在 Databricks 上对一个文件夹的加密长篇财报（单份几百页，文字与表格为主、含若干图表）
入库并答题，LLM 按次计费且慢。现有流程每页一次带页面 PNG 的 `page-layout-v2` 模型调用，是
除页元数据以外最大的调用项（300 页 ≈ 300 次）。而对没有任何图形 / 图片的纯文本页，pdfspine
的 block → line → span 几何（bbox、字号、字体）已足以产出与模型版面同 schema 的对象划分；
只有 Table / Chart / Image / Diagram 这类版面真正需要模型（chart IR 仍由模型生成，不变）。

在唯一的真实样本（AIA 业绩演示稿前 20 页）上的只读调查：473 个 pdfspine block 中 460 个
（97.3%）完全落在单个模型对象内（block 比模型对象细）；图形聚类框能覆盖 36/38 个模型视觉
对象，但 78 个聚类框里 41 个不是视觉对象 → **图形只能当回退触发器，不能当分类器**。

## Decision

新增一个实现 `PagePartitioner` 端口的组合切分器（`adapters/deterministic_partition.py`，
纯几何在 `adapters/deterministic_partition_geometry.py`），由
`IngestPlan.layout`（ADR 0025 的 `LayoutPolicy`，经 `make_partitioner` 这一处选择）决定，
`ingest_pdf(..., layout_policy=...)` / `run_folder_pipeline(..., layout_policy=...)` 可覆盖
模式预设；值为 `"model"`（两个预设，完全不变）或 `"deterministic-text-pages"`：

1. **页面分诊（拿不准就回退，不猜）**。对每个选中页判定"可确定性处理"或"回退模型版面"，
   原因码机器可读：`no_text_layer` / `rotated_text` / `span_outside_page` /
   `page_geometry_mismatch` / `has_image` / `residual_graphics` / `ambiguous_columns` /
   `partition_invalid`（产物没过 `validate_partition` 的兜底，回退而非报错）。
   跨页特征整份文档只扫一遍（经 `shared_pdfs()` 复用同一打开的文档，FUSE 上无额外重开）：
   跨页重复行键（同文本同 5pt 高度桶、≥30% 且 ≥2 页）、跨页重复绘制键（同形同位）、跨页
   重复小图片键。页内图形逐一豁免：表格区域内的绘制、细线类（≤3pt，与
   `pdfspine_tables.LINE_MAX_THICKNESS` 同值）、跨页重复装饰、整页背景填充、面积 ≤5% 的
   跨页重复图片（logo）——跨页重复豁免**只给区域内不含任何文字 span（按中心点）的图形**，
   带字的重复色块 / logo 一律回退（真实图表几乎总带坐标轴或数据标签文字）；**任何剩余图形
   或图片一律回退**。
2. **确定性切分**（保守版）。span 按竖直重叠 ≥ 较矮者一半聚行；顶 / 底 6% 带内的跨页重复行
   与页码单独成 Text 对象（不丢弃）；`find_tables(strategy="lines")` 命中、≥2 行 ≥2 列、
   bbox 内每个 span 都落在原生单元格且每个 present 单元格都有 span 时才出 Table 对象
   （bbox 即表格框，归属判据与 `PdfspineTableAdapter` 的 center-inside 逐字一致，网格证明
   原样复验）；其余文本按"标题（字号 ≥ 正文众数 1.15 倍）开启新对象并吞并其后段落"分块，
   连续 ≥2 个项目符号 / 编号行成 List（分项即行组，编号列表 `list_ordered=True`），单个
   项目符号并回 Text。栏式：无贯穿栏沟按行读；一条栏沟且跨沟行 ≥80%（行对齐的标签 / 数值
   版面，至少一侧行段中位 ≤24 字符）按行读；跨沟行 ≤20% 的真双栏先左后右；其余
   `ambiguous_columns` 回退——基线恰好对齐的双栏叙事明确回退，不交错。疑似无框线表格的
   区域只出 Text（按行），**绝不猜 Table**（"识别不出网格的表格按行收录"归并行分支
   `feat/unverified-table-rows`）。
3. **产物与缓存**。确定性产物 producer 为
   `page-layout-deterministic-v1:pdfspine/<version>`，与模型产物
   （`page-layout-mapper-v3:…`）天然区分；组合切分器的 stage 指纹同时含两个 producer，两种
   策略的 stage 缓存互不覆盖、同一 store 内共存。回退页的模型请求与默认策略逐字节相同，
   因此已用一种策略入库过的文档切换策略后**零新增 live call**（模型缓存命中），发布指针
   一如既往以最近一次成功发布为准。回退页的模型 partition diagnostics 追加
   `deterministic-partition: model fallback (<reason>)`，`IngestionSummary` 新增
   `pages_partitioned_deterministically` / `pages_partition_model_fallback` /
   `partition_fallback_reasons`（从已存产物重导出，缓存重放时数字不变）。

## 保证强度

- 反捏造 / 溯源不变：对象文字全部是 span 逐字内容（本模块只分组，从不改写）；每个 span 有
  归属（`unassigned_span_ids` 恒空）；产物过 `validate_partition`；诊断只有原因码与计数。
- Table 的数值资格仍完全由 ADR 0014 的网格证明与逐字转录决定；本 ADR 只决定"哪个区域作为
  Table 对象提交给它"。
- 确定性切分的**对象边界**是启发式（阈值集中为有名常量，依据注释在代码里），没有模型兜底；
  边界不同只影响检索粒度，不影响证据链正确性。

## 质量对照（2026-10-05，AIA 业绩演示稿前 20 页，对照已存模型版面）

- 判定分布：`ok` 3、`has_image` 3、`residual_graphics` 13、`ambiguous_columns` 1。
- 以"模型版面含 Chart / Image / Diagram 的页"为应回退参照（15 页）：14 页回退；唯一判
  `ok` 的 p2，其模型视觉对象是**跨页重复的矢量 AIA logo（0 个 span，"AIA logo graphic in
  the upper-right corner"）**，按装饰豁免属设计内：无文本损失，IMAGE 对象本身不可检索。
  即逐例解释后的漏判率为 0。
- 3 个 `ok` 页上：确定性对象 4 个 vs 模型 12 个（slide 版式下更粗），span 归属纯度 3/4
  （1 个对象并了模型拆开的两段）。
- 10 页混合合成文档（9 文本页 + 1 矢量柱状图页，脚本化模型）：版面调用 1 vs 10；总调用
  11 vs 20（页元数据每页一次不变）；对象 46 vs 19（对照的是单区域脚本桩，真实模型见上）。
- 外推（**假设** 300 页、20% 含图页、其余全部可确定性处理）：版面调用 300 → ≈60。本机
  没有长篇财报样本，**长财报上的实际省调用比例未经验证**；AIA 演示稿这类图表密集 deck 只
  省 3/20。

## 未覆盖的版式（明确回退或已知粗糙）

- 三栏及以上、带整页宽标题的双栏、基线对齐的双栏叙事（`ambiguous_columns`）。
- 旋转文字、无文本层（扫描页）、span 越过页面边界的页。
- 含任何非装饰图形 / 图片的页（含矢量图表、单元格底纹之外的色块面板）。
- 无框线表格区域按行出 Text，不出 Table；跨页重复页眉每页各成一个对象（未去重索引）。

## Integration（2026-10-05）

- 接入点：`ingest_mode.LayoutPolicy = Literal["model", "deterministic-text-pages"]`，
  `make_partitioner(plan, client, sources, snapshot)` 在后者时把模型切分器包进
  `make_text_page_partitioner`。原先的 `partition_strategy` 参数并入 `layout_policy`。
- **库预设不启用、notebook 默认启用**：`ingest_plan("full")` 与 `ingest_plan("lite")` 都是
  `layout="model"`；`notebooks/run_folder.ipynb` 的 `LAYOUT_POLICY` 默认
  `"deterministic-text-pages"`，只在 `INGEST_MODE="lite"` 时传给 `run_folder_pipeline`；
  `INGEST_MODE="full"` 时 notebook 恒用 `"model"`（full 逐字节不变）并打印一行说明忽略了它。
  依据：失败方向是保守的——拿不准就回退模型；即便漏判，后果是该页图表的数字不可答，页面
  文字仍逐字收录，不会产生捏造的数字。
- 可见性：每份 PDF 的确定性页数 / 回退页数 / 回退原因分布在 notebook 状态表后的提示行与
  `report.md` 里，作为在真实长财报上判断是否漏判、调阈值的依据。
- 与 `text_lines.py`（ADR 0025）的几何去重：常量与 `RunningKey` 改为 import；`TextLine` /
  `text_lines` / `running_lines` 因行为差别保留本地版本（见模块 docstring）。
- 与按行收录（ADR 0027）的叠加：确定性页只出 Text / List 与划线 Table；不规整的无框线表格
  使 `column_layout` 判 `ambiguous`，整页回退模型，其 Table 照常按行收录；规整的无框线表格
  按行读成一个 Text 对象，每行科目与数值同在一行、同一检索单元
  （`test_deterministic_partition_table_rows.py`）。
