# ADR 0039: 版面回退不再调用 VLM（`layout_fallback`）

Status: Accepted, 2026-10-09, as an **explicit choice only**: the default `layout_fallback="model"`
keeps every byte of ADR 0028 / ADR 0030（两个 `ingest_mode` 预设与 notebook 默认都不选它）。
Amends [ADR 0028](0028-deterministic-text-page-partition.md) 与
[ADR 0030](0030-onnx-layout-partitioner.md) 的组合切分器：只换掉它们**回退位**上的模型版面。

## Context

`deterministic-text-pages` / `onnx-layout` 两个路由切分器拿不准的页，各自回退到
`ModelPagePartitioner`：每页渲染一张 PNG 再发一次 `page-layout-v2` VLM 调用（几秒到十几秒）。
实测一份 71 页的报告在没装 onnxruntime 的机器上 79% 的页走了这条回退，是真实环境 ingest 的
最大耗时来源。之前有人用 `MAX_LIVE_CALLS_PER_PDF=0` 当"不调模型"的开关，结果回退页停在
"需要模型"（预算延期）状态，整页版面缺失、对象全丢——预算是上限，不是策略开关。

## Decision

新增 `IngestPlan.layout_fallback`（`ingest_mode.LayoutFallback`），设置项
`Settings.layout_fallback` / 环境变量 **`APP_LAYOUT_FALLBACK`**，`ingest_pdf(layout_fallback=…)`
可逐次覆盖（`None` 读设置）：

| 值 | 路由拿不准的页 | ONNX 低置信页（`onnx_low_confidence`） |
|---|---|---|
| `model`（默认） | 模型版面，逐字节同前 | 模型版面 |
| `onnx-accept` | 文本切块 | **接受** ONNX 返回的全部框（含 [0.3, 0.5) 低分框）；完全没框才文本切块 |
| `text-only` | 文本切块 | 文本切块 |

- `make_partitioner` 仍是唯一选择点：非 `model` 时回退位换成
  `text_block_partition.TextBlockPartitioner`（零调用：span 聚行 → 竖直空白或标题行分块 →
  每块一个 Text 对象；页外 span 显式未归属；永远产出通过 `validate_partition` 的划分），
  `onnx-layout` 时 `OnnxPagePartitioner(accept_low_confidence=…)`。
- `layout="model"`（含 full 模式）时不起作用：那里没有路由，也就没有回退位；full 的字节不变。
- **不丢页**：两种非 `model` 值下版面阶段不发任何模型请求，预算为 0 也每页都有划分（测试钉住
  默认 `model` + 预算 0 时 7 页只剩 4 页、新策略 7/7 且 `live_call_count == 0`）。
- **可见**：文本切块产物 producer `page-layout-text-blocks-v1`、首条诊断 `text-partition: ok`
  （诊断只有 code / 计数）；`IngestionSummary.pages_partition_text_fallback` /
  `partition_text_fallback_reasons`（最内层路由的原因码，**不计入** `pages_partition_model_fallback`）/
  `pages_onnx_low_confidence_accepted`（ONNX 页诊断带 `onnx_low_confidence_accepted`）；
  `document_done` 进度事件在非零时带前两项；`report.md` 的版面行追加
  `text fallback N (codes)`（为 0 时行文不变）。从已存产物重导出，缓存重放同样成立。
- 缓存隔离：`TextBlockPartitioner` 的指纹进路由指纹，接受低置信的 ONNX 路由指纹多
  `;accept-low-confidence`，与默认策略的阶段缓存互不混淆；切回 `model` 只补发那些页的版面调用。
- 路由追加到回退产物上的诊断沿用历史措辞 `…: model fallback (<code>)`——它是解析键，含义是
  "交给被包装的切分器"；这页实际用了什么由 producer 决定。

## Consequences

- 省下的是每个回退页一次 VLM 调用。代价：文本切块页只有 Text 对象——那页的图表 / 图片 / 公式
  不成对象，图表数字在这些页不可答（`text-only` 下 ONNX 低置信页同样如此；`onnx-accept` 把低分
  图表框收成 Chart，IR 仍由模型分支生成）；分栏页左右栏可能并进同一块（只影响检索粒度，逐字
  内容与溯源不变）。拿召回换时间，所以只做显式选择。
- `onnx-accept` 放弃了 ADR 0030 的漏图守卫（低分视觉框不再触发回退，而是直接收成对象），
  可能多出误检的 Chart / Table（多花图表分支调用），但不会静默丢掉低分图表。

## Databricks：让 ONNX 版面真的可用

三个 notebook（`run_folder` / `ingest_timing` / `ingest_diagnostics`）此前只装 `..[pdf,service]`，
onnxruntime 根本不在集群上，`LAYOUT_POLICY="auto"` 永远退到 `deterministic-text-pages`。现在：
安装格装 `..[pdf,pdf-onnx,service]`（`ingest_timing` 沿用 run_folder 的安装）；新增 `onnx-check`
格在开头打印 `onnx_partition.ensure_onnx_layout_weights(...)`（权重缺失时从
`ONNX_LAYOUT_MODEL_URL`——与 pdfspine `_onnx.LAYOUT_MODEL_URL` 相同的 ModelScope RapidAI
地址，约 130MB——下载：配了 `APP_ONNX_LAYOUT_MODEL` / `PDFSPINE_ONNX_MODELS` 就下到那里，
都没配就下到 `DEFAULT_ONNX_MODELS_DIR` = `<ROOT_DIR>/data/models/pdfspine-onnx/`（`data/` 不进
git），`resolve_onnx_layout_model` 在其余位置都没有时最后找它——所以 pull 后不配任何变量，
`LAYOUT_POLICY="auto"` 即选 `onnx-layout`；同一格把 ADR 0031 的表格结构权重 `slanet-plus.onnx`
（约 8MB）下到同一目录，`pdfspine_tsr` 在其余位置都没有时也最后找默认目录，于是
`UNVERIFIED_TABLE_STRUCTURE="auto"` 即选 `tsr`；下载失败只给出地址，不中断）、
`onnx_partition.onnx_layout_status(...)`（`可用=是/否, 权重=<路径|未配置>` + 缺什么怎么装）与
当前 `APP_LAYOUT_FALLBACK`。权重许可门（ADR 0030）不变：权重不进仓库、不进 wheel。
