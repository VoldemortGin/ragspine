# Claude 交接：通用文档 RAG 与公开样本验收

更新时间：2026-09-20

> 阅读顺序：先看下方“恢复开发记录”；后续旧暂停快照保留作证据，不能作为实时发布或服务状态。

## 会话收尾状态（2026-09-20，下一 session 从这里接手）

> **重启前接手清单（2026-09-20 深夜，Claude Code 重启前写）**
> 1. 工作树未提交（~45 个文件）：第 2 项 ①②③ + 第 3 项 1–4 + bbox 容差修复（`processing/geometry.py`）+ `webui_gate` 的 `document-catalog` profile/relay + 自带登录 opt-in（`ENTERPRISE_WEBUI_AUTH=1`）。最后一次 `pytest tests/enterprise_pdf_rag` **785 passed**、mypy 448 files 零错误、四个 check 与 `check_doc_drift` 全绿；**完整 `bash scripts/ci.sh` 尚未整体跑绿**（曾为修 bug 中断）。
> 2. 用户已授权：完整门全绿后把所有分支合并到 `main`（`merge/enterprise-pdf-rag` 已含于 main，实际只合 `feat/generic-document-service`），然后 commit + push（main 与 feature 都推）。**PyPI 发版未授权。**
> 3. 之后：以 `document-catalog` profile 起后端 8768 + Open WebUI **3200**（命令见 `open-webui.md` "document-catalog profile" 与"可选：启用 Open WebUI 自带登录"），首次开放注册建管理员（Name `linhan`，Email 需邮箱格式如 `linhan@local.test`，密码由用户口头给出、不写入文档），再以 `ENABLE_SIGNUP=False` 重启；从界面问几个 AIA 与合成 PDF 的问题并截图。
> 4. 公网暴露：frps 未配 `vhostHTTPPort`，frp 原生 basic auth 不可用；frpc（`/opt/homebrew/bin/frpc` v0.65.0，配置 `~/.config/frp/frpc.toml`，LaunchAgent `com.frpc`，无 admin API）只能重启生效，会让本机全部隧道含公网 SSH 6001 瞬断，动之前告知用户。决定：frp `type="tcp"` 把 3200 映到 `106.15.8.67:13200`（云安全组需放行），鉴权靠 Open WebUI 自带登录（frp basic auth 会与前端 `Authorization: Bearer` 冲突）。完整片段/回滚在 `data/local-models/frp-expose-3200.md`（gitignore）。
> 5. 真实模型 env（含 key）在 `data/local-models/local-models.env`（gitignore，600）；隧道 PID 78200（39002/39001）在跑；8766/8767 是旧服务别 kill；`webui_preview.py` 的 `processes.json` 可能拒绝与旧服务并存。

**仓库状态**：分支 `feat/generic-document-service`，HEAD `f0ca40d`（其前一提交 `2a7d1c1` 含第 2 项阶段 ① 与第 3 项阶段 1）。本节对应的工作树含**未 commit** 的第 2 项阶段 ②③ 与第 3 项阶段 2–4 全部代码、测试与文档（清单以 `git status --short` 为准：代码侧 17 个修改文件 + 15 个新增文件/目录，文档侧本节所列）；`main` 与本分支均未 push。提交前先核对用户授权与实际工作树。

**本 session 验证（工作树，各实现 agent 分别跑）**：`pytest tests/enterprise_pdf_rag -q` **758 passed**（BUG-1 修复前 752，文档收尾 agent 复跑一致；修复后 +6）；`mypy` 447 文件零错误；`ruff check` / `ruff format --check` 通过；`check_conformance` / `check_architecture`（含 `answers`）/ `check_schema`（8 份契约）/ `check_drift` 通过；`scripts/check_doc_drift.py` 23 tracked / 0 stale（`src/enterprise_pdf_rag/CLAUDE.md` 已 bump 到 `f0ca40d`）。完整 `bash scripts/ci.sh` 本 session **未整体跑绿**：文档 bump 前它在第 2 步（doc drift）停止，其余步骤未跑；下一 session 起手先跑一遍。

**设计方案与决定记录**：函数级设计 `plans/item2-document-catalog-and-service-mount.md`、`plans/item3-answer-chain.md`，合并调研 `plans/merge-into-ragspine-investigation.md`，路线汇报页 `rag-ingestion-brief.html`；决定、被拒方案与遗留统一记在 [ADR 0011](adr/0011-document-catalog-and-verified-answer-chain.md)；可复制的启动 env、curl、响应信封与状态码表在 [测试与入库指南](testing-and-ingestion.md) 的“document-catalog 模式”节。

**已拍板的决定**（与 ADR 0011 一致）：`MountedDocument` 含 `search/resolve/manifest/member_texts/chart_context/displayed_context`；claim 校验失败走“逐条剔除 + 散文数值门”（散文里任何数字不在已验证 claim 内 → 整体拒答 `CLAIM_NOT_IN_EVIDENCE`；零验证 claim → 拒答并取第一条 rejected 的原因）；rerank 默认关、显式开关；TABLE 成员只放行逐字转写 `VERIFIED` 的并把资格 policy 升 `-v2`；AIA 样本用 `APP_LEGACY_DOCUMENT_ROOTS` 兼容（默认空）；`execution_mode="document-catalog"` 与 `aia-source-review` 并存；`ragspine` 只能在 `enterprise_pdf_rag/adapters/` 下 import（conformance 白名单未改）。

**第 2 项（文档目录与服务挂载）— 阶段 ①②③ 完成**
- 阶段 ①（已在 `2a7d1c1`）：`core/settings.py`（`document-catalog` 模式、`ingestion_dir`/`APP_INGESTION_DIR`、`legacy_document_roots`/`APP_LEGACY_DOCUMENT_ROOTS`、`ingestion_root`）；`adapters/pdf_ingestion.py` 默认根改 `ingestion_root`；`adapters/document_catalog.py`（`CatalogEntry`、`DocumentCatalog`、`scan_catalog`、`MountedDocument`、`mount_document(entry, *, embedder)`、`MountedCatalog`、`mount_catalog`、`QueryEmbeddingUnavailable`）；测试 `test_document_catalog.py`（15）、`test_document_catalog_aia_smoke.py`（真实 store 只读）、共享 fixture `generic_publication_helpers.py`。
- 阶段 ②（本 session，未 commit）：`adapters/http/catalog_schemas.py`（契约 `document-catalog-v1`：`DocumentListItem` = `CatalogEntry` + `mounted`/`mount_error`、`DocumentListResponse`、`DocumentDetailResponse`、`DocumentSearchRequest/Response`、`DocumentContextRequest/Response`）；`adapters/http/documents.py`（`create_documents_router(mounted)` / `create_documents_app(catalog, *, embedder, llm=None, reranker=None)`；`GET /v1/documents`、`GET /v1/documents/{id}`、`/manifest`、`POST .../search`、`POST .../context`；未知 id 404、可见未挂载 409 附原因、`QueryEmbeddingUnavailable`/`ProviderRequestError` 503 固定文案、每请求 `mount.manifest()` 重校验）；`app.py::create_configured_app` 新增 `document-catalog` 分支（`scan_catalog(settings.ingestion_root, legacy_roots=...)`，`LocalEmbeddingAdapter` 显式构造一次共享，缺 `EMBEDDING_*` → embedder=None；`aia-source-review` 分支原样）；`check_schema.py` 登记；`docs/enterprise-pdf-rag/schemas/document-catalog-v1.json`；测试 `test_documents_http.py`（14）。注意 FastAPI 0.141 `app.routes` 里 `include_router` 是惰性 `_IncludedRouter`，测试用 `_route_paths` 展开。
- 阶段 ③（本 session）：文档——`src/enterprise_pdf_rag/CLAUDE.md`（目录树、Run、Invariants、`verified-against`）、ADR 0011、`testing-and-ingestion.md`、本文件，以及 `docs/enterprise-pdf-rag/README.md`、根 `CLAUDE.md`/`README.md` 各一两行。

**第 3 项（证据链上的自然语言回答）— 阶段 1–4 完成**
- 阶段 1（已在 `2a7d1c1`）：`answers/ports.py`（`MemberText`、`MountedDocument` Protocol）；`adapters/hybrid_search.py`（`LexicalIndex` 按快照 id 内容寻址、`build_lexical_index`、`lexical_rank`、`fuse`、`HybridSearch.search`、`RerankPort`/`LocalRerankJudge`；精确复用 `ragspine.retrieval.lexical.retrieval.{tokenize,bm25_scores,rrf_fuse}` 与 `ragspine.retrieval.rerank.listwise_rerank.{ListwiseJudge,listwise_rerank}`）；`processing/context_builder.py`（`ContextBlock.prompt_text()`、`SpanEvidence/CellEvidence/ChartFieldEvidence`、`build_context_block`、`budget_blocks` 整块丢弃）；测试 `test_hybrid_search.py`（10）、`test_context_builder.py`（8，含阶段 4 新增 1）。
- 阶段 2（本 session）：`answers/models.py`（`AnswerStatus`、`ClaimKind`、`AbstainReason`、`from_refusal`、`FusedHit`（自 `hybrid_search` 迁入并回导）、`AnswerRequest(question, document_sha256=None, top_k=6, channel_limit=20, rerank=False, history=())`、`ClaimCitation`、`VerifiedClaim`、`RejectedClaim`、`AnswerResult(status, answer, claims, rejected, abstain_reason, abstain_detail, document_sha256, processing_id, snapshot_id, member_ids, fused, request_fingerprint, llm_live_calls, cache_hit)`）；`answers/prompt.py`（`ModelClaim`/`ModelAnswer` strict pydantic、claims ≤16、`SYSTEM_RULES`、`build_prompt`）；`answers/verify.py`（`ClaimVerification`、`verify_claims`、`_verify_quote/_verify_cell/_verify_chart_value`（bar 走 `chart_context()`+`check_displayed_evidence`，donut 走 `check_context`/`check_fields`，`Decimal` 相等或来源显示串相等，永不派生）、`prose_grounded`、`decide`）；`adapters/json_completion.py::complete_text_json`（独立指纹盐 `bounded-text-json-v1`，可选 `system`）；`adapters/answer_service.py`（`AnswerSettings(rrf_k=60.0, prompt_budget_chars=18_000, max_output_tokens=1024)`、`AnswerService(documents, llm, *, settings, reranker, index_cache)`、恰好一次 LLM、异常 `UnknownDocument`/`AmbiguousDocument`/`DependencyUnavailable(.code)`）；`check_architecture.py` 把 `enterprise_pdf_rag.answers` 加进 `PACKAGES` 并用 `EXTRA_ALLOWED` 只放行 pydantic；测试 `tests/enterprise_pdf_rag/answers/`（`store_mounted_document.py` 测试桥、`fake_llm.py`、`test_verify.py` 12、`test_answer_service.py` 10）。
- 阶段 3（本 session）：`adapters/http/chat_schemas.py`（契约 `rag-chat-v1`：`RagChatRequest(model, messages 1..32, stream, stream_options, document, rerank; extra=forbid)`、`ClaimCitationOut/ClaimOut/RejectedClaimOut`、`AnswerEnvelope`、`RagCompletionResponse`、`RagCompletionChunk`）；`adapters/http/chat.py`（`create_chat_router(mounted, service|None)`：`GET /v1/models` 每个已挂载文档一个 `enterprise-pdf-rag/<sha256[:12]>`；`POST /v1/chat/completions` 选文档 `document` > `model` > 目录唯一，最后一条须 `user`，历史轮作数据、client `system` 丢弃，`content` = 回答 + `引用:` 列表 + `enterprise_pdf_rag` 信封，拒答 200，`stream=true` 先算完再切片、末帧 `choices: []` + 信封再 `[DONE]`；状态码见指南）；`app.py` 分支显式构造 `JsonCompletionClient(load_llm_config(), cache_dir=ingestion_root/"model-cache", max_live_calls=settings.answer_max_live_calls)`（缺 `OPENAI_*` → chat 503，不静默 mock）与 `RERANK_*` 齐时的 `LocalRerankJudge(LocalRerankAdapter(...))`；`core/settings.py::answer_max_live_calls`（`APP_ANSWER_MAX_LIVE_CALLS`，默认 200）；`check_schema.py` 登记；`docs/enterprise-pdf-rag/schemas/rag-chat-v1.json`；测试 `test_chat_http.py`（14）。
- 阶段 4（本 session，TABLE 放行）：`processing/table_transcription.py`（纯规则 `table_span_ids` / `check_table_transcription`，producer 与 validator 共用）；`adapters/source_objects.py::source_table_description()`；`semantic_objects.py::_table` 提取后尝试转写，失败保留 ir、description/qualification 两 stage `UNAVAILABLE` 带原因；`literal_qualification.py` TABLE 分支；`processing_retrieval.py::eligibility` 收 TABLE（非 VERIFIED 理由 `Table transcription is not verified; only verified tables are retrievable`），`_POLICY` → `source-transcription-and-scoped-chart-qualification-v2`（进 `RetrievalPlan.qualification_policy` 与 snapshot_id 哈希）；`draft_publication.py::DraftQualification.qualification_policy` → `retrieval-eligibility-kind-and-stage-completeness-v2`；`context_builder.py` TABLE 块 verification 取 description。测试：`processing/test_table_transcription.py`（3）、`test_pdf_ingestion.py::authored_pdf(table_page=)`、e2e +2、`test_draft_publication.py` +2、`test_context_builder.py` +1、`test_verify.py` +1。多 span cell 按 span 顺序拼接，顺序不同则拒绝而非误验。

**偏离计划与理由**
- **TABLE 的 “VERIFIED” 定义**：原计划放行“VERIFIED 的 TableIR”，但 `TableIR.verification`/`TableCell.verification` 在 `processing/table_models.py` 的 `__post_init__` 钉死 `PENDING`，“已验证网格”不可实现。改为与 TEXT/LIST/GROUP 同标准的**字面转写 VERIFIED**（`ObjectDescription.verification == VERIFIED`、producer `exact-source-transcription-v1`、`LiteralQualification` 回执、qualification stage SUCCEEDED），网格结构仍 PENDING；单元格引用只证明原文，不证明行列关系。
- **`mounted=True` 的“仅证据挂载”语义**：缺 `EMBEDDING_*` 时文档仍 `mounted=true`、`embedding_configured=false`，manifest/context 可用，search/chat 503；只有指纹不符、非 ready、证据损坏才 `mounted=false` 并带 `mount_error`。证据读取本身不需要 embedder，且目录必须如实区分“未配置检索”与“拒绝挂载”。
- **契约名 `rag-chat-v1`**：与 plans/item3 §5 一致，未改。
- **`FusedHit` 归属**：迁到 `answers/models.py`（`AnswerResult` 要引用它）并在 `hybrid_search` 回导，计划已预留该选项。
- **`answers/` 放行 pydantic**：用 `check_architecture.EXTRA_ALLOWED` 只放行 pydantic，而不是把模型输出 schema 挪到 adapters。
- **图表值校验口径**：claim 文本等于来源显示串 **或** 数值等于资格化 `Decimal`，二者之一即通过；verified claim 的 `text` 统一为来源显示串、`value` 为 `Decimal`。
- **旧快照兼容**：没有任何“policy 不匹配即拒绝”的逻辑，policy 只是信息字段；真实 AIA 快照 policy 为 chart-qa promotion 的 `source-transcription-and-numeric-paint-qualification-v1`，照样 ready。AIA 前 20 页无 Table 对象，`qualify` 仍 eligible=189 / skipped=52。

**遗留清单**
1. `adapters/http/webui_gate.py` 只认 `aia-2026-interim-source-review-v1` / `enterprise-pdf-rag-offline-demo-v1`，Open WebUI 网关不能前置 `document-catalog` 模式（另一 agent 正在加 `document-catalog` profile，未完成）。
2. `JsonCompletionClient` 以 `retry_failed=False` 构造：真实调用失败会被缓存回放，删 `<ingestion_root>/model-cache/requests/<fingerprint>.json` 才能重试。
3. 自然语言问答没有冻结 gold 集（只有 chart-qa v1/bar 两套）。
4. 真实 embedder 的 `index`、真实答案模型的 chat、`APP_LEGACY_DOCUMENT_ROOTS` 挂真实 AIA 发布——已于 2026-09-20 做过一轮（18 用例，反捏造守住），结论与遗留（ISSUE-2 图表召回、ISSUE-3 散文门年份（已于 0.14.0 解决）、信封无 `request_fingerprint`、`visual_semantics` 无专属回归）见下方“真实模型验收”。
5. `deploy/enterprise-pdf-rag/open-webui/backend.Dockerfile` 未复验（ADR 0021）。
6. 第 20 页 v2 独立验收（第 4 项）未动。
7. 完整 `bash scripts/ci.sh` 未在本工作树整体跑绿；全部改动未 commit / 未 push。
8. `docs/spine-family.md` §5 仍写“ragspine 的 `HybridRetriever`/rerank/agent 尚未接入它的回答链”；该文件真源在家族根目录 `~/startup/spine/docs/spine-family.md`，须改真源再 `make family-doc-sync`，本仓副本未单独改。

**环境坑**：全局 `uv` 已于 2026-09-20 升到 0.12.17（`uv self update --token $(gh auth token)`，未认证会撞 GitHub API 限流）；`.venv` 为 3.12，`uv sync --all-extras --no-extra ocr`；不用 uv 时可直接 `.venv/bin/python -m pytest|mypy`、`.venv/bin/ruff`；任何触及 `src/ragspine` 的提交后要把 23 份 tracked 文档的 `verified-against` bump 到新 HEAD，否则 `scripts/check_doc_drift.py` 红；11G 运行现场在 `data/`（gitignore），别清理；本地模型隧道变量（`LOCAL_MODELS_SSH_HOST` 等）默认不在 shell 里。

**下一 session 起手顺序**：读 `src/enterprise_pdf_rag/CLAUDE.md` → 本节 → ADR 0011 → 下方“真实模型验收”结论 → `git status --short` 核对工作树 → `bash scripts/ci.sh` 全门 → 按用户授权 commit（先 `ruff format` / `ruff check --fix` 规范化）→ 再从遗留清单挑：自然语言问答 gold 集、`webui_gate` 接 `document-catalog`、第 4 项 p20 v2。

**真实模型验收（2026-09-20）**

环境（密钥、私有主机与端点一律不写）：答案模型用 shell 里既有的 `OPENAI_BASE_URL` / `OPENAI_API_KEY`（外部 OpenAI-compatible 端点）+ `OPENAI_MODEL=gpt-5.6-luna`；embedding / rerank 为 linhan 4080 台式机（linhan-pc）上的 vLLM 0.14.0 容器 `rag-vllm-embedding`（远端 127.0.0.1:28002，`Qwen/Qwen3-Embedding-4B`，2560 维，fingerprint `local-http/Qwen/Qwen3-Embedding-4B` 与现有 AIA 索引一致）与 `rag-vllm-reranker`（远端 127.0.0.1:28001，`Qwen/Qwen3-Reranker-4B`，`/v1/rerank` Cohere 形状），经项目受管 SSH 隧道映射到本机 39002 / 39001（本包强制 loopback）。服务：`document-catalog` 模式 uvicorn `127.0.0.1:8768`，`APP_LEGACY_DOCUMENT_ROOTS` 指向 `data/output/aia-2026-interim/pages-001-020`，就绪 ≤17.8s（两文档 13.86s）。证据目录 `data/validation/generic-chat-2026-09-20/`（`aia/` 13 个、`generic/` 13 个原始响应、`generic-after-fix/`、`summary.json`）；日志与证据中三个 API key 出现 0 次，无 traceback。

AIA 前 20 页（model `enterprise-pdf-rag/df902346791b`，189 成员）：

| # | 问题 | 结果 |
| --- | --- | --- |
| a | “record Operating ROE in 1H 2026?” | 200 answered，1 claim，p.4 `fragments.<span>` 逐字引用 “record Operating ROE of 17.5%”，`llm_live_calls=1`，16.2s |
| b | “1H26 Distribution Mix chart… Agency %?” | answered 但引用的是 p.14 文本 “Premier Agency: 55% of VONB”——答非所问（正确是 p.18 donut 72%）；`rerank=true` 同样。**ISSUE-2 图表召回** |
| b2 | b 改写为含 “Partnerships” | answered，2 个 chart claim：p.18 `points.point-agency.value=72%`、`points.point-partnerships.value=28%`，回指 svg 元素，13.2s |
| c1 | “forecast VONB for 2027?” | 200 abstained `model_declined` / `not_in_context`，6.7s |
| c2 | “Agency 比 Partnerships 高多少个百分点?” | abstained `model_declined` / `needs_calculation` |
| d | 重复 a | `cache_hit=true`、`llm_live_calls=0`，4.4s |
| e | a 的 `stream=true` | 6 帧：role → content×2 → 空 delta → 尾帧含 `enterprise_pdf_rag` → `[DONE]` |
| f | a 的 `rerank=true` | 真实调用了 39001（lsof 证据），但 abstained `claim_not_in_evidence`：模型原文 “…in 1H 2026 was 17.5%.”，17.5% 已验证，“2026” 不在 claim 文本内被散文数值门整体拒答。**ISSUE-3**（已于 0.14.0 放宽，见下方验收遗留） |
| g1 / g2 | `temperature` 字段 / 不存在的 `document` | 422 `extra_forbidden` / 404 |

通用合成 PDF（虚构，3 页文本 + 第 3 页原生 4×2 划线表）：修复前 sha `f41da5…`：`ingest --stage semantics --max-live-calls 3` → live=3（每页 1 次 layout，设计内）、object_count=8、failed_stage_count=5（**BUG-1**，见下）；`qualify` eligible=3 / skipped=5；`index`（首次真实 Qwen3 embedder）member_count=3、dims [2560]、1.06s；`publish` ready。重启后 `/v1/documents` 两文档 ready/mounted，`/v1/models` 两个 id。chat：

| # | 问题 | 结果 |
| --- | --- | --- |
| t1 | “cash balance?” | answered，引用 p.2 quote “Cash balance stood at 987 million” |
| t2 | 表格 Revenue | answered，`cells.table-cell-v1:…` = “1,234” 逐字一致 |
| t3 | 空单元格 Margin | abstained `not_in_context` |
| t4 | “Revenue minus Net profit” | abstained `needs_calculation` |
| t5 | 未入索引的 headcount 行 | abstained `not_in_context` |
| t6 | `model=default` 无 `document` | 422 “Document selection required” |
| t7 | `model=default` + `document=<sha12 前缀>` | answered（缓存） |

结论：18 个真实用例没有任何“证据外的数字/事实”进入 answered 回答；引用精确到 span / cell / point、页码正确；拒答理由合理。反捏造守住。ISSUE-2 是召回质量问题，不是捏造。

**BUG-1（已修复，同 session，代码未 commit）**：根因——prompt 用 `json.dumps` 全精度渲染 canonical bbox（如 `42.400000000000006`），模型回传最短小数（`42.4` / `308` / `20`），下游多处严格 `<=` 判“模型区域框 ⊇ canonical 图元”时差 6e-15 即失败；离线 stub sender 原样回传 float，所以从未暴露。修法——新增纯模块 `src/enterprise_pdf_rag/processing/geometry.py`（stdlib）：`COORDINATE_TOLERANCE = 1e-6`，`contains(outer, inner, *, tolerance)` 外框每边放宽 tol、内框保持非退化；替换 6 处“模型外框 vs canonical 内框”比较：`adapters/source_objects.py` ×2、`adapters/object_processing.py`、`processing/table_transcription.py`（`_inside` 删除；`_center_inside` 是 canonical vs canonical，不动）、`adapters/literal_qualification.py`（index / resolve 复核须与生产者一致）、`adapters/visual_semantics.py`（`_inside` 委托）、`processing/service.py::validate_partition`。不动 `table_models.py`、`pdfspine_tables.py`（本有 0.5pt 容差）、`figures/` 与 donut / bar 几何族。1e-6 的理由：float 渲染噪声 <1e-12 pt，真实版面偏移 ≫1e-3 pt，两侧各留 ≥3 个数量级，差 0.5pt 的越界 span 仍拒绝。不做边界规范化的理由：prompt 无固定精度、改 prompt 会改 request fingerprint、canonical 不能舍入（内容寻址）、snap 不覆盖表格网格与 diagram 节点。测试：新 `tests/enterprise_pdf_rag/processing/test_geometry.py`，`test_source_objects` / `test_table_transcription` / `test_partition` / `test_object_stages` 各加 “tolerates model-rendered float noise but not real overreach” 用例；离线 stub sender `_extent` 改为 `round(v, 6)` 模拟真实回传，e2e 4 个用例修前红、修后绿。全包 **758 passed**（752 + 6）、mypy 447 files 零错误、四个 check 与 doc-drift 通过。真实复验：新合成 PDF sha `3f7233e3…`（`generic-after-fix/`）`ingest` failed_stage_count=0、8 对象全部 succeeded、43.5s；`qualify` eligible=8 / skipped=0（Table 1 + Text 7）；`index` member_count=8、dims [2560]；`publish` ready。AIA 前 20 页只读 `qualify` 修前修后 JSON 逐字节一致（189/52）。

**验收遗留**（已同步进 ADR 0011 follow-ups）：
- ISSUE-2 图表召回：图表成员只嵌入短描述，词面弱于长文本，top-6 未召回 p.18 donut；候选方向：图表描述加入 period / 类别别名，或对 chart 成员做 query 侧加权，待定。
- ISSUE-3 散文数值门把年份当数字——**已解决（rag-spine 0.14.0，用户拍板）**：`answers/verify.py::prose_grounded` 现在放行三类数字：(a) 属于某条已验证 claim 的 text / value；(b) 逐字出现在用户问题（`AnswerRequest.question`）里；(c) 出现在已验证 claim 所引用证据的原文中（span quote、表格 cell 原文、图表 claim 的 period / category 标签与 source_display，即 `ClaimCitation.quote`）。其余数字仍整体拒答，零验证 claim 的处理不变，`decide` 顺序不变；用例见 `tests/enterprise_pdf_rag/answers/test_verify.py` 与 `test_answer_service.py`。
- `AnswerEnvelope` 不含 `request_fingerprint`，排障时无法直接定位 `model-cache/requests/<fp>.json`。
- `visual_semantics.py` 的 4 个 `contains` 调用点无专属回归测试。
- `ingest` 的 layout 阶段每页 1 次真实 LLM 调用，纯文本页也一样（设计内）。
- `webui_gate.py` 的 `document-catalog` profile 另一 agent 进行中，**未完成**。

## 并入 rag-spine 记录（2026-09-20）

本项目已作为**独立顶层包**并入 rag-spine 仓库（`/Users/linhan/startup/spine/ragspine`，分支 `merge/enterprise-pdf-rag`），import 名仍是 `enterprise_pdf_rag`，不在 `ragspine.*` 命名空间下；决定、被拒方案与待办见 rag-spine 的 [ADR 0021](../adr/0021-merge-enterprise-pdf-rag-as-sibling-package.md)。四个提交：

| 提交 | 含义 |
|---|---|
| `0f8499c` | `git subtree` 导入：enterprise-pdf-rag 完整历史进入 `_incoming/enterprise-pdf-rag/`，原提交可 `git log --follow` 追溯 |
| `8ce3301` | 依赖与工具链全部升到最新，`requires-python>=3.12` 与 pdfspine 对齐 |
| `95f607e` | 纯格式：ruff 0.16 全仓 format，无行为改动 |
| `407849e` | 重排合并：搬到最终目录、单一 `pyproject.toml`、ruff/mypy 按目录分级 scope、一次 pytest 合跑、`scripts/ci.sh` 第 9 步接入四个 check 脚本 |

新路径速查（均相对 rag-spine 仓库根；文中 `src/enterprise_pdf_rag/...` 与 `data/...` 写法不变）：

| 原仓库 | rag-spine |
|---|---|
| `src/enterprise_pdf_rag/` | `src/enterprise_pdf_rag/`（不变） |
| `tests/` | `tests/enterprise_pdf_rag/`（自带 `conftest.py` 的 no_network 守卫） |
| `scripts/*.py`、`scripts/start.sh` | `scripts/enterprise_pdf_rag/` |
| `configs/settings.yaml` | `config/enterprise-pdf-rag/settings.yaml` |
| `benchmarks/aia-2026-interim/` | `benchmarks/enterprise-pdf-rag/aia-2026-interim/` |
| `deployment/open-webui/` | `deploy/enterprise-pdf-rag/open-webui/` |
| `docs/`（ADR、PRD、schemas、samples、本文件） | `docs/enterprise-pdf-rag/` |
| `CLAUDE.md`、`AGENTS.md` | `src/enterprise_pdf_rag/CLAUDE.md`、`src/enterprise_pdf_rag/AGENTS.md`（rag-spine“每个模块一份 CLAUDE.md”约定） |
| `./ci.sh`、`.github/workflows/ci.yml` | `bash scripts/ci.sh`（rag-spine 的 GitHub Actions 为手动触发，本地门是唯一真源；旧 workflow 未并入） |
| `data/` | `data/`（运行现场以 APFS 克隆带入，仍 gitignore） |

门的跑法（在 rag-spine 仓库根）：`bash scripts/ci.sh` 全门——第 5 步一次 pytest 同时收集两套测试，第 9 步顺序运行 `scripts/enterprise_pdf_rag/check_conformance.py .`、`check_architecture.py`、`check_schema.py`、`check_drift.py`；单跑本包 `.venv/bin/python -m pytest tests/enterprise_pdf_rag -q`；文档漂移 `.venv/bin/python scripts/check_doc_drift.py --quiet`（`src/enterprise_pdf_rag/CLAUDE.md` 带 `covers` 头，改包内代码后要把它的 `verified-against` bump 到新 HEAD）。

原仓库 `/Users/linhan/startup/enterprise-pdf-rag` 的 `merge/into-ragspine` 分支是并入前快照（HEAD `03357a4`），`data/` 原件（output、validation、samples、current 指针等）保留在那里，不要清理；rag-spine 侧的 `data/` 是其克隆。本文件及 `docs/enterprise-pdf-rag/` 下其余文档中的 `./ci.sh`、`scripts/xxx.py`、`tests/adapters/...`、`benchmarks/aia-...`、`deployment/` 等路径已按上表改写；仅“历史暂停快照 / 前 5 分钟”里以 `cd /Users/linhan/startup/enterprise-pdf-rag` 开头的命令块保留原仓库布局。

并入后已知待办（详见 ADR 0021）：`httpx` 与 `httpx2` 选型待统一；`fastapi/uvicorn` 同时在 base 与 `[service]` extra；`deploy/enterprise-pdf-rag/open-webui/backend.Dockerfile` 仍 `uv sync --locked`，容器内会找不到 `[tool.uv.sources]` 指向的本地 `../corespine`，需改 `--no-sources` 或等价方案，尚未验证。下一步的通用服务挂载与自然语言回答链改在 rag-spine 上实现（见下方“当前后续工作顺序”第 2、3 项）。

## 恢复开发记录（2026-09-19）

用户已恢复开发，要求项目面向通用 PDF；AIA 仅为公开验收样本。已在线核对 [AIA 官方业绩页](https://www.aia.com/en/investor-relations/overview/results-presentations) 与 README 中的官方 PDF，封面为 2026 年 8 月 20 日、71 页，原 PDF 不随公共仓库分发。README 已明确此边界，原批准 PRD、ADR、旧资产与旧快照仍保留。

本次服务验收中，8767 界面、8766 模型发现/原文/状态均为 200，当前仍是下文的 `a7384f…` / `f59d…`，241 objects、189 vectors、第 18 页 2 个 qualified claims。普通 OpenAI 兼容聊天只做来源审阅；财务问题 422。第 20 页 v2 尚不能因工作树有代码而宣称在线激活。

发现旧 app factory 没有注入 query embedder，因此已有索引仍不足以让在线 search 工作。本轮最小修复复用 `LocalEmbeddingAdapter` 和既有配置验证：app factory 读取独立 `EMBEDDING_BASE_URL/MODEL/API_KEY`；来源 app 将 adapter 传入 processing router。缺少/无效配置时来源读取继续，search 503；已知 provider 错误映射为不泄密的 503，不重试。只有 API 子进程继承三项 embedding 配置，Open WebUI 不继承任何模型 key。回环 URL 限制、索引 fingerprint/维度检查和财务资格 guard 保持。

离线 ASGI/transport 测试覆盖 query-only 一次调用、启动/读取零调用、缺失/部分/远端配置、服务失败、错模型/维度、context 回填、财务拒答及 launcher 凭证隔离。正式门通过后已按 owned stop/start 切换现有服务，active `.venv` 使用官方 pdfspine 0.11.0。恰好一次真实 query embedding 搜索返回 5 条命中，context、p18 typed lookup（72%、verified、字段引用）、确定性来源审阅聊天、API 状态和 UI/config 均 200；LLM、rerank、新文档 embedding 调用数为 0。运行与完整原始响应保存在 `data/validation/rollout-search-2026-09-19/`。两个 runner 退出后的独立只读检查确认 managed tunnel 和 owned API/WebUI 持续运行，marker/cwd 匹配；API 保留 embedding key，WebUI 与相关日志无该 key。原始 PDF 和两个 current 指针摘要前后相同，未推广第 20 页。完整通用自然语言回答、任意 PDF 的 UI 选择/发布仍未实现。

通用 `ingest --pdf ... --pages ... --stage ...` 入口与 `scripts/enterprise_pdf_rag/ingest.py` 薄封装已落地，首轮离线行为测试通过：完整 source 按 SHA 隔离、选页范围取真实页数、禁用 AIA 布局修正、共享调用预算、缓存可回放、始终 draft/no-index/no-activation。结果的 `retrieval_status` 明确 `not_ready`；这不是通用聊天已完成。独立只读 review 发现的共享 Table adapter 20 页上限已修复，第 21 页原生 table 的 merge/来源 span 回归由入口实现任务完成 RED→GREEN；AIA 专用入口仍保持 1–20 页范围。SDK [官方 PyPI 0.11.0](https://pypi.org/project/pdfspine/0.11.0/) 已发布；main/tag 指向 `5a1f22e`，[release run 35473565983](https://github.com/VoldemortGin/pdfspine/actions/runs/35473565983) 成功，5 wheels + sdist，并已完成 fresh Python 3.12 官方 pip 的 paint-profile API 检查。RAG 正式 `pyproject.toml` 与 `uv.lock` 已精确锁定 `pdfspine==0.11.0`，官方包独立环境完整 `bash scripts/ci.sh` 639 tests 通过（22.90s，格式 227 files、strict mypy 201 files、schema/architecture/drift 全绿）。fresh Python 3.12 plain `pip install .`、`pip check`、checkout 外 noneditable ingest/script/profile/render/app factory/HTTP 与 CI 的 installed smoke 步骤均通过；证据 `data/validation/generic-ingest-official/summary.json`。这些是本机验证，尚不代表 GitHub Linux CI 或真实 Databricks 部署。官方 macOS wheel 对真实样本第 18/20 页 native SVG 字节与候选一致，paint-profile API 完整；证据 `data/validation/official-sdk-0.11-source-check.json`，未用此代替 RAG 的 profile digest 序列化验证或 Linux 真 PDF 验收。以下“尚未发布/先修 viewBox/主动暂停”的条目是恢复前状态，不能重复执行或据此回滚。执行发布与激活前须检查对应任务的最终证据及当前 git/tag/PyPI/current 指针，不因本记录自动激活第 20 页。

通用 draft 的资格 / description-only 索引 / 发布入口已落地，按 `ingest` 返回的 store 根和 `processing_id` 工作，不再写死 AIA 文件名/页数/来源 SHA。新模块 `src/enterprise_pdf_rag/adapters/draft_publication.py`：`qualify_draft` 只读、零模型（`ProcessingStore.load` + `validate_processing_source` + 资格谓词统计）；`index_draft`（显式注入 embedder）用 description-only `ProcessingRetrieval.build` → `save_draft` 产新不可变 snapshot，`export_processing_review(update_current=False)` 不切指针，标题反映实际文档；`publish_draft(activate_source=True)` 原子切 `current-processing`，可选切 `current-manifest`，未 `index` 的 draft 抛 `ValueError`，内容寻址幂等。三 Boundary 模型 `DraftQualification/DraftIndex/DraftPublication` 把 ingest 的 `not_ready` 依次推进为 `qualified; indexing pending → indexed; publication pending → ready`。`processing_retrieval.py` 抽出模块级 `eligibility(record)` 由 `build` 与资格共用，行为不变。CLI 新增 `qualify|index|publish`（共享 `--source-store/--processing-store/--processing-id`，`index` 有 `--document-label`，`publish` 有 `--activate-source/--no-activate-source` 默认激活）；`index` 仅用生产 `LocalEmbeddingAdapter(load_local_model_config("embedding"))`，离线替身仅测试注入不暴露 flag；错误 → `{"error": ...}` + 退出码 1，fail closed。离线 E2E `tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py` 用非 AIA 程序化三页财务 PDF `meridian-semiannual.pdf` 走 ingest→qualify→index（`OfflineDescriptionEmbedder`，dims 64）→publish→`search`/`resolve`，命中带 snapshot_id/member_id、retrieval snapshot 的 `scope.source_manifest_id` 与 ingest 一致，另有 `cli.main` 三命令 JSON 状态推进 smoke；单元测试见 `tests/enterprise_pdf_rag/adapters/test_draft_publication.py`。阶段完成后 `bash scripts/ci.sh` 全绿（≥660 tests，最终数字以 `bash scripts/ci.sh` 为准）。真实冒烟（本机）：对真实 AIA store `data/output/aia-2026-interim`（processing 在 `pages-001-020`）的 `current-processing` `a7384f0c…` 只读 `qualify` 得 eligible=189、skipped=52（stage 未完 43、image 7、diagram 2）、chart=9、kinds Text163/List11/Group6/Chart9，与既有 189 vectors 一致；在 scratchpad 完整副本上 `publish` 两次幂等回到同一 `a7384f0c`（member_count 189、dims [2560]、retrieval snapshot `f59d2308…`、source_activated true、status ready）。**未覆盖项**：真实 `index` 未能在本地 embedder 上运行（`scripts/enterprise_pdf_rag/with_local_models.py` 报 `TunnelConfigurationError: Missing or invalid setting: LOCAL_MODELS_SSH_HOST`，当前 shell 无隧道配置），真实链路 index 需隧道环境；通用 `ingest` 亦从未对真实 PDF 跑过（`data/ingestion/` 不存在）。冒烟前后 `git status --short data/` 为空、两个 current 指针 shasum 不变；全部改动仍未 commit。这只是资格/索引/发布入口，**不等于通用 RAG 回答链完成**，OpenAI-compatible chat 仍只有来源审阅。

可复制 API 请求和完整能力边界见 [测试与入库指南](testing-and-ingestion.md)。

## 当前后续工作顺序

本轮 RAG 改动保持未 commit / 未 push；已发布的是 SDK 0.11.0。（2026-09-20 补记：这些改动已在原仓库以 `03357a4` 提交，并随上方四个提交并入 rag-spine；此后的工作在 rag-spine 仓库进行。）不要根据下方历史清单再次发布同一版本、重做已修复的 SVG smoke，或回退正式 lock。当前可测试范围以 [测试与入库指南](testing-and-ingestion.md) 为准。

优先按用户的通用文档目标推进：

1. （已完成 2026-09-19）为通用 `ingest_pdf` 的 draft 建立明确的资格、description-only 索引和发布入口，按返回的 store/manifest ID 工作，避免再写死 AIA 文件名、页数或来源 SHA。入口 `src/enterprise_pdf_rag/adapters/draft_publication.py` 与 CLI `qualify|index|publish`；证据见离线 E2E `tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py`、单元 `tests/enterprise_pdf_rag/adapters/test_draft_publication.py` 及上方恢复记录的真实 `qualify`/`publish` 冒烟（eligible=189、幂等回到 `a7384f0c`）。真实 embedder `index` 需隧道，属未覆盖。
2. （已完成 2026-09-20，代码未 commit）文档目录/选择与通用服务挂载：`adapters/document_catalog.py`（`scan_catalog`/`mount_document`/`mount_catalog`）、`adapters/http/documents.py` + `catalog_schemas.py`（契约 `document-catalog-v1`）、`app.py` 的 `document-catalog` 分支与 `APP_LEGACY_DOCUMENT_ROOTS`；决定见 [ADR 0011](adr/0011-document-catalog-and-verified-answer-chain.md)，证据 `tests/enterprise_pdf_rag/adapters/test_document_catalog.py`（15）、`test_documents_http.py`（14）、`test_document_catalog_aia_smoke.py`。真实 AIA 发布经 legacy root 在线挂载与真实 embedder search 的结论见顶部“真实模型验收”。
3. （已完成 2026-09-20，代码未 commit）证据链上的自然语言回答：`adapters/hybrid_search.py`（精确复用 `ragspine.retrieval.lexical.retrieval.{tokenize,bm25_scores,rrf_fuse}` 与 `ragspine.retrieval.rerank.listwise_rerank`，而非 `HybridRetriever`/agent 整体接入——理由见 ADR 0011 被拒方案）、`processing/context_builder.py`、`answers/`（models/prompt/verify）、`adapters/answer_service.py`（恰好一次 LLM + 逐字段校验 + 散文数值门）、`adapters/http/chat.py` + `chat_schemas.py`（契约 `rag-chat-v1`）、TABLE 成员逐字转写放行（`processing/table_transcription.py`，policy `-v2`）。离线已独立验收文本引用、表格单元格、图表值（donut/bar）的正例、拒答与损坏证据：`tests/enterprise_pdf_rag/answers/`（`test_verify.py` 12、`test_answer_service.py` 10）、`test_chat_http.py`（14）、`test_hybrid_search.py`（10）、`test_context_builder.py`（8）、`test_table_transcription.py`（3）。真实模型上的合成与校验结论见顶部“真实模型验收”；在此之前不称通用 RAG 聊天已验收。
4. 第 20 页 v2 属于独立的待完成验收：仍未新增描述 embedding、未运行新 runtime 38-case 真 API 验收、未激活。若继续该切片，沿 ADR 0009 的独立评测和 atomic activation 门推进，不把两个样本事实当成通用图表能力。
5. 准备提交/推送时再次核对用户授权与实际工作树，运行既有完整门；GitHub Linux CI 和 Databricks 部署只有实际执行后才能标记完成。

## 历史暂停快照

> 以下 CI、发布、PyPI、服务与运行结果是恢复开发前的最后核查快照；保留用于追溯，不表示当前仍然如此。

## 前 5 分钟

先做只读检查，保留现有工作树和运行现场：

```bash
cd /Users/linhan/startup/enterprise-pdf-rag
git status --short
sed -n '1,220p' AGENTS.md
sed -n '1,260p' docs/CLAUDE_HANDOFF.md
sed -n '1,240p' docs/adr/0009-source-qualified-expense-ratio-bar-lookup.md
```

- 已完成边界：第 20 页 typed `chart-qa-v2` 实现、候选 proof/qualify/resolve、兼容检查与候选 SDK 隔离环境完整门；具体证据见下文。
- 当前第一项待做：仅修复 CI smoke 示例 SVG 缺失的 `viewBox="0 0 10 10"`，然后复验该 smoke。
- 发布前置条件：先确认 SDK exact-main CI 全部成功，再按既有 release 流程发布并验证官方 PyPI 0.11.0；官方包可用前，不得更新 RAG 正式依赖或继续激活流程。
- 尚未完成：官方 SDK 发布与锁文件升级、新 runtime 38-case 验收、一次新描述 embedding、atomic activation、服务切换、RAG commit/push 与 GitHub Linux CI。
- 本轮 RAG 改动仍未提交。不要清理、还原、覆盖或移动现有现场。

## 交接范围

- 项目：`/Users/linhan/startup/enterprise-pdf-rag`
- SDK：`/Users/linhan/startup/spine/pdfspine`
- RAG 仓库：https://github.com/VoldemortGin/enterprise-pdf-rag
- 本轮开发已主动停止；以下工作均未继续执行。
- 不要声称整个 PRD 或完整图表支持已经完成。

## 工程约束

- Python 3.12；本地使用 `uv`，Databricks 预期通过普通 `pip install .` 安装。
- 遵循 ADR + TDD。
- Ruff 可安全修复及格式化；`bash scripts/ci.sh` 是唯一离线、只读的完整质量门。
- 领域层仅用 stdlib、immutable 数据和 `Protocol`；外部 SDK 位于 adapters。
- `pdfspine` 是唯一 PDF parser。
- 普通迭代不得触发 LLM。
- 只处理 `data/samples/aia-group-2026-interim-results-presentation.pdf` 的物理第 1–20 页。
- 原始 PDF SHA-256：`df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e`。
- 不得覆盖原始资产、旧 proof 或旧 snapshot。
- 只允许独立的自然语言 description embedding；禁止 embedding ChartIR 或 SVG。
- 所有推断必须携带证据、置信度和验证，并 fail closed。

## Git 状态

- RAG 本轮代码全部未 commit、未 push。
- RAG `HEAD` 与 `origin/main` 均为 `4e3fd82b46af73aab8a571cdc5b6d1918dc4fb9f`。
- SDK 0.11.0 代码已 commit/push 到 `main`：`5a1f22e23e403897439ecae96e0f3a5e4073a27b`。
- SDK tree clean。

## 本轮 RAG 实现

- 新增第 20 页 Expense Ratio 的 typed `chart-qa-v2` lookup。
- 唯一允许的正向事实：`1H24 = 8.2%`、`1H26 = 6.9%`。
- `1H25` 必须返回 unavailable/refusal。
- 禁止跨期差额、箭头 `130bps` 推断和柱高估值。
- 保留原 ChartIR 全局 `period=None`。
- 新增独立 `PointPeriodInterpretation`，与 category 共享源 occurrence。
- 已保留字段引用、原页汇率 footer 引用和 description normalization receipt。
- v1 donut 行为保持不变。
- ADR：`docs/enterprise-pdf-rag/adr/0009-source-qualified-expense-ratio-bar-lookup.md`
- 阶段说明：`docs/enterprise-pdf-rag/chart-qa-bar-stage.md`
- Gold：`benchmarks/enterprise-pdf-rag/aia-2026-interim/chart-qa-bar-gold-v1.json`
- 已实现新 source profile、stroke/bar proof、8-lineage、存储 resolver、immutable draft/append/independent targets、v2 capture/evaluator。
- 两组交叉只读 review 均无阻塞问题。

## RAG 验证状态

- 候选 SDK 隔离环境：`data/validation/chart-qa-venv`
- 完整门已通过：616 tests、219 files Ruff/format、197 files strict mypy、架构/schema/drift。
- 运行候选完整门必须使用：

```bash
UV_PROJECT_ENVIRONMENT=/Users/linhan/startup/enterprise-pdf-rag/data/validation/chart-qa-venv UV_NO_SYNC=true bash scripts/ci.sh
```

- 原因：避免 `uv` 按旧 lock 将 SDK 降级。
- 正式 `pyproject.toml`/`uv.lock` 仍固定 `pdfspine==0.10.0`，尚未在官方锁环境验证。
- `.github/workflows/ci.yml` 已新增 Ubuntu 24.04、Python 3.12、uv 0.6.2 流程：locked 离线全门、独立 venv plain pip、checkout 外 `python -I` smoke。
- 该 workflow 在 616 测试通过后新增了实际 profile/render/app factory smoke。
- 当前明确待修：10x10 示例 SVG 缺 `viewBox`，`render_svg_png` 抛 `KeyError`。
- 修复方式：给示例 SVG 增加 `viewBox="0 0 10 10"`，然后复验。
- 尚无 GitHub RAG 新 CI 实跑，也未进行真实 Databricks 部署。

## SDK 0.11.0 状态

- 新增冻结 typed `Page.get_paint_profile()`：`strict-paint-profile-v1`。
- 覆盖 inherited Resources、native audit 和 fail closed；允许窄范围 opaque RGB page group。
- 本地验证：Rust 2065 pass / 1 ignored；Python 1574 pass / 68 skip；fmt、clippy、mypy、packaging 通过。
- 既有 vendor/deny warnings 仍存在，不可声称全 SDK 零 warning。
- 最终本地 wheel：`/Users/linhan/startup/spine/release-candidates/pdfspine-0.11.0-final-candidate/pdfspine-0.11.0-cp311-abi3-macosx_11_0_arm64.whl`
- Wheel SHA-256：`3595294a46a4c32259fd82539d40684bef49bca753e4abb0b1f04fa10bbb3788`
- 该 wheel 仅是 Mac 本地候选，不是官方公开包。
- GitHub Actions：https://github.com/VoldemortGin/pdfspine/actions/runs/35472046070
- 最后核查时仍 `in_progress`，仅剩 Windows Python 3.12/3.13，无失败。
- 本地和远端均无 `v0.11.0` tag；release 未启动；PyPI 0.11.0 尚为 404。
- 本地 watcher 已停止；远端 CI 未取消。

## 当前运行版本

- Processing：`a7384f0c2654d4a2d195e6f119e6e441ef08319af35a7655d8b7a9a099caa8d5`
- Snapshot：`f59d230869d5dac6981f0545286b4772a1a9770e63597350f1a19b1a14349703`
- 当前共有 241 objects / 189 vectors；第 18 页 donut 两个事实已 qualified。
- Review：http://127.0.0.1:8766/v1/processing/review/review.html
- UI：http://127.0.0.1:8767
- 两个地址此前均核验 HTTP 200。
- 现有服务未重启，current 未切换；未新增 LLM、embedding 或 rerank 调用。
- `current-processing` 文件 SHA-256：`c98a31f23ee1d54111352d8dfa290e0aa68e383a26aaa435ac48a3b3767f6baf`
- Review 文件：`data/output/aia-2026-interim/pages-001-020/review.html`
- 只使用 `scripts/enterprise_pdf_rag/start.sh` / `scripts/enterprise_pdf_rag/webui_preview.py` 的已有 owned-process 管理；不得 kill 外部服务。

## 第 20 页候选证据

- 本地 proof/qualify/resolve 已通过。
- 报告：`data/validation/bar-p020-candidate3/report.json`
- 来源字形、柱关系、footnote 均成立；尚未 embedding 或 activate。
- 兼容报告：`data/validation/bar-source-candidate3.json`
- 已验证第 18 页旧 v2 整套 proof 与 ID 精确相等，SVG bytes 不变。
- 尚未对真实 PDF 做跨 OS 验证。
- 原始 raw 必须保持不变。
- 仅以下两条独立原句可进入合格的描述/embedding 投影；原始及 normalization 审计资产中的其他 claims 保留，但不可进入本次索引或精确问答：
  - `Expense Ratio for 1H24: 8.2 %.`
  - `Expense Ratio for 1H26: 6.9 %.`

## 历史建议续跑顺序（已被顶部最新状态取代）

1. 阅读 `AGENTS.md`、ADR 0009、本文和 `git diff`。
2. 修复 CI smoke 的 SVG `viewBox`，复验对应 smoke。
3. 确认 SDK exact-main CI 全部成功；创建 annotated `v0.11.0` tag，必须准确指向 `5a1f22e` 并 push。
4. 等既有 `release.yml` 完成 5 wheels + sdist + PyPI；禁止 force，也禁止本地 twine 绕过检查。
5. 验证官方 PyPI 0.11.0；精确升级 RAG 的 `pyproject.toml` 和 `uv.lock`。
6. 运行 `make fmt`、完整 `bash scripts/ci.sh`、fresh Python 3.12 plain-pip smoke，以及 checkout 外 smoke。
7. 仅在上述全部通过后，用既有本地 embedding 配置调用一次新 description embedding；保持同 fingerprint、2560 维，原样复用 189 个旧 vectors，最终为 190。
8. 禁止新增 LLM 或 rerank 调用。
9. 执行 draft/save/source-aware independent targets，并跑真实 v2 gold 19 cases 与 v1 旧 19 cases 回归；旧 runtime 的 v1 此前 19 cases 已通过，新 runtime 合计 38 cases 尚未实跑。
10. v2 的 gold/targets/raw observations/report 四文件必须 content-addressed；分别验证 200 business refusal、422 unsupported、409/503 fault。
11. 不得靠全拒答获得虚假通过；fault stores 使用独立 copy-on-write，禁止 hardlink 后 truncate。
12. 全部通过后才 atomic activate，并用 owned restart；激活前 PDF 和 current 指针摘要须保持不变；激活时原子替换 current，旧不可变 snapshot/资产保持不变并验证 rollback。
13. 更新准确的阶段文档，然后 commit/push RAG，检查 GitHub Linux CI。

## 授权与边界

- 用户此前授权过 commit/push 和公开发布 SDK。
- 本轮已主动停止；待用户在 Claude 中恢复开发后，沿用已授权的范围继续；无需重复询问已授权的常规操作。
- 所有运行产物必须写入 `data/`，不要写入仓库根目录 `output/`。
- 不要在文档、提交或日志中写入任何密钥、私有 host 或凭证。
