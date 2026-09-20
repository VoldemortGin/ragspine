# 测试与通用 PDF 入库

本文区分已运行的服务、通用入库草稿和完整产品验收。2026-09-19 已在官方 pdfspine 0.11.0 环境通过完整离线门（639 tests）、独立 Python 3.12 plain-pip 安装及 checkout 外 smoke，并受控重启 API/WebUI。实际在线验收包含一次真实 query embedding 搜索、5 条命中、同 snapshot context 回填、第 18 页带引用的 72% 查值和来源审阅聊天；界面可达本身不作为完整 RAG 通过依据。2026-09-20 新增与 `aia-source-review` 并存的 `document-catalog` 服务模式（多文档目录、按文档检索、证据链上的自然语言回答），已离线实现并测试，并在真实 Qwen3 embedder / reranker 与真实答案模型上做过一轮 18 用例验收（AIA 发布 + 合成 PDF，证据 `data/validation/generic-chat-2026-09-20/`）；结果、修复的 BUG-1 与遗留（ISSUE-2 图表召回、ISSUE-3 散文门年份——后者已于 0.14.0 解决）只以 [交接文档](CLAUDE_HANDOFF.md) 为准，本文不重述。

## 现在能测什么

| 入口 | 可测试行为 | 范围与依赖 |
| --- | --- | --- |
| [Open WebUI](http://127.0.0.1:8767) | 选择来源审阅模型，输入 `查看当前文件`、`查看第18页` | 固定到已发布 AIA 样本；审阅已有产物，无模型调用 |
| `GET /v1/aia/pages/18/text` | 原文 span、物理页号、bbox | 来源观测，不是 LLM 摘要或数值关系资格 |
| `GET /v1/processing/status` | 处理状态与当前 ID | 当前 241 个对象、2 个获准数值 claim；不等于全部对象可问答 |
| `POST /v1/queries` | 第 18 页 VONB/1H26/Agency 72%、Partnerships 28% 查值与有序百分点差 | typed ChartQA v1、固定 processing/snapshot/member；无模型调用 |
| `POST /v1/processing/search` | 对已发布 description-only 索引执行查询向量检索 | 已完成受控重启和一次真实 query embedding 烟测，返回 5 条命中；需本地模型服务与匹配的索引配置 |
| `POST /v1/processing/context` | 按有效 hit 回填同一 snapshot 的描述、IR、资格和来源 | 不调用模型；不能把转录/标签资格当财务关系资格 |
| `POST /v1/chat/completions`（`aia-source-review` 模式） | OpenAI 兼容的来源/状态审阅，支持 SSE | 普通财务问答返回 422；该模式不做 RAG 回答 |
| `GET /v1/documents*`、`GET /v1/models`、`POST /v1/chat/completions`（`document-catalog` 模式） | 多文档目录与挂载状态、按文档检索/证据回填、证据链上的自然语言回答（逐字段引用或拒答） | 需另起 `APP_EXECUTION_MODE=document-catalog` 进程，见下文同名节；离线已实现并测试，真实模型验收结论见 [交接文档](CLAUDE_HANDOFF.md) |

第 20 页 typed ChartQA v2 已有工作树实现和候选证据，但不能在新 runtime 验收、发布与激活之前当成当前在线能力。多文档目录/切换与证据链聊天已有离线实现和测试（`document-catalog` 模式），但只有真实模型验收后才能称为已验收；完整图表能力与通用 TableQA 仍未验收——TABLE 成员目前只放行逐字转写 `VERIFIED` 的表，单元格引用只证明原文，不证明行列关系。

本地 `8766` API 当前没有调用者 API key 校验。来源审阅、context、typed ChartQA 不需要 `OPENAI_API_KEY`。查询向量由后端使用独立的 `EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`EMBEDDING_API_KEY`；客户端不提交这些凭证。Open WebUI 厂商进程仍拿不到 embedding/LLM/rerank key。

## 可复制的后端请求

先读取本次运行的身份，避免复制旧 snapshot 到新服务。所有页号均指 PDF 物理页，API 用 1-based 页号。

```sh
curl --fail-with-body http://127.0.0.1:8766/v1/models
curl --fail-with-body http://127.0.0.1:8766/v1/processing/status
curl --fail-with-body http://127.0.0.1:8766/v1/aia/pages/18/text
```

来源审阅聊天仅返回已有原文/状态。将 `stream` 改为 `true` 可测 SSE；它不是一次 LLM 请求。

```sh
curl --fail-with-body http://127.0.0.1:8766/v1/chat/completions \
  -H 'Content-Type: application/json' \
  --data '{"model":"aia-2026-interim-source-review-v1","messages":[{"role":"user","content":"查看当前文件"}],"stream":false}'
```

下面是 2026-09-19 核查时已激活的第 18 页查值请求。若 `status` 的 processing/snapshot 已改变，先在对应发布记录中取新 member ID，不要只替换其中一个 ID。

```sh
curl --fail-with-body http://127.0.0.1:8766/v1/queries \
  -H 'Content-Type: application/json' \
  --data '{
    "kind":"chart",
    "processing_id":"a7384f0c2654d4a2d195e6f119e6e441ef08319af35a7655d8b7a9a099caa8d5",
    "snapshot_id":"f59d230869d5dac6981f0545286b4772a1a9770e63597350f1a19b1a14349703",
    "member_id":"3ded2dd7e682c29dbe874ebb5253db08b426a222d3f2d2be36fd2dfd5f24efc4",
    "operation":"lookup","series":"VONB","period":"1H26","unit":"%",
    "points":[{"point_id":"point-agency","category":"Agency"}]
  }'
```

预期为 `chart-qa-v1` 的 answered 结果，值为 72、单位 `%`，并有字段证据；HTTP 200 本身也可能是业务拒答，须检查 `status` / `refusal_reason`。完整范围与独立 19-case 证据见 [ChartQA 阶段](chart-qa-stage.md)。

搜索请求使用 `status` 返回的 `processing_id`。**它会调用一次配置的本地 embedding 服务**，不执行 rerank 或 LLM；2026-09-19 已用此问题在新进程完成一次实际调用；重复执行会再次请求查询向量。

```sh
curl --fail-with-body http://127.0.0.1:8766/v1/processing/search \
  -H 'Content-Type: application/json' \
  --data '{"processing_id":"a7384f0c2654d4a2d195e6f119e6e441ef08319af35a7655d8b7a9a099caa8d5","query":"Distribution Mix chart","limit":5}'
```

回填时把搜索响应中一个完整 `hits` 元素原样作为 `hit`，与同一响应的 `processing_id` 一起提交：

```json
{
  "processing_id": "<search 响应的 processing_id>",
  "hit": {
    "snapshot_id": "<search hit 的 snapshot_id>",
    "member_id": "<search hit 的 member_id>",
    "score": 0.5
  }
}
```

将该 JSON 中的 `score` 也替换为实际 hit 的值，保存为 `context-request.json`，再执行：

```sh
curl --fail-with-body http://127.0.0.1:8766/v1/processing/context \
  -H 'Content-Type: application/json' --data-binary @context-request.json
```

## 查询向量配置与启动

app factory 在启动时读取既有独立配置，不读 `.env`，不在启动时连接模型。只接受 `127.0.0.1`、`localhost`、`::1` 的 HTTP(S) 地址；需要远端模型时使用已有受管 SSH 隧道。不要把云 LLM key 或任意远端 URL 填进 embedding 设置。模型 fingerprint 和向量维度必须与已发布索引一致，不能启动时重建或替换索引。

通过现有安全配置方式将上述三项环境变量提供给启动器，再运行 `./scripts/enterprise_pdf_rag/start.sh`。启动器仅将 `EMBEDDING_*` 传给 API 子进程，厂商 Open WebUI 环境不继承它们。已有健康进程会被复用；修改代码或配置后，要通过已有 owned-process `stop` / `start` 流程受控重启才能生效，不能以一次 `start` 输出推断配置已经更新。具体命令见 [Open WebUI 使用说明](open-webui.md)。

缺少、部分缺少或无效配置时，来源读取继续工作，search 返回 503；服务连接/响应失败返回 503，不自动重试。错 snapshot、损坏证据或不匹配的模型/维度会被拒绝，不回退合成向量。当前 HTTP 搜索为 cosine 排序，已有 CLI 的 rerank 验收不等于在线搜索含 rerank。该回环配置限制尚未在 Databricks 网络部署中验收，安装通过也不能代替部署通过。

本次上线证据位于被 Git 忽略的 `data/validation/rollout-search-2026-09-19/`：`baseline.json`、`rollout.json`、`smoke-summary.json`、各接口原始响应和 `post-runner-liveness.json`。原始 PDF 和 source/processing current 指针的 SHA 前后相同；没有文档重算 embedding、LLM 或 rerank 调用，第 20 页未激活。两个验收 runner 退出后，managed tunnel、API 和 WebUI 仍运行；API 保留独立 embedding 配置，厂商 WebUI 和日志不包含该 key。

如使用项目既有 SSH 隧道与容器 key 读取器，可在当前 shell 通过安全运维配置设置以下变量，再运行现有管理脚本；占位值必须替换为自己的已授权配置，任何 key 都不写进命令或文件：

```sh
export LOCAL_MODELS_SSH_HOST='<SSH user@host>'
export LOCAL_MODELS_SSH_PORT='<SSH port>'
export EMBEDDING_LOCAL_PORT='<local embedding port>'
export EMBEDDING_REMOTE_PORT='<remote embedding port>'
export RERANK_LOCAL_PORT='<local rerank port>'
export RERANK_REMOTE_PORT='<remote rerank port>'
export EMBEDDING_MODEL='<model matching the published index>'
export RERANK_MODEL='<configured rerank model>'
export EMBEDDING_REMOTE_CONTAINER='<existing embedding container>'
export RERANK_REMOTE_CONTAINER='<existing rerank container>'
export OPEN_WEBUI_PYTHON='/path/to/python3.12-with-open-webui-0.6.5'

.venv/bin/python scripts/enterprise_pdf_rag/local_model_tunnel.py status
# 仅在确认没有运行中的本项目隧道、也没有 stale PID 记录时启动
.venv/bin/python scripts/enterprise_pdf_rag/local_model_tunnel.py start
.venv/bin/python scripts/enterprise_pdf_rag/with_local_models.py -- \
  env OPEN_WEBUI_PYTHON="$OPEN_WEBUI_PYTHON" ./scripts/enterprise_pdf_rag/start.sh
```

已有健康隧道时跳过 `start`；stale PID 或被占用端口须先核对所属进程，不能删除记录来跳过所有权检查。`with_local_models.py` 的现有契约需要两组模型配置并只读取得两组 key，但不会调用任一推断接口；启动器仍只把 embedding 配置传给 API，WebUI 没有上游 key。wrapper 退出不停止长期运行的受管服务。修改配置需要先用现有 `webui_preview.py stop` 停止本项目旧进程，再启动新进程；不能凭模型发现 200 判定配置已更新。

## 通用 PDF 入库命令与 Python API

安装后使用公开命令，不要求运行时安装 uv。以下命令默认只做来源处理，适合先核对新文档的可提取性：

```sh
enterprise-pdf-rag ingest --pdf /path/to/document.pdf --pages 1-3,5
enterprise-pdf-rag ingest --pdf /path/to/document.pdf --pages all \
  --output-dir /path/to/ingestion-output --stage source --max-live-calls 0
```

仓库内等价入口为 `uv run --locked enterprise-pdf-rag ingest ...`，或者在已安装包的 Python 环境中执行 `python scripts/enterprise_pdf_rag/ingest.py ...`。从 checkout 外运行时遵守既有安装契约：`APP_ROOT_DIR` 指向存在的工作目录，`APP_DATA_DIR` 可指定持久数据目录；相对输入和 `--output-dir` 相对命令当前工作目录解析。不要把脚本所在目录当成输入路径基准。

```sh
APP_ROOT_DIR=/existing/workdir APP_DATA_DIR=/existing/workdir/data \
  enterprise-pdf-rag ingest --pdf /absolute/path/document.pdf --pages all
```

Python 入口调用同一条流水线：

```python
from pathlib import Path
from enterprise_pdf_rag.adapters.pdf_ingestion import ingest_pdf

result = ingest_pdf(
    pdf=Path("/path/to/document.pdf"),
    pages="1-3,5",
    output_dir=Path("/path/to/ingestion-output"),
    stage="source",
    max_live_calls=0,
)
print(result.model_dump_json(indent=2))
```

`pages` 支持 `all` 或 1-based 范围/列表，如 `1-3,5`；重复、重叠、倒序和越界会拒绝。source manifest 始终保存完整文件和全部来源页；选页仅影响下游 Canonical/layout/semantics。不同 PDF bytes 按 SHA 分目录；已有正确源缓存可复用，缺失/损坏缓存拒绝，不覆盖当前发布。

| 参数/结果 | 含义 |
| --- | --- |
| `--stage source` | 默认；完整来源提取和选页 Canonical，layout/semantics deferred，无模型配置和调用 |
| `--stage layout` | 显式运行/回放模型布局；对象语义仍 deferred |
| `--stage semantics` | 同一客户端依次布局并生成独立 IR/description 分支；图表资格策略为 `none` |
| `--max-live-calls N` | layout 和两条语义分支共享的新请求上限；默认 0 仅复用缓存，不自动获取正预算 |
| `source_store` / `processing_store` | 实际磁盘 store 根目录，可交给后续显式索引/发布流程 |
| `source_manifest_id` / `processing_id` | 不可变 draft 身份；不能冒充当前服务已加载的身份 |
| `source_page_count` / `selected_physical_pages` | 全源页数与下游实际选页 |
| `source_cached` / `live_call_count` | 是否复用已有来源缓存、实际新模型请求数 |
| `failed_stage_count` / `semantic_status` / `review_path` | 查看部分失败、deferred 或 unavailable 的具体结果；CLI 返回 JSON 不代表全部阶段成功 |
| `activated` / `indexed` | 此命令始终为 `false`；没有索引构建、发布或服务切换 |
| `retrieval_status` | 明确 `not_ready`；资格、索引和发布需要独立流程 |

layout/semantics 即使 `--max-live-calls 0` 也需提供 `OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_API_KEY`，以固定同一 provider/cache 身份；配置加载不连接模型。只有明确选择正预算才允许新请求，预算耗尽或缓存缺失体现在阶段诊断。入库从不执行 embedding/rerank。`source` 阶段若附带正预算会拒绝，避免把默认提取误当成推断。

坐标比较的规范化规则：layout / semantics 模型收到的是 canonical 坐标的全精度 repr，回传时常写成最短小数（`42.400000000000006` → `42.4`、`307.9999999999998` → `308`），因此所有"模型给出的区域框是否包含 canonical 图元（span / 原生表格网格 / 页面几何）"的判定统一走 `processing/geometry.py` 的 `contains(outer, inner, tolerance=1e-6)`，外框每边放宽 1e-6 pt——远高于浮点渲染噪声（< 1e-12 pt）、远低于任何真实版面偏移，所以差 0.5 pt 的越界 span 仍被拒绝；canonical 与 canonical 之间的比较（cell 中心、cell ⊂ 网格）不放宽。

新入口解除 AIA 文件名/SHA/页数限制，不承诺支持所有 PDF 变体。现有 pdfspine 来源适配器会对旋转页、非默认 CropBox/MediaBox 坐标等未验证情况 fail closed；失败应按诊断处理，不能回退 OCR、其他 parser 或合成文本。来源 HTML 位于返回的 `source_store/review.html`；处理 HTML 使用 `review_path`。这些是本地独立产物，不会自动出现在当前 Open WebUI 来源模型中。

## draft 的资格 / 索引 / 发布命令

`ingest` 产出的 draft 由三条独立显式命令推进，全部按 `ingest` 返回的 store 根与 `processing_id` 工作，三者共享 `--source-store/--processing-store/--processing-id`：

```sh
enterprise-pdf-rag qualify --source-store <src> --processing-store <proc> --processing-id <id>
enterprise-pdf-rag index   --source-store <src> --processing-store <proc> --processing-id <id> [--document-label <名称>]
enterprise-pdf-rag publish --source-store <src> --processing-store <proc> --processing-id <id> [--no-activate-source]
```

| 命令 | 作用与状态推进 | 是否调模型 | 写盘 / 切指针 |
| --- | --- | --- | --- |
| `qualify` | 只读统计资格：`eligible_member_count`/`skipped_object_count`/`chart_member_count`/`kinds`/`skipped_reasons`；`retrieval_status` → `qualified; indexing pending` | 否（零模型） | 不写盘、不切指针 |
| `index` | description-only 构建检索快照；`retrieval_status` → `indexed; publication pending`；`--document-label` 缺省用 source manifest filename | 是，一次 embedding（生产 `LocalEmbeddingAdapter`；需匹配已发布 fingerprint/维度的本地服务） | 产新不可变 snapshot，`update_current=False` 不切指针 |
| `publish` | 原子切 `current-processing`；默认 `--activate-source` 再切 `current-manifest`，`--no-activate-source` 只切 processing；`retrieval_status` → `ready`；未 `index` 的 draft 拒绝 `ValueError`；内容寻址幂等 | 否 | 原子替换 current 指针（幂等） |

三命令错误统一输出 `{"error": ...}` 并以退出码 1 fail closed。完整生命周期为 ingest 的 `not_ready` → `qualified; indexing pending` → `indexed; publication pending` → `ready`。

### 离线验证 vs 真实验证

离线 E2E `tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py` 已用非 AIA 程序化三页财务 PDF `meridian-semiannual.pdf` 覆盖 ingest→qualify→index（`OfflineDescriptionEmbedder`，dims 64）→publish→`search`/`resolve`：命中带 snapshot_id/member_id，retrieval snapshot 的 `scope.source_manifest_id` 与 ingest 一致，另有 `cli.main` 三命令 JSON 状态推进 smoke；单元测试见 `tests/enterprise_pdf_rag/adapters/test_draft_publication.py`，`bash scripts/ci.sh` 随此全绿。真实 `qualify`/`publish` 已对真实 AIA store（`data/output/aia-2026-interim`）只读跑通并幂等（eligible=189、`publish` 回到同一 `a7384f0c`、dims [2560]）。但真实 `index` 需要本地 embedder，当前 shell 无隧道配置（`scripts/enterprise_pdf_rag/with_local_models.py` 报 `TunnelConfigurationError: Missing or invalid setting: LOCAL_MODELS_SSH_HOST`），真实链路 index 仍未覆盖，须在项目受管 SSH 隧道环境运行；通用 `ingest` 亦从未对真实 PDF 跑过。

## document-catalog 模式：多文档目录、按文档检索与证据链聊天

`APP_EXECUTION_MODE=document-catalog` 是与 `aia-source-review` 并存的第二个显式服务模式（[ADR 0011](adr/0011-document-catalog-and-verified-answer-chain.md)）。它扫描入库根下每个 `<sha256>/{source,processing}` 目录及其 `current-*` 指针，把 `retrieval_status == ready` 的文档按 pinned processing id / retrieval snapshot id 挂载；`not_indexed` / `corrupt` 的文档只在目录里可见并带原因。挂载不调用模型；每个请求都重新读取 pinned manifest，任何漂移都 409。

### 启动

所有 provider 都从各自独立的环境变量组显式构造一次；缺一组只让依赖它的路由返回 503，不回退 mock。

```sh
export APP_EXECUTION_MODE=document-catalog
export APP_INGESTION_DIR=/abs/path/data/ingestion   # 可省略；默认 APP_DATA_DIR/ingestion，即 ingest 的默认输出
# 可选：原地挂载 AIA 发布。每项是 processing store 根，其父目录即 source store 根；JSON 列表，默认空
export APP_LEGACY_DOCUMENT_ROOTS='["/abs/path/data/output/aia-2026-interim/pages-001-020"]'
# 检索 vector 通道：缺失 → 文档仍 mounted=true 但 embedding_configured=false，search 与 chat 503
export EMBEDDING_BASE_URL='<loopback url>' EMBEDDING_MODEL='<model matching the index>' EMBEDDING_API_KEY='<key>'
# 聊天合成：缺失 → /v1/chat/completions 503（/v1/models、/v1/documents* 照常）
export OPENAI_BASE_URL='<https url>' OPENAI_MODEL='<model>' OPENAI_API_KEY='<key>'
# 可选 rerank：只在请求显式 "rerank": true 时使用；缺失时该请求 503
export RERANK_BASE_URL='<loopback url>' RERANK_MODEL='<model>' RERANK_API_KEY='<key>'
export APP_ANSWER_MAX_LIVE_CALLS=200                 # 进程内模型真实调用预算；缓存回放不计，用尽即 503

enterprise-pdf-rag serve --host 127.0.0.1 --port 8766
```

embedding 指纹必须与已发布索引一致，否则该文档拒绝挂载（`mounted=false`，`mount_error` 给出原因），不会重建索引、不调用模型。回环 URL 限制与 [本地模型说明](local-models.md) 的隧道方式不变；任何 key 都不写进命令或文件。

### 目录与按文档检索（契约 `document-catalog-v1`）

```sh
curl --fail-with-body http://127.0.0.1:8766/v1/documents
```

响应字段：`schema_version`、`ingestion_root`、`legacy_roots`、`embedding_configured`、`embedding_fingerprint`、`unpublished`（有目录但无发布指针的 sha）以及 `documents[]`；每项是目录条目（`document_id` 即 PDF 的 SHA-256、`origin` `ingestion`/`legacy`、`retrieval_status`、`reason`、`current_processing_id`、`retrieval_snapshot_id`、`member_count`、`embedding_fingerprint`、`document_label`、`source_page_count`、`selected_physical_pages`、`source_activated`）加本进程的 `mounted` / `mount_error`。列目录不调用模型。

```sh
DOC=<document_id>
curl --fail-with-body http://127.0.0.1:8766/v1/documents/$DOC            # 条目 + 处理状态；未挂载时 status 为 null
curl --fail-with-body http://127.0.0.1:8766/v1/documents/$DOC/manifest   # pinned processing manifest
curl --fail-with-body http://127.0.0.1:8766/v1/documents/$DOC/search \
  -H 'Content-Type: application/json' --data '{"query":"Distribution Mix chart","limit":5}'
```

search 恰好调用一次 query embedding，返回 `{"document_id","processing_id","snapshot_id","hits":[{"snapshot_id","member_id","score"}]}`。把一个完整 hit 原样回填：

```sh
curl --fail-with-body http://127.0.0.1:8766/v1/documents/$DOC/context \
  -H 'Content-Type: application/json' \
  --data '{"hit":{"snapshot_id":"<hit.snapshot_id>","member_id":"<hit.member_id>","score":0.5}}'
```

context 不调用模型，返回与 `/v1/processing/context` 相同结构的 `RetrievalContext`（描述、typed IR、资格回执、来源 SVG 引用）；hit 不属于该文档的 snapshot → 409。

### 证据链聊天（契约 `rag-chat-v1`）

```sh
curl --fail-with-body http://127.0.0.1:8766/v1/models
```

每个已挂载文档一个 model：`id` 为 `enterprise-pdf-rag/<sha256 前 12 位>`，`name` 为 `<document_label> (<sha12>)`，`owned_by` 为 `enterprise-pdf-rag/document-catalog`。

```sh
curl --fail-with-body http://127.0.0.1:8766/v1/chat/completions \
  -H 'Content-Type: application/json' \
  --data '{
    "model": "enterprise-pdf-rag/<sha12>",
    "messages": [{"role": "user", "content": "What does page 2 say about the outlook?"}],
    "stream": false,
    "document": null,
    "rerank": false
  }'
```

文档选择优先级：`document`（完整 sha256 或 ≥12 位十六进制前缀）> `model` 形如 `enterprise-pdf-rag/<sha12>` > 目录里唯一一个已挂载文档；多文档且未指定 → 422。最后一条消息必须是 `user`；之前的 `user`/`assistant` 轮作为数据进入 prompt，客户端 `system` 消息被丢弃。请求 `extra=forbid`，`temperature` 等未声明字段 → 422。`rerank` 默认 `false`。

响应是标准 `chat.completion`，`choices[0].message.content` 为回答正文加 `引用:` 编号列表，并多一个 `enterprise_pdf_rag` 信封（下例只示意字段；值以实际响应为准）：

```json
{
  "id": "chatcmpl-…", "object": "chat.completion", "created": 1758000000,
  "model": "enterprise-pdf-rag/<sha12>",
  "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
    "content": "Page 2 reads: <verbatim span text>\n\n引用:\n[1] p.2 fragments.<span_id>: “<verbatim span text>”"}}],
  "enterprise_pdf_rag": {
    "schema_version": "rag-chat-v1",
    "status": "answered",
    "abstain_reason": null, "abstain_detail": null,
    "document_sha256": "<sha256>", "processing_id": "<pinned>", "snapshot_id": "<pinned>",
    "member_ids": ["<member_id>", "…"],
    "claims": [{"claim_id": "q1", "kind": "quote", "text": "<verbatim span text>", "value": null, "unit": null,
      "citations": [{"member_id": "<member_id>", "kind": "text", "page_index": 1,
        "field_path": "fragments.<span_id>", "evidence_ids": ["<span_id>"],
        "bbox": [x0, y0, x1, y1], "quote": "<verbatim span text>", "chart_citation": null}]}],
    "rejected": [],
    "llm_live_calls": 1, "cache_hit": false
  }
}
```

`claims[].kind` ∈ `quote` / `cell` / `chart_value`；图表值的 `text` 是来源显示串（如 `72%`）、`value` 是十进制字符串、`unit` 是单位，正文引用写作 `[n] p.N points.<point_id>.value = 72% (svg #<element>, …)`。`citations[].kind` 是证据块类型（`text`/`list`/`group`/`table`/`chart`），`page_index` 为 0-based（正文 `p.N` 为 1-based），`field_path` 是 `fragments.<span_id>` / `cells.<cell_id>` / `points.<point_id>.value`，`evidence_ids` 是来源 span 或 SVG 元素 id，`chart_citation` 只有图表值有。`rejected[]` 列出被逐条剔除的 claim（`claim_id`/`member_id`/`field_path`/`text`/`reason`/`detail`），供审计。`llm_live_calls` 是本次真实模型调用数（0 或 1），`cache_hit` 表示指纹命中缓存回放。

拒答也是 200：信封 `status` 为 `abstained`，`content` 为 `无法基于已验证证据回答 (<abstain_reason>): <abstain_detail>`，`claims` 为空。判定顺序：无检索命中或预算内无证据块 → `no_relevant_member`；模型自报 abstain → `model_declined`；模型输出不合 schema/截断 → `model_output_invalid`；逐条校验失败的 claim 进入 `rejected`；零验证 claim → 取第一条 rejected 的原因（否则 `no_verified_claim`）；散文里的数字既不属于已验证 claim 的 text / value、也不逐字出现在用户问题里、也不在已验证 claim 所引用证据原文（span quote / cell 原文 / 图表 period、category 标签与 source_display）中 → 整体 `claim_not_in_evidence`（0.14.0 起；此前任何不在 claim 内的数字都拒答）。图表类 refusal（`value_unavailable`、`period_mismatch` 等）与 ChartQA 同名。

`"stream": true` 时先完成检索、模型调用与校验，再把正文按 128 字符切片以 SSE 回放：首帧 `delta: {"role":"assistant"}`，倒数第二帧 `finish_reason: "stop"`，最后一帧 `choices: []` 且带完整 `enterprise_pdf_rag` 信封，然后 `data: [DONE]`；`usage` 恒为 `null`。

### 状态码

| 状态 | 触发 |
| --- | --- |
| 200 | 已回答或业务拒答（看信封 `status`） |
| 404 | 未知 `document_id`；`document`/`model` 引用不匹配任何目录条目 |
| 409 | 目录可见但未挂载（`mounted=false`，附原因）；pinned manifest 漂移或证据损坏；hit 不属于该 snapshot；`ChartQueryError` `INVALID_EVIDENCE` / `PIN_CONFLICT` |
| 422 | 文档引用前缀歧义；多文档未指定；最后一条不是 `user`；空问题等 `AnswerRequest` 不变量；未声明字段 |
| 503 | `OPENAI_*` 未配置（chat）；`EMBEDDING_*` 未配置或 provider 失败（search / chat）；请求 `rerank` 但未配置 `RERANK_*`；模型传输失败或 `APP_ANSWER_MAX_LIVE_CALLS` 用尽（`DependencyUnavailable`）；`ChartQueryError` `UNAVAILABLE_EVIDENCE` |

错误文案固定，不含凭证、provider 响应体或磁盘路径。

### 缓存与重试

`JsonCompletionClient` 以请求指纹（盐 `bounded-text-json-v1`）缓存到 `<ingestion_root>/model-cache/`；同一文档、同一问题、同一上下文的重复请求回放缓存（`llm_live_calls=0`、`cache_hit=true`）。客户端以 `retry_failed=False` 构造：真实调用失败也会被缓存并原样回放；要重试须删除 `<ingestion_root>/model-cache/requests/<fingerprint>.json`。这是有意为之，没有自动重试。

### 离线可测 vs 需真实模型

离线（默认门，零网络）：`tests/enterprise_pdf_rag/adapters/test_document_catalog.py`、`test_documents_http.py`、`test_hybrid_search.py`、`test_chat_http.py`，`tests/enterprise_pdf_rag/answers/`（store 桥 `store_mounted_document.py` + 脚本化 LLM `fake_llm.py`），`processing/test_context_builder.py`、`processing/test_table_transcription.py`，以及 e2e / draft publication / pdf ingestion 里新增的程序化表格页用例。它们用程序化 PDF、`OfflineDescriptionEmbedder` 和脚本化模型输出，证明契约、状态码、恰好一次模型调用、逐字段校验与拒答策略。

需真实模型：真实本地 embedder 的 `index` 与在线 search（隧道）、真实答案模型的合成与校验、`APP_LEGACY_DOCUMENT_ROOTS` 挂载真实 AIA 发布后的检索 / 引用 / 拒答验收。2026-09-20 已做一轮（18 用例，无证据外数字进入 answered 回答；图表召回 ISSUE-2 待定，散文门年份 ISSUE-3 已于 0.14.0 解决），结论只以 [交接文档](CLAUDE_HANDOFF.md) 为准，本文不作宣称；它是一轮验收，不是冻结金标集。

## 通用性与完整 RAG 的完成条件

产品目标面向不同文档。AIA 的公开来源、SHA、前 20 页 gold 和数值资格只是一个可复现的验收样本；它们不应该成为任意 PDF 入库的文件名、页数或业务规则前置条件。当前 API 的 `/v1/aia/*` 路径、来源审阅 model ID 和默认存储仍属于这一兼容 profile。

通用入库的来源资产保存、layout/semantics、description 资格、embedding 索引、发布/服务挂载、自然语言回答是不同阶段。只有来源入库成功时，不能宣称新文档已可检索或聊天。截至 2026-09-20，这条链的每一段都有代码与离线测试：证据与资格链（含逐字转写 `VERIFIED` 的 TABLE 成员）、显式 `qualify`/`index`/`publish`、`document-catalog` 模式的按文档服务入口、hybrid 检索 → 一次模型调用 → 逐字段校验的回答链，以及文本 / 表格单元格 / 图表值的正例、拒答、引用与损坏证据用例（[ADR 0011](adr/0011-document-catalog-and-verified-answer-chain.md)）。尚未完成的是：真实模型上的持续验收（2026-09-20 已做一轮，结论与遗留 ISSUE-2 / ISSUE-3 见 [交接文档](CLAUDE_HANDOFF.md)，本文不替它下结论）、自然语言问答的冻结金标集、Open WebUI 网关对 `document-catalog` 模式的接入、`backend.Dockerfile` 复验，以及第 20 页 v2 的独立验收。在这些完成前，不能称通用聊天已验收。
