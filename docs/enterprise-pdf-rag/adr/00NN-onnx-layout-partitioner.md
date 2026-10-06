# ADR 00NN: 本地 ONNX 版面切分器（pdfspine PP-DocLayoutV3）

Status: Draft（分支 `feat/onnx-layout-partitioner`；编号 00NN 为占位，集成时定号）, 2026-10-06,
as an **explicit choice only**: no library preset selects it（两个 `ingest_mode` 预设仍是
`IngestPlan.layout="model"`）。在 ADR 0028（确定性文本页切分）之上再加一层：确定性分诊
回退的"含图页"不再直接花一次带 PNG 的 LLM 版面调用，而是先用 pdfspine 0.11.0 内置的本地
ONNX 版面模型（PP-DocLayoutV3，进程内推理、零网络、零 LLM 调用）切分，只有 ONNX 也拿不准
的页才回退模型版面。Amends [ADR 0025](0025-lite-ingest-mode.md) 的 `LayoutPolicy` 与
[ADR 0028](0028-deterministic-text-page-partition.md) 的组合切分器；chart IR 仍只由模型
生成（用户决定），本 ADR 只决定"哪里是图表 / 表格 / 文本"，从不读数。

## Context

用户在 Databricks 上对加密长篇财报入库答题，LLM 按次计费。lite + `deterministic-text-pages`
后，纯文字页已零调用，但含图 / 残余图形 / 分栏不清的页仍每页一次带页面 PNG 的
`page-layout-v2` 调用，是剩余最大开销。对比调研：RAGFlow 的 deepdoc 用进程内 ONNX 版面模型
做到版面零 LLM；pdfspine 0.11.0 已打包同类模型（`find_layout()`，PP-DocLayoutV3 RT-DETR，
输出标题 / 段落 / 表 / 图 / 页眉页脚 / 脚注及阅读顺序键），ragspine 此前从未调用。

## Decision

新增 `LayoutPolicy` 值 **`"onnx-layout"`**（`make_partitioner` 仍是唯一选择点），组合顺序：

```
deterministic-text-pages（纯文字页, 零调用零推理, ADR 0028 原样）
  → OnnxPagePartitioner（其余页, 本地 ONNX 推理, adapters/onnx_partition.py）
    → ModelPagePartitioner（ONNX 回退页, 与默认策略逐字节相同的模型请求）
```

1. **标签映射**（pdfspine 已把 PP-DocLayoutV3 的 25 类归一化为 `label`，原始类在
   `raw_label`）：`table` → Table；`figure` 原始类 `image` / `seal` → Image，其余（`chart`
   及未来未知 figure 类）**保守地 → Chart**——Chart 交给后续模型分支取 IR 并自行判定，
   错标成 Chart 的代价是两次模型调用，错标成 Image 的代价是图表数字永久不可答；
   `isolate_formula` → Formula；页眉 / 页脚 / 页码 → Text 并在 interpretation 里带角色
   标记（schema 无专门字段，与确定性切分器一致，span 绝不丢弃）；`header_image` /
   `footer_image` → Image；标题 / 正文 / 题注 / 脚注与一切未知标签 → Text。
   PP-DocLayoutV3 没有列表类，List 的分项 schema 无从满足，列表按 Text 收录；也没有
   diagram 类，流程图通常落为 Image / Text——**diagram 证明链不可达**是已测的已知限制
   （见质量对照），需要 Diagram 对象时该页用 `"model"`。
2. **span 归属**：按中心点落在哪个 block 内（与 `PdfspineTableAdapter` / ADR 0028 同一
   判据）；中心点同时落在多个 block 时仅接受纯嵌套（最小候选框被其余每个候选框完全包含，
   取最内层），真重叠整页回退 `onnx_overlapping_blocks`；未归属 span 并入（矩形距离）
   最近的 Text 块并把对象 bbox 扩到并集，落空占比 > `ONNX_MAX_UNASSIGNED_SPAN_SHARE`
   (0.2) 或页上没有 Text 块可并时回退 `onnx_unassigned_spans`。产物 `unassigned_span_ids`
   恒空，必过 `validate_partition`，不满足即回退 `onnx_partition_invalid`。
3. **双阈值漏图守卫**：检测按 `ONNX_SUSPECT_VISUAL_SCORE` (0.3) 请求；得分 ≥
   `ONNX_MIN_BLOCK_SCORE` (0.5) 的框才进划分；落在 [0.3, 0.5) 的视觉框（figure / table /
   isolate_formula）若没有任何已接受视觉区覆盖（中心点互指），说明这页可能有图被漏掉，
   整页回退 `onnx_low_confidence`。依据：AIA 真实对照 p18 右半页柱状图仅 0.389 分，单阈值
   下该页被"干净地"切完、图表数字**静默**不可答——这是最坏的失败方向；守卫把它变成一次
   模型调用。低分文本框不设守卫（文本永不静默丢失，span 覆盖规则兜底）。
4. **producer 与缓存**：`page-layout-onnx-v1:pdfspine/<ver>:<模型文件 sha256 前 12 位>`，
   模型换了缓存自然隔离。**onnxruntime 版本不进指纹**：权重摘要已唯一标识所算的函数，
   ort 升级的数值抖动远小于阈值粒度，进指纹只会让每次 ort 升级作废全部已存版面产物。
   组合切分器 stage 指纹串联三个 producer，三种策略产物在同一 store 共存互不覆盖；回退页
   的模型请求与默认策略逐字节相同，已入库文档切换策略**零新增 live call**（测试钉住）。
   回退页 diagnostics 追加 `onnx-partition: model fallback (<原因码>)`，原因码只有
   `onnx_unavailable` / `onnx_low_confidence` / `onnx_overlapping_blocks` /
   `onnx_unassigned_spans` / `onnx_partition_invalid` 五个，绝不含正文。
5. **可用性预检，绝不静默回退**：构造切分器（入库开始前）即检查 onnxruntime / numpy /
   Pillow 可导入与模型文件存在，缺失给中文报错（装 `pip install 'pdfspine[onnx]'`、权重放
   哪、`APP_ONNX_LAYOUT_MODEL` 怎么指）——静默逐页回退会让用户以为省了调用实际没省。
   单页推理失败按页回退 `onnx_unavailable` 并计入原因分布（可见）。模型路径解析：
   settings `onnx_layout_model`（env `APP_ONNX_LAYOUT_MODEL`，可指文件或目录）→ 环境变量
   `PDFSPINE_ONNX_MODELS` 目录。权重不随任何 wheel 分发。
6. **性能**：每页一次推理；pdfspine 按（模型路径, provider, 变体）缓存 onnxruntime 会话，
   逐页**绝不重新加载模型**；PDF 经 `shared_pdfs()` 作用域只打开一次。实测 Apple Silicon
   CPU 0.74 秒/页（首页含会话初始化 1.38 秒）；pdfspine 文档记录 x86 CPU 约 3.2–3.7 秒/页。
7. **可见性**：`IngestionSummary.pages_partitioned_onnx`（新字段）与既有
   `pages_partitioned_deterministically` / `pages_partition_model_fallback` /
   `partition_fallback_reasons` 并列，从已存产物重导出（缓存重放数字不变）；回退页取
   **最内层路由**的原因码（到模型手里说明 ONNX 也放弃了，它的 `onnx_*` 码才解释这次调用）；
   report.md 的每 PDF 行在 onnx 页数非零时插入 `onnx N,`，为零时与 ADR 0028 行文逐字节一致。

## 保证强度

- 反捏造 / 溯源不变：对象文字全部是 span 逐字内容（本模块只分组，从不改写），每个 span
  有归属，产物过 `validate_partition`，诊断只有原因码与计数。Table 数值资格仍完全由
  ADR 0014 网格证明决定，chart 数字仍要过 ADR 0008/0016 的源资格——ONNX 只提出区域。
- 漏图的失败方向保守：被漏的图其文字 span 仍逐字进 Text 对象（可 quote），不会捏造数字；
  双阈值守卫进一步把"模型自己都犹豫的图"变成模型版面调用而不是静默丢失。

## 质量对照（2026-10-06，AIA 业绩演示稿 20 页，对照已存模型版面，本机真实权重）

- 回退分布：ONNX 切分 11/20 页；回退 9 页 = `onnx_low_confidence` 6（其中 p18 为守卫命中，
  p5/p13/p14 等为低分可疑图）+ `onnx_unassigned_spans` 2 + `onnx_overlapping_blocks` 1。
- **漏判逐例**（ONNX 切分的 11 页上，模型版面视觉对象 vs ≥0.5 分重合视觉框，IoU≥0.3）：
  共 5 例——4 例是**零 span 的矢量公司 logo**（p1/p2/p4/p19，Image 本就不可检索，无文本
  损失，与 ADR 0028 的装饰豁免同类）；1 例是 **p6 的三阶段路径 Diagram**（3 个 span）：
  V3 无 diagram 类，节点文字进了 Text（可 quote），diagram 类型引用不可达——已知限制。
  **Chart 零漏判：ONNX 切分页上的 16/16 个模型 Chart 全部有 ≥0.5 分重合图框**
  （p6 2、p8 1、p10 3、p11 3、p12 2、p15 4、p19 1）；守卫未启用前 p18 丢 1 图（0.389 分），
  守卫后该页回退模型。
- span 归属纯度：104/120（86.7%）的 ONNX 对象其 span 全部来自同一个模型对象（不纯的主要是
  slide 版式下 ONNX 文本框比模型对象粗或细一级，只影响检索粒度）。
- 每页耗时：均值 0.74 秒（Apple Silicon CPU，onnxruntime 1.30，144 DPI，800×800 输入）。
- 版面调用外推：AIA 这类图表密集 deck 20 页 → 9 次（-55%）；长篇文字财报上确定性分诊已
  拿走纯文字页，ONNX 针对的是剩下的含图页——**真实长财报上的实际省调用比例未经验证**。

## 许可门与进默认路径的条件

- 权重许可：pdfspine `_onnx.py` 注释称 PP-DocLayoutV3 为 Apache-2.0（PaddleX/PaddleOCR
  上游，RapidAI 发布的 ONNX 导出；曾因 AGPL 弃用过 YOLO 检测器）。按
  [家族 ADR 0009（依赖与框架政策）](../../adr/0009-dependency-and-framework-policy.md)
  的许可门，**进默认路径前**须独立核对权重文件自身的许可声明与再分发条款，不以注释为准；
  权重不进 wheel、不进仓库，只按部署文档放置。
- 精度门：进任何预设前须在**真实长篇财报**（几百页、文字表格为主）上重跑本对照（漏图
  逐例、回退率、省调用比例）；AIA 20 页 deck 不构成长财报证据。
- 在那之前：两个库预设保持 `layout="model"`；notebook 由集成步骤决定是否默认（建议见
  集成备注）。

## Databricks 部署

权重放 Unity Catalog Volume（如 `/Volumes/<catalog>/<schema>/models/pdfspine-onnx/
pp_doc_layoutv3.onnx`），`APP_ONNX_LAYOUT_MODEL` 填该绝对路径（或其目录）；集群装
`pdfspine[onnx]`（onnxruntime CPU 即可）。详见 `docs/enterprise-pdf-rag/databricks-deployment.md`
（本分支同步了相应小节）。**未在真实 Databricks 环境实测**（FUSE 上 onnxruntime 读
Volume 权重、每页渲染的内存占用），列为集成后续。

## Integration（待集成步骤确认）

- 接入点：`ingest_mode.LayoutPolicy += "onnx-layout"`；`make_partitioner` 在该值时
  `make_text_page_partitioner(make_onnx_page_partitioner(model, …), …)`。
- notebook 建议（本分支不改 `notebooks/run_folder.ipynb`）：`LAYOUT_POLICY` 常量在权重就位
  后可改 `"onnx-layout"`（仅 lite 时传入，与 ADR 0028 相同的门）；提示行建议在既有
  deterministic / fallback 行基础上加 onnx 页数（`pages_partitioned_onnx`），并在权重缺失
  报错时原样展示中文指引。
- 测试：`tests/enterprise_pdf_rag/adapters/test_onnx_partition.py`（桩 + `@pytest.mark.onnx`
  真模型集成用例，CI `-m "not onnx"` 跳过）、`test_onnx_partition_ingest.py`（端到端：
  版面调用数 = 回退页数、chart IR 仍走模型、切换策略零新增调用、缺权重入库前报错）。
