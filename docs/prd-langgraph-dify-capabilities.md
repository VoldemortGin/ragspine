# PRD — LangGraph 状态编排与 Dify 应用能力

> 创建：2026-09-26。状态：需求基线，分阶段实施；不代表全量能力已经交付。
> 代码审计基线：`ragspine@6a55c8157d9e5a543773be67f41e1d1bc83619da`。
> 方法：先完成需求与验收矩阵，再写失败测试，再实现，再回归；每个阶段分别提供证据。
> 产品范围暂沿用家族分层：RAGSpine 是引擎与兼容入口，spineagent 是通用编排，spinestudio 是多租户应用。
> 本文是活跃规划，不使用描述已完成代码的 `covers` 前言。历史 ADR 不在本次直接改写；首片新增 [ADR 0026](adr/0026-optional-dify-run-history.md)。

## 1. 目标与用户任务

用户希望继续开发 rag-spine，使其同时具有 LangGraph 的可编程、有状态、可恢复编排能力和 Dify 的可视化应用构建能力，同时保留现有 RAG、反捏造、溯源与可替换后端。这里的“兼具”按可验收行为定义，不以节点数量或同名 API 作为等价证明。

- 开发者能用 Python 定义状态、节点和分支，运行后查询历史、暂停审批、恢复或分叉，失败时不丢失已确认的进度。
- 应用作者能复用现有画布导入工作流、配置模型与知识库、调试、发布版本，并查看执行进度和失败原因。
- 运维人员能在服务重启后查询运行记录，理解重试、副作用与恢复边界，隔离不同应用与工作区的数据。
- 知识工作者得到可追溯的答案；无证据时拒答，受限材料不进入未经授权的生成路径。

**首个实施切片 S1：Dify public Workflow API 可选 SQLite 运行历史。** 它解决终态记录随进程消失的问题，形成真实可交付增量；它不是 checkpoint、执行恢复或完整 LangGraph 替代品。

## 2. 官方能力依据与纠偏

2026-09-26 使用 Context7 先 resolve 再 query，选用官方来源 `/websites/langchain_oss_python_langgraph` 与 `/langgenius/dify-docs`，没有引用或复制第三方实现代码。

| 能力依据 | 官方来源 | 本项目要求 |
|---|---|---|
| LangGraph 以 thread/checkpoint 保存状态并查询历史，支持同步持久化模式 | [Persistence/checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers) | 区分终态历史、节点检查点与执行调度，不能将保存输出称作恢复 |
| 恢复从 checkpoint 边界回放，已持久化 task 结果可复用；副作用与非确定性操作需要任务边界及幂等设计 | [Functional API / determinism](https://docs.langchain.com/oss/python/langgraph/functional-api) | 文档明确执行语义，不许承诺通用外部副作用 exactly-once |
| 可从历史 checkpoint 更新状态并分叉执行 | [Time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel) | 分叉须保留父版本、父 checkpoint 与新 run 身份 |
| Dify 能导出应用 DSL | [Dify CLI apps reference](https://github.com/langgenius/dify-docs/blob/main/en/cli/reference/apps.mdx) | 导入导出已有实现可复用；不得继续宣称 Dify 不可导出 |
| Dify 有运行及节点历史 | [History and logs](https://github.com/langgenius/dify-docs/blob/main/en/self-host/use-dify/debug/history-and-logs.mdx) | 区分持久记录、实时事件、隐私 trace 三种数据用途 |
| Dify Human Input 会产生 `human_input_required`，用 run/form 标识继续交互 | [Human input flow](https://github.com/langgenius/dify-docs/blob/main/en/api-reference/guides/human-input-flow.mdx) | 暂停恢复是后续独立需求，不能拿现有同步审批机制冒充完整兼容 |

LangGraph 并不要求每张图都调用付费模型；普通 Python 节点与测试替身可以离线运行。离线、无 key、确定性不应被描述为本项目独占能力。现有 `migration-from-langgraph.md` 等历史材料中的绝对化比较需另行修订；本文不沿袭其判断。

## 3. 现有能力与复用证据

审计的是源代码能力，不代表已发布 PyPI 版本或所有部署已具备同样能力。已有 [Orchestration compatibility PRD](prd-orchestration-compat.md) 记录 0.11 发布待验收，不能据此推断今天的外部发布状态。

| 范围 | 已有证据 | 缺口或实际边界 |
|---|---|---|
| Dify 编译与 IR | `src/ragspine/dify/{parse,ir,codegen,optimize}/`；`tests/dify/` | 已支持条件、并行、iteration、有限 loop、LLM、检索、提取等；外部工具仍可能只有占位；未知类型不能假装可执行 |
| 执行与安全门 | `src/ragspine/service/dify/{runner,safety,http_client}.py`；`tests/service/dify/` | 继续统一经过现有执行开关与限制；L1 是防御层，不是完备不可信代码沙箱，平台差异见 ADR 0014 |
| Workflow public API | `src/ragspine/service/api/dify_public.py`；`tests/service/api/test_api_dify_public.py` | `app.state` 的 `OrderedDict` 仅缓存最近 100 次运行；重启丢失；SSE 是执行结束后回放事件；token 统计目前为 0 |
| 可视化画布 | `studio/src/pages/workflows/`、`studio/src/workflow/` | 已有 React Flow、编辑、导入导出、执行检查器与模板，不再另造一套画布 |
| 本地工作流体验 | `src/ragspine/workflows/`、`src/ragspine/cli/main.py`；`tests/workflows/` | 已有模板、预览、scaffold、run/serve 与打包资产；目录数量不是可运行能力或质量证明 |
| 图结构展示 | `src/ragspine/pipeline/graph.py`、`topology.py` | 仅静态拓扑，不能作为有状态图运行时复用后冒称已实现 |
| 通用 agent | `../spineagent/src/spineagent/orchestration/`、`agent/` | 已有 Coordinator、工具循环、middleware；未发现持久化图 checkpoint 执行器 |
| 审批 | `../spineagent/src/spineagent/agent/approval.py` | 已有请求摘要、三态决议、一次性 token；默认内存，重跑 step 不等于跨进程恢复 |
| 产品与权限 | `../spinestudio/backend/src/spinestudio/`、`../spinestudio/docs/PRD.md` | 已有用户、工作区、权限、KB、聊天；需把工作流版本、运行与审批接入产品权限 |
| 基础原语 | `../corespine/src/corespine/` | 复用 provider、trace、credential、queue、trigger；不要预先将工作流领域塞入薄核 |
| RAG 质量 | `src/ragspine/{retrieval,agent,storage,eval}/`、`docs/invariants.md` | 继续复用双通道、来源与数字保护、RESTRICTED 过滤；任意图不得绕过它们 |
| 文档网站 | `../rag-spine-web/` | 静态展示与模板发现，不承载后端执行和租户存储 |

## 4. 架构边界

`corespine ← ragspine / spineagent ← spinestudio` 的依赖方向保持。ragspine 与 spineagent 的组合走公开接口、工具或运行时适配，不引入反向依赖。家族产品目标不意味着将所有实现塞进 ragspine。

- **ragspine**：RAG、Dify/n8n 兼容入口、已有本地 Studio、Workflow public API 历史；后续通过适配器接入通用编排。
- **spineagent**：Python 状态图、状态归并、调度、checkpoint、恢复、子图与审批执行语义。
- **spinestudio**：工作区、协作、版本发布、应用与凭据权限、审批收件箱、工作流运行管理。复用或组合 ragspine 现有画布，不未经评估重写。
- **corespine**：已有中立原语；只有多个真实消费者需要的稳定抽象才上提。
- **rag-spine-web**：公开文档与兼容性矩阵。

持久化执行状态可能包含正文；它属于受访问控制的产品/运行数据，不能混入只允许 code/计数/耗时的 `TraceSink`。首片 SQLite 的输入输出也遵循这一分离，不宣称数据库无正文。

## 5. 兼容性与非目标

### 5.1 版本规则

当前没有经过验证的完整上游版本兼容声明。需求参考的是上述 **2026-09-26 文档快照**；支持范围只以本仓 fixture 与契约测试为准。S1 保持当前公开请求/响应结构，不扩大 Dify 版本承诺。

FR-002 要求后续发布兼容包时固定上游 tag/commit、DSL schema 版本和 OpenAPI 快照；每一项标注“支持 / 部分支持 / 不支持 / 未验证”。尚未固定的版本必须明确写“未验证”，不能标记“兼容最新版”。LangGraph 在本轮是能力对标，不承诺其 import、序列化 checkpoint 或 Python API 的替换兼容。

### 5.2 不支持行为

未知 Dify 节点维持可分析/可导出警告骨架，但执行前明确拒绝，不能静默跳过。受限参数、插件、无限循环、未支持的 HTTP body 或任意代码均按现有门处理。后续新增节点也必须同时补 parser/IR、执行、安全、表单、fixture 和失败测试。

本规划不要求复制 Dify/LangGraph 源码，不引入必须依赖其服务的默认路径，不承诺其插件市场、许可证或所有 API 的同等实现；不将 Dify/n8n API 的形状相同解释为所有语义相同。外部托管、计费市场、全套企业采购治理暂不进入交付承诺。

## 6. 功能需求与验收

优先级 P0 为前置契约与首片；P1 为主要能力；P2 为成熟度扩展。除已有能力外，以下均为待实现或待验证，阶段完成后才更新状态。

### 6.1 全局要求

| ID | 优先级 | 要求与验收 |
|---|---|---|
| FR-001 | P0 | 需求→测试→实现可追溯。每个切片先固定验收、记录失败测试原因，再提交实现与通过结果；不以删除断言或新增 skip 使测试变绿 |
| FR-002 | P0 | 兼容清单固定上游版本/契约。未验证或不支持的能力显式报告，不宣称完整替代；原有入口、默认值和 golden fixture 无意外变化 |
| FR-003 | P0 | 默认离线、无 API key、核心 import clean；真实 provider 只经既有协议与可选适配；测试不得调用计费模型 |
| FR-004 | P0 | 所有执行入口复用安全门；disabled、未知节点、非法参数不能绕过；新存储配置不能开启执行 |
| FR-005 | P0 | 隐私 trace 不记录输入输出、secret；独立运行存储内容如实说明，权限隔离按验收用例证明 |
| FR-006 | P0 | RAG 输出维持反捏造、来源、受限材料隔离；图与兼容入口的 RAG 节点必须跑对应不变量回归 |
| FR-007 | P0 | macOS/Windows/Linux 使用平台无关路径、SQLite/JSON；非 ASCII 与空格路径测试；平台隔离能力如实标注 |
| FR-008 | P1 | 性能用有界状态/事件/队列和可注入时钟测试；没有测量证据时不宣称吞吐或延迟数值 |
| FR-009 | P1 | 默认行为保持向后兼容；存储格式升级、弃用与迁移有明确版本，不静默清空已有运行记录 |

### 6.2 S1：可选 SQLite public run history

| ID | 优先级 | 要求与明确验收 |
|---|---|---|
| FR-100 | P0 | 增加可选 `ServiceConfig.dify_public_run_store_path: str | None = None` / `RAGSPINE_DIFY_PUBLIC_RUN_STORE_PATH` 路径，默认 `None`继续进程内存储；不创建默认磁盘库，不添加重型依赖 |
| FR-101 | P0 | SQLite 开启后，成功 run 的 `GET /v1/workflows/run/{id}` 在创建新 app/连接后保留原响应；inputs、outputs、status、标识、时间、步数与错误字段保持原语义 |
| FR-102 | P0 | 失败 run 同样持久化；流式响应产生的同一 run 可在新 app 中读取；SSE 仍保持已有回放行为，不假装实时 |
| FR-103 | P0 | 查询按运行创建时的 API key 所有权隔离；其他 key 和未知 ID 均为同一 404；数据库不得保存原始 bearer key，使用单向摘要作为归属标识 |
| FR-104 | P0 | 默认内存保持最近 100 条全局运行的旧行为；SQLite 每个 API key 摘要归属保留最近 100 条，另一归属不能驱逐本归属记录；按写入顺序淘汰，读取不提升优先级，重启仍有界；首片不含 TTL |
| FR-105 | P0 | SQLite 写入与淘汰在事务内，成功保存后才返回正常 HTTP/SSE；存储不可用/损坏统一 `503 / history_unavailable`，不静默退回内存，不自动重试工作流；对外错误不泄漏路径或凭据。执行后存储失败可能已有副作用，不代表工作流未执行 |
| FR-106 | P0 | 序列化用 JSON 与已有响应模型；不得使用 pickle 或执行存储中的代码；JSON round-trip 保留原公开字段，读取重新验证响应模型；归属与 run ID 同时作为查询条件；损坏结构返回 `503 / history_unavailable`，不能编造空记录或误认属于其他应用 |
| FR-107 | P0 | 不将运行记录写入隐私 trace，不将 key 写入数据库；输入输出可能含正文、可能含用户传入敏感值，应明确 SQLite 是可选明文应用数据存储，未承诺自动加密或全面脱敏 |
| FR-108 | P0 | 空格/中文路径可用；新 app/独立存储实例可访问同一文件；SQLite 连接不跨不兼容线程共享，连接及时关闭；重建 app 不依赖旧进程对象 |
| FR-109 | P0 | 仅保存已完成 run 的终态。执行中的进程崩溃无恢复承诺；不新增 resume/stop/fork API，不改变 token=0，不修改画布或把历史误称 checkpoint |

归属保持与现有 API key 模型一致：轮换 key 后旧 key 的记录不自动转移；多个应用共享同一个 key 不能获得应用级隔离。身份迁移、工作区归属、可配置工作区配额属于 FR-303，而不是偷偷扩大 S1。SQLite 的每归属 100 条只是应用 key 容量隔离，不等于工作区配额。

### 6.3 S2–S4：LangGraph 类状态编排

| ID | 优先级 | 归属 | 要求与验收 |
|---|---|---|---|
| FR-200 | P1 | spineagent | Python 声明状态图、起止节点、条件边与有界循环；拒绝缺失节点/不可达终点/非法配置，确定性 fake 图可脱离 LLM 运行 |
| FR-201 | P1 | spineagent | 状态更新与 reducer 合同；并行节点写同一字段无 reducer 时显式冲突，指定 reducer 时归并顺序与重跑结果可预测 |
| FR-202 | P1 | spineagent | thread/run/checkpoint 分离，Memory/SQLite 存储 conformance；每一节点或 superstep 的提交边界明确，新进程恢复已确认进度 |
| FR-203 | P1 | spineagent | 工作流版本与状态 schema 指纹随 checkpoint 保存；不同版本恢复必须显式迁移或拒绝，不能将旧状态喂给新图静默执行 |
| FR-204 | P1 | spineagent | 持久化 interrupt/resume，复用现有审批请求与 token 语义；审批前不执行被保护动作，拒绝/重复 resume/错误 run 授权有失败用例 |
| FR-205 | P1 | spineagent | retry、timeout、取消与中断状态分别定义；fake clock 驱动重试测试，崩溃测试证明已提交任务结果复用，不把可重试等同幂等 |
| FR-206 | P1 | spineagent | 副作用任务边界和幂等键；“副作用已发生、checkpoint 未提交”故障窗必须有明确 at-least-once 或人工处理策略；不承诺外部 exactly-once |
| FR-207 | P1 | spineagent | 子图与多 agent 可组合，namespace 隔离状态与 checkpoint；子图失败不污染其他 run；复用 Coordinator/tool/middleware |
| FR-208 | P2 | spineagent | 查询历史、回放与 fork；已完成任务不无条件再次调用，fork 有新 ID 与父来源，原历史不可变 |
| FR-209 | P1 | spineagent + ragspine | 真正执行中产生 node/state/token 事件；顺序号、终态唯一、断连/重连与背压有测试；兼容 API 的 replay 与 live 模式明确区分 |

上述能力影响 ADR 0013 的“只编译、拒绝新 runtime”边界；在进入 S2 实现前需新增 ADR 说明可选编排运行时、IR 复用或适配方式，不改写旧 ADR 的历史决定。

### 6.4 S4–S5：Dify 类产品闭环

| ID | 优先级 | 归属 | 要求与验收 |
|---|---|---|---|
| FR-300 | P1 | ragspine + spinestudio | 复用既有画布/节点表单，Python 与画布语义对应；导入→编辑→导出保留已支持字段，未知字段保留且执行前报告 |
| FR-301 | P1 | spinestudio | 草稿、不可变发布版本、回滚和应用入口分离；运行绑定发布版本，修改草稿不改变运行中或历史版本 |
| FR-302 | P1 | ragspine + spinestudio | 逐节点调试、运行列表、真实事件、错误定位、停止与恢复；UI 显示能力可用性，后端未实现时不可显示可执行按钮 |
| FR-303 | P1 | spinestudio | 工作区/应用/角色隔离工作流、历史、checkpoint 与审批；跨工作区统一拒绝，key 轮换与凭据引用不泄漏明文；每条 API 有权限测试 |
| FR-304 | P1 | spinestudio + spineagent | Human Input 表单与审批收件箱连接持久化恢复；过期/重复提交/越权/流程版本变化均有确定结果 |
| FR-305 | P1 | ragspine | 节点兼容表与测试 fixture 配套；现有 loop/iteration/branch/variables/LLM/知识检索功能分别标注限制；工具、插件等只在真实适配可用后标支持 |
| FR-306 | P2 | spinestudio | 手动、定时与 webhook 触发复用 trigger/queue；重复投递有幂等策略，禁用应用不会触发执行，异步队列恢复行为可测 |
| FR-307 | P1 | ragspine + spinestudio | 模型/provider/工具凭据用现有协议与引用；缺配置明确提示，导出默认不含 secret，生产调用须独立真实环境验收 |
| FR-308 | P2 | ragspine + spinestudio | chatflow 跨轮会话状态与 workflow 单次状态分离；会话历史不污染检索路由及权限，token/费用按可用 provider 数据统计，不将 0 冒称真实用量 |

### 6.5 S5–S6：RAG、生态与质量

| ID | 优先级 | 归属 | 要求与验收 |
|---|---|---|---|
| FR-400 | P1 | ragspine | 知识入库、检索、rerank、引用、双通道作为公开可组合节点；同一 fixture 经 Python 与工作流得到等价来源与拒答行为 |
| FR-401 | P1 | ragspine + spinestudio | 文档版本、增量更新、删除、检索过滤与工作区权限一致；删除材料不会在新检索中出现，历史数据保留策略明确 |
| FR-402 | P1 | ragspine | 用既有评估套件比较质量，不降低 numeric/citation/refusal/fabrication 基线以通过新工作流；真实模型能力与 mock 机制测试分别报告 |
| FR-403 | P2 | spineagent | MCP/A2A、工具与子 agent 复用现有协议；超时、缺工具、结构错误可诊断，插件发现不在 import 时联网 |
| FR-404 | P2 | 家族 | 安装 wheel 后的 CLI/Studio/API 冒烟及三平台验证；离线基础路径可用，可选依赖缺失提示正确；发布状态有实际制品证据 |

## 7. 分期与依赖

| 阶段 | 范围 | 前置 | 完成判据 |
|---|---|---|---|
| S0 | 本 PRD、现状与验收矩阵 | 同步后基线 | 需求编号与失败行为明确；测试/实现尚未被标为完成 |
| S1 | FR-100–109；必要的 FR-001–007 | S0 | 失败测试先出现；SQLite 成功/失败/流式历史、重启与隔离测试通过，原入口回归通过 |
| S2 | FR-200–203 状态图及持久化基础 | 新架构 ADR、S0 | fake 图与 checkpoint conformance、跨进程与冲突测试通过；暂不承诺所有 Dify 图适配 |
| S3 | FR-204–208 审批、重试、恢复、子图 | S2 | 故障注入与副作用边界用例通过，恢复不越权，不丢已提交结果 |
| S4 | FR-209、300、302、305，现有 IR/画布接入 | S2–S3 | 同一工作流经 Python/API/画布满足对应合同；实时与回放可区分 |
| S5 | FR-301、303、304、307、308、400–402 | S4 与产品权限模型 | 发布→执行→暂停→审批→恢复→历史的授权用户闭环，以及 RAG 质量回归 |
| S6 | FR-306、403、404 与成熟度补齐 | S5 | 触发、生态、打包、性能与平台证据齐全；逐项更新兼容矩阵 |

全量范围作为路线图，不把后续阶段缩成 S1 的附带实现。每阶段开始前再次检查代码与上游版本，已有实现发生变化时调整证据和测试，不重复开发。

## 8. 测试驱动与质量门

1. 完成需求与[独立测试矩阵](test-plan-langgraph-dify.md)并评审编号/边界，尤其是 S1 的容量、所有权、明文数据与错误行为。
2. 先写故障与用户行为测试，运行并记录合理失败；导入错误可证明缺模块，但关键行为仍需通过 API/存储断言验证。
3. 实现最小改动，复用现有 response model、config、app factory 和隔离规则；不顺手重构无关代码。
4. 跑聚焦测试与原有 public API 测试，再运行适用的类型、lint 与回归门；保存真实结果，不预填通过数字。
5. 审核完成后再更新实现状态并同步 Git；不得因尚未有多 OS 环境而宣称三平台已实测。

所有命令从各项目根运行。ragspine 聚焦入口为 `.venv/bin/python -m pytest tests/service/api/test_api_dify_public.py -q`，新持久化测试随切片补充；编译与执行回归为 `.venv/bin/python -m pytest tests/dify tests/service/dify -q`；完整本地门为 `make ci`（实际脚本还含文档引用/漂移、mypy、ruff、离线测试、QA 基线和 demo）。若涉及已有画布，在 ragspine 根通过 `pnpm --dir studio test` 与 `pnpm --dir studio build` 验证。

外部服务、GPU、付费模型与未安装可选依赖的验证范围独立列明。测试失败需区分本次回归与同步后已有问题；任何未执行项不得写成已通过。

## 9. 待收敛决策

- 用户是否希望以 spinestudio 为统一产品入口，或长期保留独立本地 Studio；当前先保留两者现有职责，S1 不依赖此决定。
- 通用图运行时 API、checkpoint 粒度与 Dify IR 适配方式需在 S2 的 ADR 中定案，避免与纯编译路径混为一谈。
- 工作区历史保留、加密、归档、key 轮换后的记录转移在产品阶段决定；首片保持现有 key 归属，默认内存仍是全局 100 条，SQLite 采用每 key 100 条容量。
- 上游兼容版本必须在宣称版本兼容前固定并建立 fixtures；此文不凭当前文档猜版本号。
