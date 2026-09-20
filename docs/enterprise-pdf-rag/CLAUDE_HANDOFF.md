# Claude 交接：通用文档 RAG 与公开样本验收

更新时间：2026-09-19

> 阅读顺序：先看下方“恢复开发记录”；后续旧暂停快照保留作证据，不能作为实时发布或服务状态。

## 恢复开发记录（2026-09-19）

用户已恢复开发，要求项目面向通用 PDF；AIA 仅为公开验收样本。已在线核对 [AIA 官方业绩页](https://www.aia.com/en/investor-relations/overview/results-presentations) 与 README 中的官方 PDF，封面为 2026 年 8 月 20 日、71 页，原 PDF 不随公共仓库分发。README 已明确此边界，原批准 PRD、ADR、旧资产与旧快照仍保留。

本次服务验收中，8767 界面、8766 模型发现/原文/状态均为 200，当前仍是下文的 `a7384f…` / `f59d…`，241 objects、189 vectors、第 18 页 2 个 qualified claims。普通 OpenAI 兼容聊天只做来源审阅；财务问题 422。第 20 页 v2 尚不能因工作树有代码而宣称在线激活。

发现旧 app factory 没有注入 query embedder，因此已有索引仍不足以让在线 search 工作。本轮最小修复复用 `LocalEmbeddingAdapter` 和既有配置验证：app factory 读取独立 `EMBEDDING_BASE_URL/MODEL/API_KEY`；来源 app 将 adapter 传入 processing router。缺少/无效配置时来源读取继续，search 503；已知 provider 错误映射为不泄密的 503，不重试。只有 API 子进程继承三项 embedding 配置，Open WebUI 不继承任何模型 key。回环 URL 限制、索引 fingerprint/维度检查和财务资格 guard 保持。

离线 ASGI/transport 测试覆盖 query-only 一次调用、启动/读取零调用、缺失/部分/远端配置、服务失败、错模型/维度、context 回填、财务拒答及 launcher 凭证隔离。正式门通过后已按 owned stop/start 切换现有服务，active `.venv` 使用官方 pdfspine 0.11.0。恰好一次真实 query embedding 搜索返回 5 条命中，context、p18 typed lookup（72%、verified、字段引用）、确定性来源审阅聊天、API 状态和 UI/config 均 200；LLM、rerank、新文档 embedding 调用数为 0。运行与完整原始响应保存在 `data/validation/rollout-search-2026-09-19/`。两个 runner 退出后的独立只读检查确认 managed tunnel 和 owned API/WebUI 持续运行，marker/cwd 匹配；API 保留 embedding key，WebUI 与相关日志无该 key。原始 PDF 和两个 current 指针摘要前后相同，未推广第 20 页。完整通用自然语言回答、任意 PDF 的 UI 选择/发布仍未实现。

通用 `ingest --pdf ... --pages ... --stage ...` 入口与 `scripts/ingest.py` 薄封装已落地，首轮离线行为测试通过：完整 source 按 SHA 隔离、选页范围取真实页数、禁用 AIA 布局修正、共享调用预算、缓存可回放、始终 draft/no-index/no-activation。结果的 `retrieval_status` 明确 `not_ready`；这不是通用聊天已完成。独立只读 review 发现的共享 Table adapter 20 页上限已修复，第 21 页原生 table 的 merge/来源 span 回归由入口实现任务完成 RED→GREEN；AIA 专用入口仍保持 1–20 页范围。SDK [官方 PyPI 0.11.0](https://pypi.org/project/pdfspine/0.11.0/) 已发布；main/tag 指向 `5a1f22e`，[release run 35473565983](https://github.com/VoldemortGin/pdfspine/actions/runs/35473565983) 成功，5 wheels + sdist，并已完成 fresh Python 3.12 官方 pip 的 paint-profile API 检查。RAG 正式 `pyproject.toml` 与 `uv.lock` 已精确锁定 `pdfspine==0.11.0`，官方包独立环境完整 `./ci.sh` 639 tests 通过（22.90s，格式 227 files、strict mypy 201 files、schema/architecture/drift 全绿）。fresh Python 3.12 plain `pip install .`、`pip check`、checkout 外 noneditable ingest/script/profile/render/app factory/HTTP 与 CI 的 installed smoke 步骤均通过；证据 `data/validation/generic-ingest-official/summary.json`。这些是本机验证，尚不代表 GitHub Linux CI 或真实 Databricks 部署。官方 macOS wheel 对真实样本第 18/20 页 native SVG 字节与候选一致，paint-profile API 完整；证据 `data/validation/official-sdk-0.11-source-check.json`，未用此代替 RAG 的 profile digest 序列化验证或 Linux 真 PDF 验收。以下“尚未发布/先修 viewBox/主动暂停”的条目是恢复前状态，不能重复执行或据此回滚。执行发布与激活前须检查对应任务的最终证据及当前 git/tag/PyPI/current 指针，不因本记录自动激活第 20 页。

通用 draft 的资格 / description-only 索引 / 发布入口已落地，按 `ingest` 返回的 store 根和 `processing_id` 工作，不再写死 AIA 文件名/页数/来源 SHA。新模块 `src/enterprise_pdf_rag/adapters/draft_publication.py`：`qualify_draft` 只读、零模型（`ProcessingStore.load` + `validate_processing_source` + 资格谓词统计）；`index_draft`（显式注入 embedder）用 description-only `ProcessingRetrieval.build` → `save_draft` 产新不可变 snapshot，`export_processing_review(update_current=False)` 不切指针，标题反映实际文档；`publish_draft(activate_source=True)` 原子切 `current-processing`，可选切 `current-manifest`，未 `index` 的 draft 抛 `ValueError`，内容寻址幂等。三 Boundary 模型 `DraftQualification/DraftIndex/DraftPublication` 把 ingest 的 `not_ready` 依次推进为 `qualified; indexing pending → indexed; publication pending → ready`。`processing_retrieval.py` 抽出模块级 `eligibility(record)` 由 `build` 与资格共用，行为不变。CLI 新增 `qualify|index|publish`（共享 `--source-store/--processing-store/--processing-id`，`index` 有 `--document-label`，`publish` 有 `--activate-source/--no-activate-source` 默认激活）；`index` 仅用生产 `LocalEmbeddingAdapter(load_local_model_config("embedding"))`，离线替身仅测试注入不暴露 flag；错误 → `{"error": ...}` + 退出码 1，fail closed。离线 E2E `tests/adapters/test_generic_publication_e2e.py` 用非 AIA 程序化三页财务 PDF `meridian-semiannual.pdf` 走 ingest→qualify→index（`OfflineDescriptionEmbedder`，dims 64）→publish→`search`/`resolve`，命中带 snapshot_id/member_id、retrieval snapshot 的 `scope.source_manifest_id` 与 ingest 一致，另有 `cli.main` 三命令 JSON 状态推进 smoke；单元测试见 `tests/adapters/test_draft_publication.py`。阶段完成后 `./ci.sh` 全绿（≥660 tests，最终数字以 `./ci.sh` 为准）。真实冒烟（本机）：对真实 AIA store `data/output/aia-2026-interim`（processing 在 `pages-001-020`）的 `current-processing` `a7384f0c…` 只读 `qualify` 得 eligible=189、skipped=52（stage 未完 43、image 7、diagram 2）、chart=9、kinds Text163/List11/Group6/Chart9，与既有 189 vectors 一致；在 scratchpad 完整副本上 `publish` 两次幂等回到同一 `a7384f0c`（member_count 189、dims [2560]、retrieval snapshot `f59d2308…`、source_activated true、status ready）。**未覆盖项**：真实 `index` 未能在本地 embedder 上运行（`scripts/with_local_models.py` 报 `TunnelConfigurationError: Missing or invalid setting: LOCAL_MODELS_SSH_HOST`，当前 shell 无隧道配置），真实链路 index 需隧道环境；通用 `ingest` 亦从未对真实 PDF 跑过（`data/ingestion/` 不存在）。冒烟前后 `git status --short data/` 为空、两个 current 指针 shasum 不变；全部改动仍未 commit。这只是资格/索引/发布入口，**不等于通用 RAG 回答链完成**，OpenAI-compatible chat 仍只有来源审阅。

可复制 API 请求和完整能力边界见 [测试与入库指南](testing-and-ingestion.md)。

## 当前后续工作顺序

本轮 RAG 改动保持未 commit / 未 push；已发布的是 SDK 0.11.0。不要根据下方历史清单再次发布同一版本、重做已修复的 SVG smoke，或回退正式 lock。当前可测试范围以 [测试与入库指南](testing-and-ingestion.md) 为准。

优先按用户的通用文档目标推进：

1. （已完成 2026-09-19）为通用 `ingest_pdf` 的 draft 建立明确的资格、description-only 索引和发布入口，按返回的 store/manifest ID 工作，避免再写死 AIA 文件名、页数或来源 SHA。入口 `src/enterprise_pdf_rag/adapters/draft_publication.py` 与 CLI `qualify|index|publish`；证据见离线 E2E `tests/adapters/test_generic_publication_e2e.py`、单元 `tests/adapters/test_draft_publication.py` 及上方恢复记录的真实 `qualify`/`publish` 冒烟（eligible=189、幂等回到 `a7384f0c`）。真实 embedder `index` 需隧道，属未覆盖。
2. **（下一步）** 接入文档目录/选择与通用服务挂载，保持不可变 source/retrieval snapshot、损坏证据拒绝、无隐式模型调用及配置隔离。消费 `DraftPublication` 的 `source_store`/`processing_store`/`current_processing_id`，用它们构造 `create_processing_router(sources, outputs, processing_id, embedder=)` 的 store 与 ID，替换 app factory 里写死的 AIA store 根。
3. 在上述证据链上实现自然语言检索回答，并独立验收普通文本、表格、图表的引用与拒答。当前 OpenAI-compatible chat 仍只有来源审阅，不能称为通用 RAG 聊天。
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
- Ruff 可安全修复及格式化；`./ci.sh` 是唯一离线、只读的完整质量门。
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
- ADR：`docs/adr/0009-source-qualified-expense-ratio-bar-lookup.md`
- 阶段说明：`docs/chart-qa-bar-stage.md`
- Gold：`benchmarks/aia-2026-interim/chart-qa-bar-gold-v1.json`
- 已实现新 source profile、stroke/bar proof、8-lineage、存储 resolver、immutable draft/append/independent targets、v2 capture/evaluator。
- 两组交叉只读 review 均无阻塞问题。

## RAG 验证状态

- 候选 SDK 隔离环境：`data/validation/chart-qa-venv`
- 完整门已通过：616 tests、219 files Ruff/format、197 files strict mypy、架构/schema/drift。
- 运行候选完整门必须使用：

```bash
UV_PROJECT_ENVIRONMENT=/Users/linhan/startup/enterprise-pdf-rag/data/validation/chart-qa-venv UV_NO_SYNC=true ./ci.sh
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
- 只使用 `scripts/start.sh` / `scripts/webui_preview.py` 的已有 owned-process 管理；不得 kill 外部服务。

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
6. 运行 `make fmt`、完整 `./ci.sh`、fresh Python 3.12 plain-pip smoke，以及 checkout 外 smoke。
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
