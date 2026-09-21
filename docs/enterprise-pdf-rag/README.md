# enterprise-pdf-rag

Python 3.12 / uv 文档 RAG 后端，目标是为不同 PDF 提供可追溯的来源提取、结构化处理、检索和问答。**AIA 是当前公开验收样本，不是产品限定的客户或文档类型。** 当前已验收的真实处理范围仍是这份报告的物理第 1–20 页；完整通用 RAG 尚未完成。原始 PDF、全部 71 页的原生 SVG 和文本位置作为来源缓存保存。

样本为 AIA 官网公开可查的《2026 Interim Results Presentation》（2026 年 8 月 20 日），来源是 [AIA 官方业绩与报告页](https://www.aia.com/en/investor-relations/overview/results-presentations)及[官方 PDF](https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2026/AIA%20Group%202026%20Interim%20Results%20Analyst%20Presentation%20Final.pdf)，并非私有客户数据。原 PDF 不随公共仓库发布。2026-09-19 已核对官方页面、PDF 封面日期和 71 页页数；本地文件身份仍由下文 SHA-256 固定。

**现在可以测试** Open WebUI 来源/处理结果审阅、原文 API、已发布描述索引的在线语义搜索与证据回填，以及第 18 页已取得资格的结构化 ChartQA。2026-09-19 已在官方 pdfspine 0.11.0 环境受控重启并完成一次真实本地查询向量搜索，返回 5 条命中且回填保持同一快照；普通自然语言财务聊天在该模式下仍返回 422。2026-09-20 起另有与之并存的 `document-catalog` 服务模式（多文档目录、按文档检索、证据链上逐字段校验的自然语言回答），已离线实现并测试；真实模型验收结论以 [交接文档](CLAUDE_HANDOFF.md) 为准。具体入口、请求、凭证要求和通用 RAG 的剩余条件见 [测试与入库指南](testing-and-ingestion.md)。界面可打开不代表 RAG 全链路通过。

当前验收样本是 AIA Group 报告。页面布局、typed IR、独立描述和资格回执分别保存；模型产物初始为 **pending**，成功保存不等于独立验证。只有逐字原文投影或具有完整字段资格的描述可以进入真实本地 embedding；ChartIR 和 SVG 不进入 embedding。`text.json` 仍是 pdfspine 原文观测。每页/对象是否已完成、失败或尚未运行，以处理 manifest 和实际文件为准。

本次实际产物覆盖 **20 页、241 个对象，241 份 IR 和 241 份描述/来源转录**。189 份描述投影保持原样复用，其中 180 份证明原文转录，9 份证明图表标签。第 18 页 Distribution Mix 的一个图表另已取得 **2 个显式百分比的数值关系资格**，支持受限查值和有序百分点差；其他图表不因此获准数值回答。独立真实 API 评测的 19 个正例、拒答和证据损坏案例通过，仍不代表完整 P5 或通用财务问答完成。初始加工结果见 [加工验收记录](processing-run-2026-09-19.md)，新增资格与限制见 [ChartQA 阶段说明](chart-qa-stage.md)。

本地继续使用 uv；独立的 Python 3.12 环境可直接 `python -m pip install .` 安装完整后端运行依赖，包括 pdfspine、SVG renderer 和来源字形验证所需的 FontTools。`pdf` / `processing` extras 保留为空兼容别名，无需额外选择。安装后用 `enterprise-pdf-rag serve` 启动已有 API，并通过 `APP_ROOT_DIR` / `APP_DATA_DIR` 指定源码目录以外的工作区与持久数据。Open WebUI 仍是单独安装和隔离的服务，不随本包安装。环境条件、可复制命令及 Databricks 平台区别见 [部署说明](databricks-deployment.md)；本地安装验证不代表已在 Databricks 部署。

## 通用 PDF 入库

安装包后可直接运行以下命令；仓库开发环境也可在命令前加 `uv run --locked`：

```sh
enterprise-pdf-rag ingest --pdf /path/to/document.pdf --pages 1-3,5
# 仓库中的薄封装使用同一组参数
python scripts/enterprise_pdf_rag/ingest.py --pdf /path/to/document.pdf --pages all
```

默认 `--stage source --max-live-calls 0`，不需要模型凭证，保存完整 PDF 的来源资产，并为选择的物理页生成 Canonical 原文观测。`--pages` 不截断原始 PDF 或来源页缓存。输出默认在 `APP_DATA_DIR/ingestion/<PDF-SHA256>/{source,processing}`；用 `--output-dir <父目录>` 可修改父目录。结果 JSON 返回 store 路径、source/processing ID、实际页数、选页、阶段状态和审阅路径，明确 `activated: false`、`indexed: false`；不会修改当前 AIA 发布或把新 PDF 自动接入 Open WebUI。

可显式选择 `--stage layout` 或 `--stage semantics`，使用同一套既有布局和独立语义分支；两者即使预算为 0 也须提供模型配置以定位缓存，正预算会调用配置的模型。该入口解除 AIA 文件身份和 20 页上限，但现有来源适配器仍对旋转页、非默认 CropBox 等未验证坐标场景明确拒绝。缓存、安装后 Python API、能力限制与后续 RAG 验收条件见 [测试与入库指南](testing-and-ingestion.md) 和 [ADR 0010](adr/0010-generic-pdf-ingestion-entry.md)。

入库产出的 draft 由三条独立命令按上面返回的 store 根和 `processing_id` 推进，不写死 AIA 身份：

```sh
enterprise-pdf-rag qualify --source-store <src> --processing-store <proc> --processing-id <id>
enterprise-pdf-rag index   --source-store <src> --processing-store <proc> --processing-id <id>
enterprise-pdf-rag publish --source-store <src> --processing-store <proc> --processing-id <id>
```

`qualify` 只读统计资格，零模型；`index` 用生产本地 embedding 构建 description-only 检索快照并产新不可变 snapshot，不切指针（`--document-label` 可覆盖审阅标题）；`publish` 原子切 `current-processing`，默认同时激活来源 manifest（`--no-activate-source` 只切 processing），未 `index` 的 draft 拒绝，内容寻址幂等。检索状态依次为 `not_ready → qualified; indexing pending → indexed; publication pending → ready`。这是资格/索引/发布入口，不等于通用 RAG 回答链已完成。已发布的文档由 `APP_EXECUTION_MODE=document-catalog` 的服务进程挂载，提供 `/v1/documents*`、`/v1/models` 与证据链上的 `/v1/chat/completions`（[ADR 0011](adr/0011-document-catalog-and-verified-answer-chain.md)、[测试与入库指南](testing-and-ingestion.md)）。`metadata` 子命令（或 `ingest --stage semantics|metadata`）为每页自动抽取逐字来源的标题 / 章节 / 页型 / 期间 / 地区，索引文本带页上下文头，聊天可按期间与地区前置过滤、多文档按封面标题与年份路由（[ADR 0013](adr/0013-page-metadata-and-prefilters.md)）。

## 公开验收样本的来源处理

所有命令从仓库根目录运行：

```sh
uv sync --locked --extra pdf
uv run --locked enterprise-pdf-rag ingest-aia
```

输入固定为 `data/samples/aia-group-2026-interim-results-presentation.pdf`，SHA-256 必须为 `df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e`。PDF 不随公共仓库分发；下载位置与来源见 [样本说明](samples/aia-report.md)，[71 页盘点](samples/aia-2026-interim-inventory.md) 和 [基准 manifest](../../data/benchmarks/enterprise-pdf-rag/aia-2026-interim/manifest.json)。不匹配的文件在解析前拒绝；缺页或失败有明确诊断，不静默遗漏。

全部输出在 `data/output/aia-2026-interim/`，不使用项目根的 `output/`：

| 路径 | 内容 |
| --- | --- |
| `source.pdf` | 已校验并保存的原始文件 bytes |
| `review.html` | 71 页导航与第 25 页重点选区 |
| `pages/page-001.html` … `page-071.html` | 每页原生 SVG、原文及 bbox、前后页导航 |
| `text.json` | 全 71 页 pdfspine 原文观测，含源 SHA、物理页号与文本位置 |
| `chart-ir.status.json` / `description.status.json` | 早期全文件来源审阅的状态；不是前 20 页处理结果 |
| `pages-001-020/review.html` | 当前前 20 页处理批次入口 |
| `pages-001-020/runs/<processing-id>/page-NNN/` | 原始/修正布局、Canonical、逐对象 SVG、真实 IR/description/raw/诊断 |
| `pages-001-020/current-processing` | 本地当前处理 manifest；不等于生产发布 |
| `objects/sha256/<digest>` | 不可变、内容寻址的 PDF/SVG/text sidecar/manifest |
| `current-manifest` | 本地当前完整 manifest 的标识 |
| `attempts/*.json` | 单独保存运行时间、成功/失败与诊断，不参与来源身份 |

对象读取会校验摘要和长度；同址内容冲突、缺失和跨页引用均拒绝。页级和选区侧车绑定同一源文件。选区保留原生 SVG 子树不等于证明视觉等价：第 25 页 SVG 的底部红色基线比 PDF 渲染细，详情在审阅页和 manifest 的诊断中。

## 前 20 页处理

模型调用仅由显式命令启动，普通 API/审阅不会自动运行推断。环境须独立提供 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`；不读取 `.env`，不写入凭证。示例只处理第 18 页，`--page` 可重复选择物理页 1–20：

```sh
# 0 = 只复用已有模型缓存，不发新请求
uv run --locked enterprise-pdf-rag process-aia-layout --page 18 --max-live-calls 0
uv run --locked enterprise-pdf-rag process-aia-semantics --page 18 --max-live-calls 0
# 显式的有限标签资格；不代表数值/期间/系列关系已验证
uv run --locked enterprise-pdf-rag process-aia-semantics --page 18 --max-live-calls 0 \
  --qualification-policy source-labels-only
# 只有明确授权新模型调用时才使用正预算；失败不自动重试
```

源观察、原始布局和带规则来源的修正布局分别存储；图表与其他视觉对象直接读取同一 SVG 派生视图，独立生成 typed IR 和自然语言，互不把另一分支作为输入。单支失败保留另一支及诊断。Text/List/Group 的逐字转录资格只证明原文，不能背书金融关系。原生 SVG 的视觉完整性仍独立为 pending。

`source-labels-only` 仅索引独立描述中逐字匹配单个来源 occurrence、且不含数字的标签。检索回填的图表投影把数值、期间、轴和关系保持 unknown；完整原始 ChartIR 仍可单独审阅。标签资格与数值 QA 资格分别报告，精确财务问题不会凭标签或原文数字得到放行。

若模型回写的来源摘要错误，旧响应作为拒绝产物保存，不修改它来制造通过。显式 `--correct-description-request <原请求fingerprint>` 或 `--correct-chart-request <原请求fingerprint>` 可在预算内安排一次同源修正请求；先验证旧缓存确有该错误，新请求和旧请求分别留存，成功与失败均缓存，没有自动循环重试。

固定某个已处理 manifest 后，可以显式运行本地检索验收：

```sh
uv run --locked enterprise-pdf-rag index-aia-processing \
  --processing-id <processing-id> --query "Distribution Mix chart" --limit 5
```

该命令要求独立的 `EMBEDDING_BASE_URL/MODEL/API_KEY` 和 `RERANK_BASE_URL/MODEL/API_KEY`，仅允许本机回环服务；远端模型经项目管理的 SSH 隧道接入。`scripts/enterprise_pdf_rag/with_local_models.py` 可把运行时读取的服务 key 仅传给子进程，具体主机配置不入库。不会继承云端 LLM key 或回退为 hash embedding。向量、描述、IR、源 SVG、资格和原始两支属于同一固定检索 snapshot，任何缺失或失配都拒绝。

每次验收另存 `retrieval-evaluations/<内容标识>/` 下的 `retrieval-example.json` 和 `retrieval-validation.json`，记录模型配置来源、维度、完整召回候选、cosine/重排分数、命中对象的审阅路径和金融 guard 检查。早期直接放在 run 目录的失败记录保留，不被后续验收覆盖。`coverage.json` 分开统计 IR/描述保存、仅转录资格、仅标签资格与数值关系资格；某个字段 unavailable 不会被总对象数量掩盖。

## 本地 Open WebUI

本机已有 Open WebUI **0.6.5** 时，可启动隔离兼容预览：

```sh
./scripts/enterprise_pdf_rag/start.sh
uv run --locked python scripts/enterprise_pdf_rag/webui_preview.py status
# 停止本项目的两个进程，保留数据
uv run --locked python scripts/enterprise_pdf_rag/webui_preview.py stop
```

`start.sh` 可通过其绝对路径从任意目录执行。它读取现有 `current-processing`，要求已发布的前 20 页产物；复用属于本项目且服务同一快照的健康进程，打印界面、审阅、日志和停止方式。缺依赖或数据时明确退出，不安装依赖、不处理 PDF、不调用模型。先用 `uv sync --locked --extra pdf` 准备项目环境；Open WebUI 解释器从 `PATH` 中发现，或用 `OPEN_WEBUI_PYTHON=/path/to/environment/bin/python ./scripts/enterprise_pdf_rag/start.sh` 明确指定已有 Python 3.12 / Open WebUI 0.6.5 环境。脚本不读取个人 shell 配置或 `.env`，厂商进程仍使用隔离白名单环境。

打开 [Open WebUI](http://127.0.0.1:8767)，选择 `AIA 2026 中期业绩 — 原文审阅 / 语义待验证`，输入 `查看当前文件` 或 `查看第25页`。API 在 `127.0.0.1:8766`；[来源浏览](http://127.0.0.1:8766/v1/aia/review) 提供原始资产与逐页入口。回答固定到已保存的 manifest，不调用模型、不使用合成数值回退。已有处理批次时，`查看当前文件` 显示实际处理统计并链接前 20 页产物；71 页来源浏览仍独立保留。

Open WebUI 的内置上传、PDF 解析、RAG、工具和后台自动生成被部署边界阻断。厂商进程使用私有数据库、静态资产、缓存和最小环境，拿不到上游 API key。默认业务 profile 是 `aia-source-review`。启动前需完成上述真实文件 ingestion；缺源资产会失败，不会自动切 demo。

隔离容器固定官方 **0.11.3-slim** 镜像摘要；本机无可用 Docker daemon，容器尚未实际运行。启动方式、版本区别、限制和已知旧版首次启动静态资产副作用见 [Open WebUI 使用说明](open-webui.md)。不安装或修改全局依赖。

## 来源 API

- `GET /v1/aia/manifest`：固定来源与完整资产清单。
- `GET /v1/aia/pages/25/text`：第 25 页原文和 bbox。
- `GET /v1/aia/assets/<digest>`：只读取属于当前 manifest 的已验证对象。
- `GET /v1/aia/review`：逐页来源审阅。
- `GET /v1/aia/pages/page-001.html`：单页 SVG 与原文；页面号支持 001–071。
- `GET /v1/aia/source.pdf`、`GET /v1/aia/text.json`：同一来源的完整 PDF 和原文侧车导出。
- `GET /v1/models`、`POST /v1/chat/completions`：受限原文/处理状态审阅，支持非流式与 SSE。
- `GET /v1/processing/status`、`GET /v1/processing/manifest`：固定处理批次的实际状态和完整依赖。
- `GET /v1/processing/review/review.html`：本批次逐对象 SVG / IR / 描述 / 诊断。
- `POST /v1/processing/search`、`POST /v1/processing/context`：固定 snapshot 的描述检索与无模型证据回填。新 app factory 从独立 `EMBEDDING_*` 配置注入 query embedder；缺少/无效配置或服务失败返回 503，启动不调用模型。本轮实际在线搜索与同 snapshot 回填已通过；HTTP 搜索使用 cosine 排序，不执行 rerank 或 LLM。context 可按已有有效 hit 回填，不调用模型。
- `POST /v1/queries`：结构化 ChartQA。仅在固定 member 的来源数值资格通过后，支持显式百分比查值与同图、同系列、同期间的百分点差；每个字段保留 SVG/来源 occurrence 引用。

未知模型、越界页、错 snapshot、缺源证据或财务推断请求均明确拒绝。已有持久源资产、不可变 manifest 和本地原子指针；生产级多存储 CAS 发布、ACL/撤回、并发调度与完整财务 QA 尚未实现。

结构化问题也可直接读取持久化证据执行，不调用模型：

```sh
uv run --locked --no-sync enterprise-pdf-rag chart-qa --request query.json
```

`query.json` 必须指定 `kind: "chart"`、实际 `processing_id` / `snapshot_id` / `member_id`、`operation`、`series`、`period`、`unit` 及带 `point_id` / `category` 的 `points`。`lookup` 接受一个点；`percentage_point_difference` 接受两个有序点，结果单位为 `percentage_points`，不会伪造原文 display。请求不能提交数值、verified 标志或任意公式。旧标签资格快照仍拒绝数值查询；不可用证据与跨快照错误也不会回退到摘要。具体资格前提、独立金标和未完成的 PRD 门见 [ChartQA 阶段说明](chart-qa-stage.md)。

## 显式合成回归示例

下面的 `Revenue 2024=10 / 2025=15` 是内部创作的 PDF fixture，**不是 AIA 数据**，不作为默认业务入口：

```sh
uv run --locked enterprise-pdf-rag demo --mode offline-demo --output data/output/demo
# 仅在明确需要检查合成UI回归时使用；需先停止当前预览
uv run --locked python scripts/enterprise_pdf_rag/webui_preview.py start --profile offline-demo
```

这个独立测试链覆盖同 SVG 的 ChartIR/description 配对、仅 description 的 demo token-hash embedding、固定 snapshot 回填和字段证据。这一合成链与真实 AIA 处理/本地模型配置分离；hash 向量不代表语义检索。保守的单页提取命令仍保留：

```sh
uv run --locked enterprise-pdf-rag extract \
  --pdf data/samples/aia-group-2026-interim-results-presentation.pdf \
  --page 10 --bbox 30 120 310 330 --output data/output/aia-page-10
```

## 开发与验收

2026-09-19 正式依赖已锁定公开 PyPI `pdfspine==0.11.0`，完整 `bash scripts/ci.sh` 通过 639 tests；独立 Python 3.12 的普通 pip 安装、`pip check` 和 checkout 外 smoke 通过。本机验证不代表 GitHub Linux CI 或 Databricks 部署已经完成。

```sh
uv run --locked pytest tests/enterprise_pdf_rag/documents  # TDD 先跑相关测试
make fmt                              # 本地安全修复与格式化，会写文件
bash scripts/ci.sh                               # 唯一完整、只读、离线工程门
```

重大改动后和阶段收尾必须运行完整 `bash scripts/ci.sh`：Ruff、strict mypy、纯领域架构、版本化 schema、文档漂移、单元及离线集成测试，warnings 当作错误。测试不连接网络；真实语料测试在本地样本存在时执行，公共仓库不包含 PDF。协议与失败契约仍有独立小型离线 fixtures。

真实 LLM 测试只在大版本或模型调用流程实质变化时显式触发，普通改动使用 transport 替身。来源 ingestion 不需要 LLM；前 20 页的视觉语义加工则使用有预算、可缓存的实际模型请求。已有 `llm-smoke` 仅验证连接；从环境读取 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`，不读取 `.env`、不打印密钥，也不证明图表质量。本地 embedding/rerank 使用独立配置，未配置时拒绝，不继承云端 LLM。

架构和范围见 [ADR 0001](adr/0001-architecture.md)、[图表链 ADR 0002](adr/0002-figure-pipeline.md)、[UI ADR 0003](adr/0003-open-webui.md)、[真实来源 ADR 0004](adr/0004-aia-source-review.md)、[前 20 页 ADR 0005](adr/0005-first-twenty-pages-processing.md)、[其他视觉 ADR 0006](adr/0006-non-chart-visual-semantics.md)、[独立安装 ADR 0007](adr/0007-installable-runtime.md)、[通用入库 ADR 0010](adr/0010-generic-pdf-ingestion-entry.md)、[文档目录与回答链 ADR 0011](adr/0011-document-catalog-and-verified-answer-chain.md)、[图表索引与召回 ADR 0012](adr/0012-chart-index-text-and-retrieval-seats.md)、[页级元数据与前置过滤 ADR 0013](adr/0013-page-metadata-and-prefilters.md)、[划线表格网格证明 ADR 0014](adr/0014-ruled-table-grid-proof.md)、[Diagram 与 Formula 可检索 ADR 0015](adr/0015-diagram-and-formula-retrievable.md)、[逐字图表点位 ADR 0016](adr/0016-verbatim-chart-points.md)、[页级父子窗口 ADR 0017](adr/0017-page-context-window.md)、[PRD v0.2](PRD-v0.2.md)。PDF、密钥、运行产物、虚拟环境与本地 IDE 配置不进入公共仓库。
