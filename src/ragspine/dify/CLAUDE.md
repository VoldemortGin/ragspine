# CLAUDE.md — ragspine.dify（子包宪章）

Dify 工作流 YAML → 纯 Python 编译器 + 静态优化建议器。先读仓库根 `CLAUDE.md` 与
`docs/adr/0013-dify-workflow-compiler.md`（设计决策 + 开放问题）；本文件是本子包的操作指南。

## 这是什么

把一个 Dify `.yml`（`app.mode` ∈ {workflow, advanced-chat}）编译成一段**无框架、命令式、
可离线运行**的纯 Python 脚本，并给一份**纯静态**（零 API）的优化建议。生成脚本的骨架：

```python
def run_workflow(inputs: Inputs, *, provider: LLMProvider | None = None) -> dict:
    ...
```

LLM 节点走家族 `corespine.LLMProvider.chat(messages)` 缝，默认 `ragspine.MockProvider()`；
并行用 `concurrent.futures.ThreadPoolExecutor`（**家族全同步，绝不生成 async**）。

## 三段经 IR 解耦（去 Dify 化）

```
parse/    .yml → DifyDoc（pydantic v2 边界，extra='allow'；PyYAML 延迟 import）
ir/       DifyDoc → WorkflowIR（IRNode 图 + VarRef 数据流 + topo_order + parallel_layers）
codegen/  WorkflowIR → GeneratedCode（拓扑展平 + import 收集）  ← 纯 stdlib
          fold.py        answer_question 折叠 pass（默认开，结构匹配问答骨架）
          spineagent.py  target='spineagent' 映射（Coordinator / FunctionCallingAgent）
optimize/ WorkflowIR → list[Suggestion]（8 条静态规则纯函数）   ← 纯 stdlib
```

**pydantic 只出现在 parse 段**（边界校验，符合仓库「pydantic at boundaries only」）。
IR 及以下层零 pydantic、零 Dify 概念泄漏。

## 节点 → 家族原语映射

| Dify 节点 | 生成 |
|---|---|
| start / end | `Inputs` 字段读取 / 收集返回 dict |
| answer（advanced-chat） | 拼接 answer 模板，进结果 dict |
| llm | `provider.chat(messages)` → 取 `choices[0].message.content` |
| code | **内联本地函数**（注释标注「来源信任假设」） |
| if-else / question-classifier | `if/elif/else` |
| iteration | `for`（串行）或 `ThreadPoolExecutor`（is_parallel，max_workers=parallel_nums） |
| template-transform | Jinja2 或 `string.Template` |
| 变量 `value_selector` / `{{#nodeId.field#}}` | `VarRef` → Python 变量 |
| knowledge-retrieval | `build_narrative_retriever + retrieve`（ragspine 叙事检索原语；`KNOWLEDGE_CHUNK_DB` 默认 `':memory:'` 离线空库） |
| parameter-extractor | `provider.chat(tools=[function-tool schema])` function-calling，解析 tool_calls arguments → 写 `_ctx` |
| tool | spineagent `@function_tool` 占位（生成代码 import spineagent，编译器不新增依赖）+ invoke 调用点；函数体留 `NotImplementedError` 待补全 |
| http-request | 已建模：受控 HTTP 槽位（`_HTTP_CLIENT`），执行默认禁用——L0 闸拒绝，须 `RAGSPINE_DIFY_HTTP_ENABLED` 显式开启后由 runner 注入受控客户端（`service/dify/http_client.py`：超时钳制、1MB 截断、仅 http(s)）；生成代码零网络 import |
| 插件 等真正需外部副作用的 | **留钩子**（残余钩子集）：生成带 `raise NotImplementedError` + 详细 docstring 的函数 + warning（编译器无法凭空生成外部副作用，产可运行骨架，不整体失败） |

## P7 高层编译

- **answer_question 折叠**（默认开，`fold_answer_question` 开关）：IR 结构匹配
  `start → knowledge-retrieval → llm(context 指向该检索) → answer/end` 问答骨架 → 折叠成一次
  `ragspine.answer_question(...)`（自带反幻觉「未找到」改写 + provenance），比手接 retrieve+chat 更短更正确。
- **`target='spineagent'`**：Dify workflow 含 tool-use 结构（≥1 tool 节点）时 → 映射到 spineagent
  `Coordinator` / `FunctionCallingAgent`（每个 tool 节点 → `@function_tool`；入口 `run_agent`；生成代码
  import spineagent，编译器不新增依赖）。无 tool 节点则 `DifyCompileError(code='dify.no_agent_structure')`。

## 铁律

- **零真实 LLM API**：测试只用 `MockProvider` + fake，永不联网、永不花钱。
- **离线确定性**：同一 YAML 恒定产出同一份代码（naming 稳定、节点顺序由拓扑决定）。
- 仓库 `python-project-standard`：mypy `--strict` 零警告、`filterwarnings=error` 下零 warning、
  src 深层按域分组、TDD、最小改动复用家族原语。
- 不支持的节点**生成骨架 + warning**，不让整体编译失败（见上表末行）。

## 跑（始终从仓库根）

```bash
.venv/bin/python -c "import ragspine.dify"                  # 期望 import-clean
.venv/bin/python -m pytest tests/dify -q                    # 子包测试 GREEN
ragspine dify compile tests/dify/fixtures/seq.yml           # 端到端：打印代码 + 建议
```
