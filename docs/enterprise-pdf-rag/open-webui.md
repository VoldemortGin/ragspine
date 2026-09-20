# Open WebUI 本地来源审阅

## 当前范围

当前兼容预览固定到 AIA Group 2026 中期业绩演示文稿这一公开验收样本；项目目标是通用文档 RAG，当前界面尚不能上传、选择或切换任意 PDF。实际可测范围和请求见 [测试与入库指南](testing-and-ingestion.md)。默认界面用于审阅该样本。来源层显示 pdfspine 保存的原始 PDF、71 页 native SVG 与文字观测，并固定到同一份不可变 manifest。处理层仅覆盖物理第 1–20 页：241 个对象均已保存 typed IR 和独立描述或逐字原文 projection，其中 180 个来源转录和 9 个无数字图表标签可以进入 description-only 检索；第 18 页 Distribution Mix 已取得 2 个数值关系资格。其余推断保持 `pending`，界面不会把标签命中当作已验证财务答案。

| 部分 | 状态 | 已验证范围 |
| --- | --- | --- |
| AIA 来源 ingestion | 已实跑 | 固定 SHA-256、71 页完整覆盖、原 PDF、native SVG、文字 sidecar、第 25 页选区和 pending 状态文件已落盘 |
| 物理第 1–20 页处理 | 已实跑 | 241 个对象、241 份 typed IR、241 份描述；29 个 Chart 均有独立双支，180 个来源转录、早期 9 个仅标签资格；当前其中第 18 页 donut 已增加 2 个数值关系资格 |
| 本地检索与证据回填 | 已实跑 | 189 个合格描述、2560 维真实向量、固定 snapshot 检索与同 snapshot hydrate；金融 guard 拒绝仅转录和仅标签范围的数值问答 |
| HTTP 语义 search 与 context | 已实跑 | 官方 pdfspine 0.11.0 环境受控重启后，一次真实 query embedding 返回 5 条命中，context 保持同 snapshot；缺少/无效配置或服务失败为 503；不调用 rerank/LLM |
| OpenAI 兼容来源审阅后端 | 离线测试通过 | 唯一来源审阅模型、普通 completion、SSE、错误 snapshot、未知问题和缺失证据 fail closed |
| Open WebUI 0.6.5 兼容预览 | 已实跑 | 隔离数据目录、真实浏览器、唯一 AIA 来源模型、71 页身份、第 25 页引用与 pending 提示；不能代表 0.11.3 兼容性 |
| Open WebUI 0.11.3 官方目标 | 仅配置 | Compose 配置可静态校验；当前机器没有可用 Docker daemon，镜像未构建、容器未启动、页面未验证 |

## 准备唯一来源

样本 PDF 不随公开仓库分发。先把官方文件放在：

```text
data/samples/aia-group-2026-interim-results-presentation.pdf
```

它必须匹配仓库中记录的 SHA-256 `df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e`；来源和使用限制见 [样本说明](samples/aia-report.md)。所有命令从仓库根目录运行：

```sh
uv sync --locked --extra pdf
uv run --locked enterprise-pdf-rag ingest-aia
```

命令只接受这一个文件身份，使用 pdfspine 导出全部 71 页；缺页、摘要不匹配或对象损坏都会阻止发布新的 `current-manifest`。结果写入 `data/output/aia-2026-interim/`：

- `review.html`：全部 71 页入口和第 25 页真实 native SVG 重点选区。
- `pages/page-001.html` … `page-071.html`：逐页 native SVG、同页原文 bbox、前后页与原 PDF 页链接。
- `source.pdf`：已核对 SHA-256 的原始文件副本。
- `text.json`：71 页 pdfspine 原文观测，不是模型描述。
- `chart-ir.status.json`、`description.status.json`：明确记录尚未生成语义产物，`artifact_id` 为 `null`。
- `objects/sha256/` 与 `current-manifest`：内容寻址对象和当前完整快照指针。

上面的两个 `*.status.json` 只描述 71 页来源 ingestion 本身，不是第 1–20 页处理结果。前 20 页的原始/修正布局、逐对象 SVG、typed IR、独立描述、原始模型响应、诊断和检索记录位于 `data/output/aia-2026-interim/pages-001-020/`；入口为 `review.html`，当前不可变处理指针为 `current-processing`。页 21–71 没有运行语义处理。

这些文件位于被 Git 忽略的运行目录，不能提交到公开仓库。第 25 页 native SVG 与 PDF 渲染的底部红色基线存在粗细差异，因此 `visual_completeness` 仍为 `pending`。

## 启动 0.6.5 兼容预览

预览脚本使用项目 `.venv` 启动后端，并从 `PATH` 发现已有 Python 3.12 / Open WebUI 0.6.5 解释器；也可以用 `OPEN_WEBUI_PYTHON=/path/to/environment/bin/python` 指定。只检查安装元数据，不导入 vendor 应用探测环境，不安装、更新或下载 vendor 依赖。

```sh
./scripts/enterprise_pdf_rag/start.sh
uv run --locked python scripts/enterprise_pdf_rag/webui_preview.py status
```

`start.sh` 通过脚本位置定位项目，使用绝对路径调用时不要求当前目录在仓库内。它复用 `webui_preview.py` 这套管理，要求已有前 20 页发布产物的 `current-processing`，不重跑 ingestion、embedding 或模型请求。已记录的 API/WebUI 只有在 PID 命令、工作目录、模型 profile、当前快照及 HTTP 健康检查一致时才复用；重复执行不创建新进程。脚本会打印当前快照、所有入口和可从任意目录执行的状态/停止命令。

项目依赖缺失时先执行 `uv sync --locked --extra pdf`。若尚无兼容的 vendor 环境，可手动在项目私有目录准备，随后指定解释器（这不是启动脚本的自动步骤）：

```sh
uv venv --python 3.12 data/open-webui-runtime
uv pip install --python data/open-webui-runtime/bin/python 'open-webui==0.6.5'
OPEN_WEBUI_PYTHON="$PWD/data/open-webui-runtime/bin/python" ./scripts/enterprise_pdf_rag/start.sh
```

缺少处理数据时须先恢复已有 `data/output/aia-2026-interim/` 资产，或按 README 显式执行处理流程。没有有效数据时启动命令不会生成演示数据替代。脚本不主动 source `~/.zshrc`、不读取 `.env`，不会将上游模型凭证传给厂商进程。

`status` 的以下 HTTP 200 仅证明模型发现/UI 服务可达，不证明语义检索、模型回答或完整 RAG 通过：

- API 模型发现：<http://127.0.0.1:8766/v1/models>
- WebUI 配置探针：<http://127.0.0.1:8767/api/config>

然后打开 <http://127.0.0.1:8767>。默认唯一模型是 `aia-2026-interim-source-review-v1`。可使用：

```text
查看当前文件
查看第25页
show source document
```

“查看当前文件”返回固定处理批次的实际计数和 `/v1/processing/review/review.html` 链接；“查看第25页”等页级问题仍只列出固定来源 manifest 中的原文 span、1-based PDF 页码和左上角坐标系 bbox。`/v1/aia/review`（或 `/v1/aia/review.html`）列出 71 页；逐页地址例如 `/v1/aia/pages/page-025.html`。处理 API 另提供 `/v1/processing/status`、`/v1/processing/manifest`、固定 snapshot search/context 与逐对象审阅文件。它明确区分来源观测、`pending` 推断和有限资格，不将相邻数值、年份或图元认定为同一 series，也不生成未经资格的财务结论。本轮已在真实 0.6.5 浏览器新会话中验证默认 AIA profile：回答识别 71 页文件与第 25 页来源，显示 pending 缺口，且没有出现合成 10/15 数据或 demo fallback；前 20 页处理结果另以 HTTP 与静态产物闭包验收。

日志、PID 记录、SQLite、静态文件和缓存均位于 `data/open-webui-preview/`。可用以下命令查看日志：

```sh
tail -f data/open-webui-preview/api.log
tail -f data/open-webui-preview/webui.log
```

停止时只会向 PID 记录中、命令标记和工作目录仍匹配本项目的进程发送 `SIGTERM`：

```sh
uv run --locked python scripts/enterprise_pdf_rag/webui_preview.py stop
```

停止命令保留隔离数据。若 PID 记录失效、服务不健康或仍固定到另一个快照，启动会明确拒绝；先查看日志并使用现有 `stop` 清理本项目记录，再运行 `start.sh`。端口由未登记进程占用时不会杀掉它，也不会尝试另起一组进程绕过。

## 显式合成回归模式

内部创作的 10/15 两柱 fixture 仅保留给测试与显式回归，不是业务验收，也不会在默认 AIA profile 中出现：

```sh
uv run --locked python scripts/enterprise_pdf_rag/webui_preview.py start --profile offline-demo
```

该模式的唯一模型为 `enterprise-pdf-rag-offline-demo-v1`，问题集合和证据固定。它不能替代真实 AIA 来源 ingestion 或财报问答验证。

## document-catalog profile：任意已发布文档的证据链聊天

`--profile document-catalog` 让同一个 gate 服务 `document-catalog` 模式的后端（[测试与入库指南](testing-and-ingestion.md) 的同名节，契约 `rag-chat-v1`）。它与两个固定 profile 并存，后者行为不变。

- 模型列表来自后端 `/v1/models`：每个已挂载文档一个 `enterprise-pdf-rag/<sha256 前 12 位>`，在 Open WebUI 的模型选择器里按文档挑选；没有预设默认模型，也没有固定问题集，问题是自由文本，后端只按已验证证据回答或拒答。
- 请求路径：浏览器 → gate（`/api/chat/completions`，仍拒绝上传、工具、knowledge、联网）→ Open WebUI → gate 自带的 relay（`/v1/models`、`/v1/chat/completions`；`OPENAI_API_BASE_URL` 指向 gate 自己的端口）→ 后端 API（`ENTERPRISE_WEBUI_BACKEND_URL`）。Open WebUI 进程本身不直接连后端。
- relay 白名单净化：只转发 `model`（必须以 `enterprise-pdf-rag/` 开头）、`messages`（每条只保留 `role`/`content`，content 规约为纯文本：数组形式的多段 `text` 按行合并，含图片等非文本部分则 403 而不是悄悄丢弃）、`stream`、`stream_options.include_usage`、`document`、`rerank`；Open WebUI 附加的 `metadata`、`chat_id`、`id`、`session_id`、`params`、`temperature`、`max_tokens` 等一律丢弃，不会变成后端的 422。消息条数上限沿用契约的 32。
- 凭证隔离：relay 只接受携带本地占位 key 的调用（即同进程的 Open WebUI），转发前去掉 `Authorization`；后端不需要 key，浏览器或反代带来的任何 `Authorization` 头都不会到达后端。Open WebUI 的 `/ws/socket.io` 与其余放行路径原样透传。
- SSE 逐块透传、不缓冲；后端 4xx/5xx 保持状态码，其 `detail` 同时复制到 `error.message`（Open WebUI 只显示后者，否则界面只见 "Server Connection Error"）；后端不可达为 503，文案不含地址。

启动示例（后端 8768、UI 3200；默认仍是 8766/8767，两个端口都由环境变量给出，`status` 也要带同样的环境）：

```sh
export APP_INGESTION_DIR=/abs/path/data/ingestion              # 可省略；见测试与入库指南
export EMBEDDING_BASE_URL='<loopback url>' EMBEDDING_MODEL='<model>' EMBEDDING_API_KEY='<key>'
export OPENAI_BASE_URL='<https url>' OPENAI_MODEL='<model>' OPENAI_API_KEY='<key>'
ENTERPRISE_API_PORT=8768 ENTERPRISE_WEBUI_PORT=3200 \
  ./scripts/enterprise_pdf_rag/start.sh --profile document-catalog
ENTERPRISE_API_PORT=8768 ENTERPRISE_WEBUI_PORT=3200 \
  uv run --locked python scripts/enterprise_pdf_rag/webui_preview.py status --profile document-catalog
```

只有 API 子进程继承 `EMBEDDING_*` / `OPENAI_*` / `RERANK_*` / `APP_INGESTION_DIR` / `APP_LEGACY_DOCUMENT_ROOTS` / `APP_ANSWER_MAX_LIVE_CALLS`；Open WebUI 进程只收到构造的环境和占位 key。

### 可选：启用 Open WebUI 自带登录（对外暴露时）

默认 `WEBUI_AUTH=False`（无登录、无注册），与固定 profile 相同。只有 document-catalog profile 可显式 opt-in：`ENTERPRISE_WEBUI_AUTH=1` 让 gate 给 vendor 进程输出 `WEBUI_AUTH=True`，并只透传 `ENABLE_SIGNUP`（默认 `True`；0.6.5 按 `.lower() == "true"` 解析）与 `DEFAULT_USER_ROLE`（默认 `pending`，可选 `user`/`admin`）这两个变量，仍不透传任何模型 key。gate 此时额外放行 `POST /api/v1/auths/signup`。Open WebUI 让**第一个注册的用户成为 admin**（`routers/auths.py:signup`，`user_count == 0` → `admin`），之后的注册按 `DEFAULT_USER_ROLE`；因为 `ENABLE_PERSISTENT_CONFIG=False`，每次启动重读 env，用户表则保存在 `data/open-webui-preview/vendor/webui.db`。

```sh
# 首次：开放注册，浏览器打开 UI 端口注册管理员（表单要求邮箱格式）
ENTERPRISE_WEBUI_AUTH=1 ENTERPRISE_API_PORT=8768 ENTERPRISE_WEBUI_PORT=3200 \
  ./scripts/enterprise_pdf_rag/start.sh --profile document-catalog
# 之后：stop，再以关闭注册重启
ENTERPRISE_WEBUI_AUTH=1 ENABLE_SIGNUP=False ENTERPRISE_API_PORT=8768 ENTERPRISE_WEBUI_PORT=3200 \
  ./scripts/enterprise_pdf_rag/start.sh --profile document-catalog
```

用反代做 HTTP Basic 鉴权会与 Open WebUI 前端自带的 `Authorization: Bearer` 冲突，不要叠加。`status` 只要求 `/v1/models` 里的 id 全部以 `enterprise-pdf-rag/` 开头（空目录也算该 profile）。`start.sh` 仍固定加 `--require-processing`，它只对 AIA profile 生效。直接运行 gate 时同样的端口以参数给出：`webui_gate.py --profile document-catalog --port 3200 --backend-port 8768 --data-dir <dir>`。Compose 目前仍把 profile、只读挂载与健康检查固定在 AIA 上，document-catalog 尚无容器配置。

## 受限边界

Open WebUI 只连接本地 API，并收到固定占位 key；启动器构造完整的子进程环境，不继承用户的云端 API key、数据库、对象存储或模型下载配置。外层 ASGI gate 在 vendor 代码之前拒绝上传、文件/knowledge、内置 retrieval、工具/function、联网搜索、其他模型、未知问题和管理配置写入。旧版 UI 的 title/tag 后台任务会在进入 vendor 前被强制关闭，未知且启用的后台任务仍被拒绝。

默认 AIA 后端只暴露来源审阅模型。未知问题返回 422，其他模型返回 404，错误 snapshot 或来源证据不一致返回 409；不存在 synthetic demo、摘要或模型调用 fallback。这个边界服务于本机审阅，不提供生产鉴权、租户隔离、已验证图表数值关系或通用财务问答。完整原始 ChartIR 只供审阅；仅标签资格的检索视图会清空数值、期间、轴和 marks，并由金融 guard 拒绝数值回答。

## 官方 0.11.3 目标配置

`deploy/enterprise-pdf-rag/open-webui/compose.yaml` 固定官方 Open WebUI 0.11.3 slim 镜像 digest，并运行相同 gate。AIA 后端只读挂载预先生成的 `data/output/aia-2026-interim/`；必须先在主机完成 `ingest-aia`。后端和 WebUI 只通过内部 Compose 网络通信，对主机只暴露 loopback 端口；WebUI 数据使用私有 volume，不挂载主机 HOME、Docker socket 或全局配置。 Compose 要求把 `AIA_REVIEW_UID` / `AIA_REVIEW_GID` 设为产物所有者的数字 UID/GID，以读取权限为 `0600` 的内容寻址对象；缺变量或缺产物目录都会显式失败，不创建空来源或回退合成 demo。

[官方 v0.11.3 Dockerfile](https://github.com/open-webui/open-webui/blob/v0.11.3/Dockerfile#L43) 的运行时基于 Python 3.11；[该版本项目元数据](https://github.com/open-webui/open-webui/blob/v0.11.3/pyproject.toml#L122-L130) 声明支持 Python 3.11/3.12。复制进镜像的 standalone `webui_gate.py` 已按 Python 3.11 grammar 静态解析通过，所用标准库也均存在于 3.11；这项静态检查不替代尚未进行的镜像构建和容器启动。

当前只验证过配置展开：

```sh
export AIA_REVIEW_UID="$(id -u)" AIA_REVIEW_GID="$(id -g)"
docker compose --env-file /dev/null \
  -f deploy/enterprise-pdf-rag/open-webui/compose.yaml config --quiet
```

当前机器没有可用 Docker daemon，因此以下正式启动步骤尚未在本机执行。具备 daemon 后，应先停止 0.6.5 预览以释放相同端口，再执行：

```sh
uv run --locked python scripts/enterprise_pdf_rag/webui_preview.py stop
export AIA_REVIEW_UID="$(id -u)" AIA_REVIEW_GID="$(id -g)"
docker compose --env-file /dev/null \
  -f deploy/enterprise-pdf-rag/open-webui/compose.yaml up --build -d
docker compose --env-file /dev/null \
  -f deploy/enterprise-pdf-rag/open-webui/compose.yaml ps
```

停止 Compose 服务：

```sh
docker compose --env-file /dev/null \
  -f deploy/enterprise-pdf-rag/open-webui/compose.yaml down
```

在构建、健康检查和真实浏览器来源审阅全部成功前，不应把 0.11.3 标为已验证运行。

## 测试政策

离线工程门验证不可变对象、OpenAI 子集 schema、普通/SSE 响应、ASGI gate、隔离环境和 Compose 配置漂移。普通测试使用仓库内的合成小 fixture，不依赖本机绝对路径。真实 AIA adapter 的 7 项语料验收需要本地预置上述固定 SHA-256 PDF：样本缺失时 pytest 明确报告 7 项 skipped，存在时执行完整页覆盖与原生 SVG 检查，存在但摘要错误时失败；测试不会下载或把 PDF 加入仓库。测试不调用付费模型，也不下载 embedding/rerank 模型。

真实页面 smoke 只在 Open WebUI 版本、vendor 请求格式、来源 profile 或 gate 边界发生实质变化时运行。0.6.5 的兼容预览结果不能替代 0.11.3 的首次容器验收。

## 已知本机副作用

首次启动现有 0.6.5 vendor 时，其自身启动代码把 12 个随发行包提供的前端资产复制到 `/opt/anaconda3/lib/python3.12/site-packages/open_webui/frontend/static`。事后核对显示这 12 个目标文件与发行包来源逐字节相同；由于没有启动前 hash 或备份，无法证明同名文件此前没有本机自定义。没有安装或更新全局依赖，也没有改写其他 17 个目标目录独有的字体、Swagger 或 logo 文件。后续运行已把 `STATIC_DIR`、`FONTS_DIR`、数据库和缓存固定到本项目的 `data/open-webui-preview/vendor/`。
