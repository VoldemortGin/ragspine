# LangGraph 与 Dify 能力建设：测试需求与 TDD 验收计划

状态：需求阶段，尚未编写本轮新增测试或实现。日期：2026-09-26。

需求真源为 [能力 PRD](prd-langgraph-dify-capabilities.md)。本文定义如何证明需求已交付，不把计划中的能力写成现有能力。现状审计基线为 ragspine `6a55c8157d9e5a543773be67f41e1d1bc83619da`；未来执行结果必须记录实际代码版本，不能复用基线测试数量当作新功能证据。

## 1. 顺序、范围与归属

本轮严格按「完整需求文档 → 当前切片的失败测试 → 最小实现 → 回归与交付」推进。先完成全局需求和阶段边界，再逐个切片执行 TDD；不要求将整条路线图尚未实现的测试同时放进主分支。

通用图状态与执行语义归 `spineagent`；RAG 不变量与 Dify 接入归 `ragspine`；用户、工作区、发布版本与产品权限归 `spinestudio`；领域无关原语才可放 `corespine`。跨仓验收不能通过给兄弟引擎增加硬依赖来实现。

第一个交付切片仅为 **Dify Public API 终态运行摘要的可选 SQLite 持久化**。它保存已完成运行的查询结果，不保存执行栈、待运行节点、superstep 或恢复令牌。重启后能 GET 历史不等于 checkpoint、durable execution、resume 或 time travel。现有 streaming 仍为执行完成后的 SSE 回放，不能宣称实时 token 流。

本文的验收 ID 是稳定索引；测试落地时在测试名或紧邻注释中标明 ID，并在执行证据中关联 PRD 的 FR。未实施条目均为待验收，不能以 skipped/xfail 标记为完成。

## 2. 首片契约与失败语义

SQLite 为显式 opt-in；`ServiceConfig.dify_public_run_store_path: str | None = None` 对应 `RAGSPINE_DIFY_PUBLIC_RUN_STORE_PATH`，未配置时保留现有进程内行为。依据 [ADR 0026](adr/0026-optional-dify-run-history.md)，不从工作流输入、Bearer key 或 URL 推断文件路径。摘要包含 inputs、outputs 等业务正文，因此这是受鉴权控制的数据存储，不能写进隐私 trace。仅保存 app key 的归属摘要，不落原始 Bearer 凭据。

容量语义已冻结：默认内存保留全局最大 100 条的旧行为；SQLite 每个 owner hash 最多 100 条，另一 owner 不得驱逐本 owner 的记录。两者均按插入先后淘汰，读取不改变顺序。现有代码称为 LRU，但 GET 不 touch，实际验收应冻结 FIFO 行为。此切片不新增按时间 TTL；容量淘汰与未来 retention-day 策略必须区分。

数据库不可用、结构不兼容、记录损坏、序列化失败均返回脱敏的 `503 / history_unavailable`，对外不暴露数据库路径、凭据或正文；不能偷偷回退内存、删除库重建或把坏数据当不存在。工作流执行失败依然按现有契约产生 `HTTP 200 + status=failed` 的运行记录；存储失败是基础设施错误，不能伪装成已持久化成功。首片不承诺执行副作用与 SQLite 写入的分布式原子性，存储错误后自动重试整个工作流不在本片范围。

## 3. 首片验收矩阵（FR-100～FR-109）

精确映射：FR-100 → HIST-001；FR-101 → HIST-002/003；FR-102 → HIST-004/005；FR-103 → HIST-006/007/008；FR-104 → HIST-009/010；FR-105 → HIST-013/015/016；FR-106 → HIST-012/014；FR-107 → HIST-008 及 PRIVACY-001；FR-108 → HIST-011/016/018；FR-109 → HIST-005/017 和本文件范围声明。建议在 `tests/service/api/test_api_dify_public.py` 扩展 API 行为测试，并为存储实现新增聚焦测试文件；复用现有 `_make_client`、`_run_body`、`_parse_sse`、成功/失败 DSL fixtures、MockProvider 和 FakeQueue。

| 验收 ID | 场景与操作 | 必须断言的可观察结果 |
|---|---|---|
| HIST-001 | 不配置 SQLite，app A 执行后 app B 使用相同普通配置查询 | A 可查，独立 B 返回 404；默认配置不创建历史数据库，不增加持久化副作用 |
| HIST-002 | 配置持久化路径，完成成功 blocking run，关闭 app A 并新建 app B | B 用原 key 查询到同一 ID、workflow_id、inputs、outputs、status、error、计数和时间字段；读取不再次调用 provider/runner |
| HIST-003 | 用独立 Python 子进程写入后退出，再由新子进程查询同一文件 | 完整摘要仍存在；仅重建 TestClient 不足以单独证明进程重启语义 |
| HIST-004 | 分别产生编译失败和执行失败，再重启读取 | 都保留 `status=failed`、错误信息、空 outputs 及对应步骤计数；不能仅持久化成功结果 |
| HIST-005 | 分别消费成功与失败 streaming 响应，重启后按事件中的 run ID 查询 | 最终 `workflow_finished` 与 GET 摘要一致；原有事件顺序/跳过节点规则不变；不将本测试称为实时流验证 |
| HIST-006 | key A/B 注册同一 YAML，A 执行，B 在同一与重启 app 查询 | B 均 404，原始 key 缺失/错误仍 401；授权归属不是由相同 workflow_id 或路径决定 |
| HIST-007 | 合法 key 查询随机不存在 ID；B 查询 A 的真实 ID | 状态与标准 `{code,message,status}` 错误体一致，不泄露该 ID 的归属或存在性 |
| HIST-008 | 以专用高辨识凭据 sentinel 调用后检查所有持久化记录 | 凭据不在归属列、JSON、SQLite/WAL 内容与错误日志；同 key 跨重启归属稳定。测试输入不主动包含该凭据，避免把用户正文当存储泄漏 |
| HIST-009 | 同一 owner 容量缩为 2，写 A/B，读 A，再写 C；另起 app 查询 | A 被淘汰为 404，B/C 保留；读取不提升优先级，容量跨重启持续成立；时间戳相同也按稳定插入顺序淘汰 |
| HIST-010 | 两个合法 app key 各写 2 条，共享每 owner 容量 2 的库；A 再写第 3 条 | 总计可保留 4 条；只淘汰 A 最旧记录，B 两条全保留；归属隔离仍成立；不同数据库文件互不淘汰 |
| HIST-011 | 使用包含空格、中文的目录和数据库文件名 | 成功写入、关闭、重开；路径采用平台路径语义，不依赖 POSIX shell 转义；父目录不存在时行为与 PRD 冻结约定一致 |
| HIST-012 | inputs/outputs 包含中文、嵌套 dict/list、null、bool、整数和有限浮点 | JSON round-trip 保持值和类型，不能把 dict/list 或数值整体转换为 repr 字符串；无效 JSON 值行为显式，不产生部分可读记录 |
| HIST-013 | 指定目录为数据库文件、无法打开路径、损坏文件或不兼容 schema | 访问返回 `503 / history_unavailable`；不得 silent fallback、截断或覆盖原文件。错误响应/日志不输出路径、key 或业务正文 |
| HIST-014 | 人为放入损坏 JSON、缺少必需字段、非法字段类型的已归属记录 | 合法 owner 查询返回 `503 / history_unavailable`，不能返回伪造成功或当作 404；其它 key 仍先执行归属隔离并返回 404，不借错误暴露内容 |
| HIST-015 | 注入写入/提交错误，分别走 blocking 和 streaming | 返回 `503 / history_unavailable`，不自动重试工作流；streaming 不能先发成功终态后才发现存储失败；现有有效记录仍可读，无部分记录 |
| HIST-016 | 一个 app 写入后另一 app 连接同一文件读取；有限数量并发不同 run 写入 | 已提交记录可见，没有进程内缓存导致的假 404；容量保持有界，无 thread-affinity 异常；不要求分布式执行或并发 checkpoint |
| HIST-017 | 持久化成功后关闭执行开关，查询历史；尝试新 POST | 查询维持既有授权条件；新运行仍被统一执行 gate 拒绝，持久化不得形成新的执行入口 |
| HIST-018 | 关闭 app/连接后重命名或删除临时数据库，再清理 fixture | 无泄漏句柄，尤其 Windows 测试可清理；此项不能依赖 Unix 允许删除打开文件的行为证明 |

HIST-006 还须覆盖 key 轮换：同一路径的新 key 不能查询旧 key 历史；这不是请求 `user` 或 workspace 隔离。HIST-008 不是声称历史正文加密；首片只要求凭据归属不存明文。HIST-014 应首先按 owner 筛选再校验该 owner 可访问的数据，避免跨租户损坏内容形成信息泄露。

现有错误模式、鉴权及容量测试必须全部保留。新参数化 Memory/SQLite 测试可复用断言，但不把 Memory 的重开行为改成持久化，也不把 SQLite 的读写错误吞掉以迁就相同断言。

## 4. 后续能力验收矩阵

这些是完整路线图的测试需求，不属于首片交付声明；全程叠加 FR-001～009 全局约束。

| 验收 ID | FR 组 / 归属 | 正向与必须失败的反例 |
|---|---|---|
| GRAPH-001 | FR-200 / spineagent | 节点、条件边、循环、结束可组合；未知节点/无效入口/未声明状态键编译失败，step limit 终止无限循环 |
| STATE-001 | FR-201 / spineagent | 同一 superstep 读取一致状态快照；并行更新经声明 reducer 合并，同字段无 reducer 的并发写必须冲突，不能静默覆盖 |
| CHECKPOINT-001 | FR-202 / spineagent | 提交后终止进程并恢复，已提交节点计数不增加，下一节点正确；Memory 仅验证接口，SQLite 必须独立进程证明 |
| CHECKPOINT-002 | FR-202、203 / spineagent | 保存 state/pending tasks/父版本/工作流版本，事务前后故障无半写；损坏、未知 schema 与图版本不兼容明确失败 |
| CHECKPOINT-003 | FR-202 / spineagent | 两 worker 从同一基版本提交仅一个成功，另一方冲突，旧状态不得覆盖新状态 |
| EFFECT-001 | FR-206 / adapter | 幂等工具在外部成功而 checkpoint 前崩溃，恢复仍仅一项外部效果；不支持幂等的系统明确 at-least-once，不宣称普遍 exactly-once |
| INTERRUPT-001 | FR-204 / spineagent | 指定节点前挂起，重启后 resume payload 继续；挂起后下游未执行，错 run/token、重复消费与已结束 run 恢复失败 |
| INTERRUPT-002 | FR-204、303、304 | 审批绑定实际 run/task/参数与操作者；同 schema 不同值不复用批准，拒绝不执行副作用，并发 resume 至多一个成功 |
| REPLAY-001 | FR-208 / spineagent | 从旧 checkpoint fork 新 run，父链完整、原 run 不变；只读历史查询不执行节点，明确区分 replay、resume、重新执行 |
| PARALLEL-001 | FR-201 / spineagent | barrier/Event 证明节点重叠执行，完成顺序变化不改变声明 reducer 结果；join 等全部必需分支且仅执行一次 |
| PARALLEL-002 | FR-202、205 / spineagent | A 已提交、B 失败时恢复复用 A 且只重试 B；预算耗尽阻断下游；超时/取消按契约处理未完成分支 |
| STREAM-001 | FR-209、302 | provider 仍阻塞且工作流未结束时消费者已收到事件；事件包含 run/task/sequence，成功、失败、取消仅一项终态 |
| STREAM-002 | FR-209、302 | 慢消费者有界缓冲，断连不无限积压；按明确定义的断连策略继续或取消，重连游标去重或明确不支持 |
| DSL-001 | FR-002、300、305 / ragspine | 固定兼容 DSL 版本，支持节点 import→IR→export→import 保持语义；未支持节点/字段显式诊断，不能静默降级 |
| DSL-002 | FR-305 / ragspine | 各节点覆盖成功/缺参/类型错/分支跳过/循环边界/变量作用域；同一 fixture 原生与导入执行结果一致 |
| SAFE-001 | FR-004 / ragspine | API、CLI、webhook、未来恢复共用执行安全门；关闭执行/网络时所有入口拒绝，恢复不绕过 gate |
| PUBLISH-001 | FR-301 / spinestudio | 草稿修改不改变已发布版本；新 run 固定版本/hash，旧 run 按原版恢复，回滚只影响新运行 |
| TENANT-001 | FR-303 / spinestudio | A/B 使用相同 workflow/thread/run 名仍隔离；查询、恢复、审批、事件和下载全覆盖，未认证401/非成员404/权限不足403 |
| UI-001 | FR-300、302、304 / Studio | 编辑、校验、保存、发布、执行、挂起、恢复、失败定位走真实浏览器端到端；刷新后可继续，导入导出不丢未知元数据 |
| RAG-001 | FR-006、400、402 / ragspine | 无证据时恶意 provider 返回数字仍拒答；有事实路径丢弃额外捏造值，图/DSL/API 不能绕过最终 guard |
| RAG-002 | FR-006、400 / integration | doc ID/locator 经工具、图状态、checkpoint、事件/API/UI 保留；缺 lineage 的坏 adapter 必须被同一验收判失败 |
| RAG-003 | FR-006、401 / integration | RESTRICTED sentinel 不进入 provider/reranker prompt 或答案；覆盖 window expansion、并行汇合、恢复与工具组合 |
| PRIVACY-001 | FR-005、107 / all | 隐私 trace 仅代码/计数/耗时；嵌套字段、error、repr、异常消息不可漏正文或凭据；历史/checkpoint 属授权存储，不能当 trace |
| PACKAGE-001 | FR-003、009、404 / all | 默认 import 零网络 SDK、无 key 可运行；跨仓依赖固定 rev；干净 wheel 安装仍通过当前切片验收 |
| OS-001 | FR-007、404 / all | macOS/Windows/Linux 验证支持的基础路径、SQLite reopen、空格/中文路径；Linux 强隔离单列，不能把其它 OS 的降级称为相同保证 |

## 5. 可复用测试与不能冒充的证明

| 既有文件 | 复用内容 | 尚不能证明 |
|---|---|---|
| `tests/service/api/test_api_dify_public.py` | API 形状、鉴权、app-key 隔离、容量、SSE 序列 | 跨进程持久化、实时流、checkpoint |
| `tests/dify/test_p1_parse.py`～`test_p9_extended_nodes.py` | DSL fixtures、代码生成、分支/并行/循环、trace | 通用状态图、reducer 冲突与持久执行 |
| `tests/service/dify/test_safety.py`、`test_runner.py`、`test_subprocess_isolation.py`、`test_http_client.py` | 安全、隔离、HTTP 限制、打包执行 | 未来恢复入口自动满足同等安全性 |
| `../spineagent/tests/test_coordinator.py` | 顺序/并行/pipeline、保序、失败传播、真实并发、隐私 trace | 持久化图状态和 pending writes |
| `../spineagent/tests/test_approval.py` | 三态审批、单次 token、冲突、默认行为不变 | 现有 resume 是同进程重跑 step，不是崩溃恢复；schema 相同的参数值绑定要另测 |
| `../spineagent/tests/test_streaming.py` | provider capability、流错误归一 | 工作流结束前有事件、背压与重连 |
| `../spinestudio/backend/tests/test_isolation.py`、`test_permissions.py`、`test_history.py` | 租户隔离、权限、history 开关和 retention | history replay 创建新记录重跑，不能当 checkpoint resume |
| `tests/agent/test_agent_orchestrator.py`、`test_narrative_fallback.py`、`test_narrative_number_guard.py` | 拒答、叙述数字 guard、fallback | 新执行入口自然不绕过 guard |
| `tests/conformance/test_chunker_provenance.py`、`test_extractor_provenance.py`、`test_trace_sink.py` | provenance/privacy 参数化契约与坏 adapter 反证 | 跨仓组合无需额外集成验证 |
| `tests/workflows/test_packaging.py`、`test_workflow_cli.py` | 安装产物、CLI | 源码测试通过等同已发布产物通过 |

补充 FR 覆盖：FR-207 子图必须验证 namespace/checkpoint 隔离、嵌套失败与已有 Agent/Tool 组合；FR-306 触发必须验证重复投递幂等与禁用后零执行；FR-307 凭据必须验证缺配置错误与导出不含 secret；FR-308 必须验证会话状态与单次 workflow 状态隔离、真实 usage 与未知 usage 区分；FR-401 补文档删除/更新后新检索不返回旧材料；FR-403 补 MCP/A2A 缺工具、超时、坏结构与 import 不联网。FR-008 的资源有界要求由 STREAM-002 与容量/循环/重试预算测试共同证明。

并行/实时流测试优先使用可控同步原语和 fake clock；不要靠延时阈值、真实 LLM 响应速度或 `sleep` 猜测执行顺序。代码生成快照只证明输出稳定，必须配执行语义断言。既有 golden 缺失时自动生成的测试模式，不适合作为新增需求的 red-before-green 证据。

## 6. Red-before-green 证据规则

每个当前切片验收至少记录以下证据，并保存在 CI artifact 或本文件执行记录中：

1. PRD FR、本文验收 ID、pytest node ID，以及需求文档所在 commit。
2. 仅新增/调整测试的 commit，实际测试命令、解释器/依赖环境、退出码和失败断言摘要。
3. 首片 API 测试优先从环境变量加载新配置：旧代码忽略新配置后仍可执行，重建 app 查询出现 404 即为合理行为红灯；避免所有测试只因构造器缺少新字段而 TypeError。明确预期失败由缺少行为造成。缺少依赖、坏测试 fixture、错误路径、测试收集失败不能冒充业务红灯。
4. 若新公共符号未实现而先发生 import failure，可记录第一步失败，但随后应获得可解释的行为失败。不要用临时 `assert False` 制造红灯。
5. 实现 commit 后使用同一测试命令，记录 green 结果；禁止弱化断言、skip/xfail、删除失败用例换取通过。
6. 对隐私、反捏造、来源等关键不变量，给已知坏实现/坏数据的反证用例，证明验收有能力发现缺陷。

推荐独立 `docs:`、`test:`、`feat:` 提交，使顺序可审计。实现前先展示首片预期 red 结果给当前任务协作者审查；这属于执行协作门，不新增用户授权要求。禁止把历史测试通过记录填成此次结果。

## 7. 分阶段质量门

| 阶段 | 进入条件 | 退出条件 |
|---|---|---|
| G0 需求 | 审计现有实现和旧边界 | PRD 与本计划对齐，首片范围/配置/容量/错误/序列化已冻结，文档先于测试和实现 |
| G1 首片红灯 | G0 完成并经协作者审查 | HIST 场景落成测试，原有相关回归建立基线；新增功能按预期失败，无环境失败混入 |
| G2 首片绿灯 | G1 证据齐全 | 最小实现使同一测试全绿，跨进程重开/隔离/损坏/Unicode 证明齐全，默认内存回归保持 |
| G3 相关回归 | G2 通过 | Dify API、runner/safety、service config、类型/静态检查及受影响打包测试通过 |
| G4 项目交付 | G3 通过 | 仓库 required CI 通过，差异审查确认无扩散；提交并同步后记录实际 SHA，声明本片不等于完整 LangGraph/Dify 对齐 |
| G5 后续切片 | 该切片需求已冻结 | 状态图/并行/interrupt/实时流/产品逐片重复 G1～G4，跨仓不变量及安装产物验证通过 |

所有命令从对应项目 ROOT_DIR 运行，不能进入 `src/` 或测试子目录运行。ragspine 的针对性测试使用项目解释器执行 `python -m pytest tests/service/api/test_api_dify_public.py -q`，新增存储测试按最终路径补入同一命令。完整 required gate 以 `scripts/ci.sh` 为准，不用 `Makefile` 中标注 informational 的 lint 代替正式门禁。Windows 使用该平台虚拟环境解释器路径；不在需求中硬编码 POSIX 激活脚本。

真实 provider/GPU 测试必须单独标明是否运行、环境与限制，不替代离线确定性验收。只在当前改动涉及其它仓库时扩大回归；通过后若没有新修改/失败/未解决疑点，不重复全量测试消耗时间。

## 8. 本轮执行记录

尚未开始新增测试或代码实现。以下字段由实际执行者在相应阶段填写，不预填通过结论：需求 commit、测试 commit、red 命令与摘要、实现 commit、green 命令与摘要、相关回归、required CI、同步 SHA、仍未交付的后续 FR。
