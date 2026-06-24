---
status: accepted
date: 2026-06-24
---

# ADR 0013 — Dify 工作流 YAML → 纯 Python 编译器 + 静态优化建议器

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Relates to [0009](0009-dependency-and-framework-policy.md)（无框架锁定 + permissive-only 许可门）、
[0011](0011-python-project-standard-divergences.md)（项目标准）、family
[0001](../../../docs/adr/0001-spine-family-boundaries-and-dependency-direction.md)（家族边界 / LLM 缝）。

## Context

Dify 是流行的低代码 LLM 应用编排平台，工作流以 `.yml`（DSL）导出。大量用户在 Dify 里搭好了
workflow / advanced-chat，但想**摆脱平台锁定**：要一段无框架、可读、可进版本库、可离线跑的纯
Python。RAGSpine 的立身之本正是「framework-free backend」（ADR 0009：无框架锁定 + 只用 permissive
许可依赖），把 Dify 工作流**编译**成命令式纯 Python、并顺手给**静态优化建议**，与产品方向天然契合。

关键约束：family 全同步、零 SDK 核心、LLM 走 `corespine.LLMProvider.chat` 缝、离线 `MockProvider`
默认、permissive-only 许可门、mypy `--strict`、pydantic 仅在边界。

## Decision

**`.yml → parse → IR → codegen + optimize`，三段经一层去 Dify 化的 IR 解耦。**

- **parse**：`yaml.safe_load`（PyYAML，MIT，过许可门）读成 dict，pydantic v2 边界模型校验
  （`extra='allow'` 宽松未知字段，不因 Dify 加字段而脆断）。pydantic **只在此段**出现。
  PyYAML 经新增 `[dify]` optional extra 延迟 import，核心 `dependencies` 不动。
- **IR**：`DifyDoc → WorkflowIR`。节点归一为 frozen-dataclass `IRNode` 子类、变量引用
  （`value_selector` / `{{#nodeId.field#}}`）归一为 `VarRef`/`Literal`/`TemplateValue`，边带
  `source_handle`，Kahn 拓扑给 `topo_order` 与 `parallel_layers`，环 → `CyclicGraph`。纯 stdlib、
  零 pydantic、零 Dify 概念泄漏到此层以下。
- **codegen**：`WorkflowIR → GeneratedCode`。拓扑展平成命令式脚本
  `def run_workflow(inputs, *, provider=None) -> dict`；LLM 节点 → `provider.chat(messages)`；
  并行层 → `concurrent.futures.ThreadPoolExecutor`（**不生成 async**，family 全同步）。纯 stdlib。
- **optimize**：`WorkflowIR → list[Suggestion]`，**8 条纯静态规则**（零 API、不碰真实环境，env 上限
  可注入）：PARALLEL_001/002、BOTTLE_001/002、CACHE_001、RESOURCE_001/002、LLM_001。

落点 `src/ragspine/dify/{parse,ir,codegen,optimize}/ + api.py + errors.py`。门面：
`compile_dify_yaml(source, *, target='ragspine', provider_expr='MockProvider()', emit_trace=False,
analyze=True) -> CompileResult(code, suggestions, ir)`；`analyze(source, *, env=None) -> list[Suggestion]`；
低层 `parse_dify_yaml / lower_to_ir / generate_code`。CLI：`ragspine dify compile <path>`。

## §7 开放问题与本轮默认（MVP 主进程已定，待 review 收敛）

实现阶段为推进 MVP，对以下 6 个开放问题取了**合理默认**（用户可在 review 时改）：

1. **目标运行时**：默认纯 ragspine 命令式脚本；保留 `target=` 参数口子，spineagent 编排目标留 P7。
2. **优化范围**：纯静态规则（零 API）。动态 profiling / 真实 token 估算留后续。
3. **code 节点**：直接**内联**生成的本地函数，注释标注「来源信任假设」（Dify code 节点本就执行任意
   用户代码，编译器沿用同一信任边界，不沙箱化）。
4. **不支持的节点**（http-request / tool / knowledge-retrieval / parameter-extractor / 插件）：生成带
   `raise NotImplementedError` + 详细 docstring 的钩子函数（产**可运行骨架**，不整体失败）；明确写入
   `GeneratedCode.warnings`。
5. **app 模式**：先 `workflow` + `advanced-chat`（含 answer 节点）。`conversation_variables` / memory /
   多轮会话状态留 P7。
6. **YAML 依赖**：新增 `[dify]` optional extra（PyYAML，`safe_load`），核心 `dependencies` 不动。

## Alternatives considered (rejected)

- **直接 `.yml → 代码`（无 IR）**：拒。三段强耦合、难加目标后端（spineagent）、难做静态分析；IR 是
  优化器与多目标的共同支点。
- **运行时解释器（读 .yml 即时执行）**：拒。那是另造一个迷你 Dify runtime，违背「编译成可进库的纯
  Python、摆脱平台」初衷，也与 ADR 0009 的无框架立场相悖。
- **生成 async 代码**：拒。family 全同步；并行用 `ThreadPoolExecutor` 足够且更可读、可离线跑。
- **把 PyYAML 列入核心 dependencies**：拒。仅 parse 段需要，延迟 import + `[dify]` extra 即可，保持
  「`import ragspine` 零重依赖」。

## Consequences

- 新增 `[dify]` extra（PyYAML）；核心 `dependencies` 与离线核心不变。
- 不支持的节点产**可运行骨架 + warning**，用户得到可直接补全的脚手架，而非编译失败。
- IR 层为后续目标（spineagent 编排、真实 token 估算、动态优化）预留了扩展点。
- 本 ADR 为 **Proposed**：上述 6 默认待 review；任何改动按 supersede-don't-edit 另立/改状态。

## P7 落地（2026-06-24）：accepted

> 不改上文历史决策正文（supersede-don't-edit）。本节将 §7 默认 **#1 / #4** 由 proposed 收敛为
> **accepted**，并记录 P7 新增能力。前置元数据 `status` 随之 `proposed → accepted`。

- **默认 #1（目标运行时）→ accepted & 推进**：`target='spineagent'` 口子已实现（MVP，可离线运行的
  最小路径）。含 tool-use 结构（≥1 tool 节点）时 → 映射 spineagent `Coordinator` /
  `FunctionCallingAgent`，入口 `run_agent(inputs, *, provider=None) -> AgentResult`；无 tool 节点则
  `DifyCompileError(code='dify.no_agent_structure')` 建议改用 `target='ragspine'`。
- **默认 #4（不支持节点）→ refined & accepted**：knowledge-retrieval / parameter-extractor / tool
  三类由 NotImplementedError 钩子转为**真实代码生成**——knowledge-retrieval →
  `build_narrative_retriever + retrieve`（`KNOWLEDGE_CHUNK_DB` 默认 `':memory:'` 离线空库）；
  parameter-extractor → `provider.chat(tools=[function-tool schema])` 解析 tool_calls；tool →
  spineagent `@function_tool` 占位 + invoke 调用点。**仅真正需外部副作用的节点**（http-request 等）
  保留为钩子（编译器无法凭空生成外部副作用，留可补全骨架是诚实行为）。
- **新增：answer_question 折叠 pass**（`codegen/fold.py`，默认开，`fold_answer_question` 开关）：
  IR 结构匹配 `start → knowledge-retrieval → llm(context 指向该检索) → answer/end` 问答骨架时，折叠为
  一次 `ragspine.answer_question(...)`（自带反幻觉「未找到」改写 + provenance），比手接 retrieve+chat
  更短更正确。
- **重申**：编译器自身**不新增任何运行时依赖**——生成代码 import ragspine 检索原语 / spineagent，但
  `ragspine.dify` 自身 import 仍 clean；mypy `--strict` / ruff / `filterwarnings=error` 全绿。
