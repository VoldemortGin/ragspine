---
covers:
  - src/ragspine/dify/
verified-against: dab3119d7ccccc18d8ea58a9868a98f9ec1b257d
---

# Dify 兼容清单（固定版本）

> 依据：PRD `prd-langgraph-dify-capabilities.md` §5.1 / FR-002，测试计划 DSL-001。
> 状态取值只有四种：**支持 / 部分支持 / 不支持 / 未验证**。未固定、未比对的版本一律写「未验证」，
> 本仓**不宣称「兼容最新版」**，也不宣称可完整替代 Dify 平台。
> 判定依据只来自本仓编译器代码（`src/ragspine/dify/ir/lower.py` 的分派）与测试覆盖，不按上游文档推断。

## 1. 固定的上游版本

| 项 | 值 | 状态 |
|---|---|---|
| 上游仓库 | `https://github.com/langgenius/dify.git`（本地只读副本 `spine/dify_debug/dify`，partial clone） | — |
| 参考 commit | `3960b5710292c584102b918b982481c252b3be6f`（2026-09-26；`git describe` = `1.17.1-367-g3960b5710`，**不在 tag 上**） | 仅作对照来源 |
| 最近 tag | `1.17.1` → `8387590ace4a094de812b7847fc6a4c3a27cd52b`（2026-09-10） | 仅作对照来源 |
| 上游当前 DSL 版本 | `CURRENT_APP_DSL_VERSION = "0.7.0"`（`api/constants/dsl_version.py`，上述 tag 与 commit 相同） | **未验证**（本仓无 0.7.0 真实导出 fixture，未做逐字段比对） |
| 上游 Service API OpenAPI | 上游有生成文档 `api/openapi/markdown/service-openapi.md`（上述 commit） | **未验证**（本仓未存 OpenAPI 快照，未做 schema 比对） |

## 2. 本仓固定的 DSL 版本

| 来源 | DSL `version:` | 状态 |
|---|---|---|
| 编译器 fixture `tests/dify/fixtures/*.yml`（agent_tool / branch / iteration / knowledge / parallel / qa_fold / seq，合成） | `"0.1.5"`（`tests/dify/test_dsl_roundtrip.py` 钉死） | 支持（仅限 fixture 覆盖的节点与字段） |
| 内置工作流模板 `src/ragspine/workflows/templates` | `"0.6.0"`（`tests/workflows/test_dify_contract.py` 钉死） | 支持（仅限模板用到的节点：见该测试 `SAFE_NODE_TYPES`） |
| 上游 `0.7.0` | — | 未验证 |

编译器**不读取也不校验** DSL `version:` 字段；版本兼容性只由上表 fixture 与契约测试背书。
app.mode 只支持 `workflow` / `advanced-chat`，其余（`chat` / `agent-chat` / `completion` 等）抛
`UnsupportedAppMode`。

### DSL 往返（DSL-001）

- **import → IR**：确定性（同一输入两次 lower 结果完全相等）。
- **export**：编译器**没有 IR → Dify YAML 的反向导出器**。现有导出是 DSL 文档级的
  `ragspine.workflows.formats.dump_dify_yaml`（稳定、无 alias）；`import → dump_dify_yaml → import`
  对全部 7 个 fixture 保持 IR 逐字段相等，且再导出是不动点。「IR 编辑后再导出成 Dify YAML」**不支持**。
- **显式诊断**：未建模节点、缺 `data.type`、未建模的条件算子 → `UnsupportedNode` + 编译 warning，
  生成代码运行到该节点抛 `NotImplementedError`；服务端安全闸（warnings 非空即拒）拒绝执行。未知字段
  原样保存在 `UnsupportedNode.raw`，往返不丢。

## 3. 节点类型支持矩阵

节点类型清单取自上游 `web/app/components/workflow/types.ts` 的 `BlockEnum`（参考 commit），另加画布便签
`custom-note`。「测试」列为本仓覆盖该类型的主要测试位置。

| Dify 节点类型 | 状态 | 说明（依据代码） | 测试 |
|---|---|---|---|
| `start` | 支持 | 读 `variables` → `Inputs` 字段 | fixtures 全部、`test_p2_ir.py` |
| `end` | 支持 | 收集 `outputs` 为返回 dict | fixtures、`test_p2_ir.py` |
| `answer` | 支持 | advanced-chat 模板拼接 | `branch/knowledge/qa_fold.yml` |
| `llm` | 支持 | `provider.chat(messages)`；取 `max_tokens`，模型名透传 | fixtures、`test_p3_codegen.py` |
| `code` | 部分支持 | 仅 python3 语义内联（来源信任假设）；`code_language` 为 javascript 时未做诊断 | `test_p9_extended_nodes.py`、`tests/service/dify/` |
| `if-else` | 部分支持 | 算子支持 `= ≠ is / is not > < ≥ ≤ contains / not contains start with / end with empty / not empty`（及 `== != >= <=`）；`in / not in / all of / null / not null / exists / not exists` 等 → 整节点 UnsupportedNode | `branch.yml`、`test_dsl_roundtrip.py` |
| `question-classifier` | 未验证 | 走 if-else 分派，每个 class 条件为空，生成代码恒走第一分支，无分类语义；无测试 | 无 |
| `iteration` | 支持 | 串行 `for` 或 `ThreadPoolExecutor`（`is_parallel` / `parallel_nums`） | `iteration.yml`、`test_p4_parallel_iteration.py` |
| `iteration-start` | 支持 | 结构锚点，剔除不进 IR | `test_p2_ir.py` |
| `template-transform` | 支持 | Jinja 变量映射到 `value_selector` | `seq/parallel.yml`、`test_p2_ir.py` |
| `knowledge-retrieval` | 部分支持 | ragspine 叙事检索原语；默认 `:memory:` 离线空库，`dataset_ids` 仅作提示，不连 Dify 知识库 | `knowledge/qa_fold.yml`、`test_p5_optimize.py` |
| `parameter-extractor` | 部分支持 | function-calling 抽取；仅 fixture 一例覆盖 | `knowledge.yml` |
| `tool` | 部分支持 | 只生成 spineagent `@function_tool` 占位（`NotImplementedError`）+ warning，安全闸拒跑 | `agent_tool.yml`、`test_p7_deepening.py` |
| `variable-aggregator` | 支持 | first-non-null；含分组模式 | `test_p9_extended_nodes.py` |
| `variable-assigner`（历史别名） | 未验证 | 代码按上游改名遗留别名落 aggregator；无测试 | 无 |
| `assigner` | 部分支持 | v2 items + v1 旧形状；未知 operation → UnsupportedNode | `test_p9_extended_nodes.py` |
| `document-extractor` | 部分支持 | 仅 str/list → text 纯计算，零文件 I/O | `test_p9_extended_nodes.py` |
| `http-request` | 部分支持 | 默认禁用（需 `RAGSPINE_DIFY_HTTP_ENABLED` + 受控客户端）；form-data / binary / file → UnsupportedNode | `test_p9_extended_nodes.py`、`tests/service/dify/test_http_client.py` |
| `loop` | 部分支持 | 轮数钳制 `[0, 100]`；break 条件算子同 if-else，未建模算子 → UnsupportedNode | `test_p9_extended_nodes.py`、`test_dsl_roundtrip.py` |
| `loop-start` | 支持 | 结构锚点，剔除不进 IR | `test_p9_extended_nodes.py` |
| `loop-end` | 不支持 | 未建模，落 UnsupportedNode | 无专门测试 |
| `list-operator` | 不支持 | 落 UnsupportedNode + warning | `test_dsl_roundtrip.py`、`test_p2_ir.py` |
| `agent` | 不支持 | 落 UnsupportedNode | 通用未知节点路径 |
| `agent-v2` | 不支持 | 同上 | 通用未知节点路径 |
| `human-input` | 不支持 | 同上 | 通用未知节点路径 |
| `datasource` | 不支持 | 同上（rag pipeline 节点） | 通用未知节点路径 |
| `datasource-empty` | 不支持 | 同上 | 通用未知节点路径 |
| `knowledge-index` | 不支持 | 同上（rag pipeline 节点） | 通用未知节点路径 |
| `trigger-schedule` | 不支持 | 同上 | 通用未知节点路径 |
| `trigger-webhook` | 不支持 | 同上 | 通用未知节点路径 |
| `trigger-plugin` | 不支持 | 同上 | 通用未知节点路径 |
| `start-placeholder` | 不支持 | 同上 | 通用未知节点路径 |
| `custom-note`（画布便签） | 支持 | 无执行语义，剔除 | `test_p2_ir.py`、`test_p6_api_cli.py` |
| 缺 `data.type` | 不支持 | 落 UnsupportedNode（「缺少节点类型」warning） | `test_dsl_roundtrip.py` |

「不支持」均为**显式**：编译成功但带 warning 与 `NotImplementedError` 骨架，执行前被拒，不会静默跳过。
所有「支持」只对应本仓 fixture / 模板覆盖到的字段；上游 0.7.0 同名节点的完整字段集**未验证**。

## 4. 公共 API 端点对照（Dify Service API · Workflow App）

本仓实现：`src/ragspine/service/api/dify_public.py`（在 `dab3119` 读取；测试
`tests/service/api/test_api_dify_public.py`）。上游端点列表取自参考 commit 的
`api/openapi/markdown/service-openapi.md`。**OpenAPI 快照：未验证**——本仓无快照，下表只对照路径与形状要点，
不做 schema 级比对。

| 端点 | 状态 | 说明 |
|---|---|---|
| `POST /v1/workflows/run` | 部分支持 | blocking / streaming；Bearer app-key 选择服务端注册 YAML；streaming 为执行完成后按 trace 回放事件，非实时 |
| `GET /v1/workflows/run/{workflow_run_id}` | 部分支持 | 进程内有界缓存的 run 摘要，按 app-key 隔离 |
| `GET /v1/info` | 部分支持 | 由注册 YAML 的 app 段派生 |
| `GET /v1/parameters` | 部分支持 | 由 start 节点 variables 派生 `user_input_form` |
| `POST /v1/workflows/{workflow_id}/run` | 不支持 | 无路由 |
| `POST /v1/workflows/tasks/{task_id}/stop` | 不支持 | 无路由 |
| `GET /v1/workflows/logs` | 不支持 | 无路由 |
| `POST /v1/files/upload`、`GET /v1/files/{file_id}/preview` | 不支持 | 无路由 |
| `GET /v1/site`、`GET /v1/meta` | 不支持 | 无路由 |
| 其余 Service API（chat / completion / datasets / feedbacks 等） | 不支持 | 不在本仓兼容范围 |

## 5. LangGraph

**能力对标，不承诺 import / checkpoint / API 兼容。** 本仓不提供 LangGraph 的 Python API 替换、
不读写其序列化 checkpoint、不导入其图定义；迁移指引见 `migration-from-langgraph.md`。
