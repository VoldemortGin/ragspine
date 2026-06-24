"""target='spineagent' 口子：含 agent/tool-use 结构的 Dify 工作流 → 映射到 spineagent 编排。

P7 任务 3（MVP 口子，不必全功能）。当 Dify 工作流含 tool 节点（tool-use 结构）时，与其逐节点手拼
命令式脚本，不如把它落成家族 agent 形态：把每个 tool 节点编成 spineagent `@function_tool`，组装一个
`FunctionCallingAgent`（自带「调工具→喂回观测→再调」的多步循环），再经 `Coordinator` 跑。

生成代码 import spineagent（家族兄弟包），ragspine 编译器本身【不】新增运行时依赖——与 ToolNode 的
普通钩子一致：编译器只生成「import spineagent 的代码」，是否装 spineagent 由用户运行时决定。

入口：`run_agent(inputs, *, provider=None) -> AgentResult`。task 取 start 的首个输入变量（无则空串）。
provider 默认 ragspine.MockProvider()（离线可跑）；真实多步工具循环需 provider 会发 tool_calls
（corespine MockProvider 不发 → 一轮直接出文本，这是「不伪造 function-calling」的诚实行为）。

判据：仅当图里【有 tool 节点】才认定为 agent 结构；否则抛 DifyCompileError 提示改用 target='ragspine'。
"""

from __future__ import annotations

from ragspine.dify.codegen import nodes as node_emit
from ragspine.dify.codegen.emitter import GeneratedCode
from ragspine.dify.codegen.naming import NameTable
from ragspine.dify.errors import DifyCompileError
from ragspine.dify.ir.model import StartNode, ToolNode, WorkflowIR


def generate_spineagent_code(
    ir: WorkflowIR, *, provider_expr: str = "MockProvider()"
) -> GeneratedCode:
    """WorkflowIR（含 tool 节点）→ 映射到 spineagent Coordinator/FunctionCallingAgent 的纯 Python。"""
    tools = [n for n in ir.graph.nodes if isinstance(n, ToolNode)]
    if not tools:
        raise DifyCompileError(
            "target='spineagent' 需要工作流含 agent/tool-use 结构（至少一个 tool 节点）；"
            "本工作流无 tool 节点，请改用 target='ragspine'。",
            code="dify.no_agent_structure",
        )
    names = NameTable(n.id for n in ir.graph.nodes)
    warnings: list[str] = []

    tool_funcs: list[str] = []
    tool_vars: list[str] = []
    for tool in tools:
        tool_funcs.extend(node_emit.emit_tool_function(tool, names))
        tool_funcs.append("")
        tool_vars.append(f"_tool_{names.var(tool.id)}")
        warnings.append(
            f"节点 {tool.id}（tool {tool.tool_name}）：已编成 spineagent @function_tool "
            f"_tool_{names.var(tool.id)}（带 NotImplementedError），请补全工具逻辑。"
        )

    system = _agent_system(ir)
    start_var = _start_first_var(ir)
    source = "\n".join([
        *_PRELUDE,
        *_emit_inputs_class(ir),
        "",
        *tool_funcs,
        *_emit_run_agent(provider_expr, tool_vars, system, start_var),
    ])
    source = source.rstrip("\n") + "\n"
    return GeneratedCode(
        source=source,
        entrypoint="run_agent",
        imports=(
            "from spineagent import Coordinator, FunctionCallingAgent, function_tool",
        ),
        warnings=tuple(warnings),
    )


def _emit_inputs_class(ir: WorkflowIR) -> list[str]:
    """从 start 节点变量生成 Inputs dataclass（全可选、默认 None）。"""
    start = next((n for n in ir.graph.nodes if isinstance(n, StartNode)), None)
    variables = start.variables if start else ()
    lines = ["@dataclass", "class Inputs:", '    """工作流输入（对应 Dify start 节点变量）。"""']
    if not variables:
        lines.append("    pass")
    for var in variables:
        lines.append(f"    {var}: Any = None")
    return lines


def _agent_system(ir: WorkflowIR) -> str:
    """从 LLM 节点的 system 提示拼一个 agent system（无则用通用指令）。"""
    from ragspine.dify.ir.model import LLMNode  # 局部 import：避免顶层循环

    for node in ir.graph.nodes:
        if isinstance(node, LLMNode):
            for msg in node.messages:
                if msg.role == "system":
                    text = "".join(p for p in msg.text.parts if isinstance(p, str))
                    if text.strip():
                        return text.strip()
    return "你是一个会使用工具的助手，按需调用工具并给出最终回答。"


def _start_first_var(ir: WorkflowIR) -> str:
    """start 节点的首个输入变量名（作为 agent 任务取值；无则空串）。"""
    for node in ir.graph.nodes:
        if isinstance(node, StartNode) and node.variables:
            return node.variables[0]
    return ""


def _emit_run_agent(
    provider_expr: str, tool_vars: list[str], system: str, start_var: str
) -> list[str]:
    """组装 run_agent 入口：构 FunctionCallingAgent + Coordinator，跑顺序编排取首个结果。"""
    tools_lit = ", ".join(tool_vars)
    task_expr = (
        f"str(getattr(inputs, {start_var!r}, '') or '')" if start_var else "''"
    )
    return [
        "def run_agent(",
        "    inputs: Inputs,",
        "    *,",
        "    provider: LLMProvider | None = None,",
        ") -> AgentResult:",
        '    """编译自含 tool-use 的 Dify 工作流：组 FunctionCallingAgent，经 Coordinator 跑。',
        "",
        "    task 取 start 的首个输入变量；多步工具循环由 FunctionCallingAgent 负责（provider 需",
        '    会发 tool_calls 才会真正调用工具，否则一轮出文本）。"""',
        f"    provider = provider if provider is not None else {provider_expr}",
        "    _agent = FunctionCallingAgent(",
        '        "dify_agent",',
        "        provider,",
        f"        [{tools_lit}],",
        f"        system={system!r},",
        "    )",
        "    _coordinator = Coordinator([_agent])",
        f"    _results = _coordinator.run_sequential({task_expr})",
        "    return _results[0]",
    ]


# 固定 prelude：模块 docstring + import（spineagent 形态，含 Inputs dataclass）。
_PRELUDE: list[str] = [
    '"""由 ragspine.dify 从含 tool-use 的 Dify 工作流自动生成（target=spineagent）。',
    "",
    "把工作流映射到 spineagent 编排：每个 Dify tool 节点编成 @function_tool，组一个",
    "FunctionCallingAgent（多步工具循环），经 Coordinator 跑。LLM 走 corespine.LLMProvider 缝。",
    '编辑提示：本文件 import spineagent（家族兄弟包），需 `pip install spineagent`。"""',
    "from __future__ import annotations",
    "",
    "from dataclasses import dataclass",
    "from typing import Any",
    "",
    "from corespine import LLMProvider",
    "from ragspine import MockProvider",
    "from spineagent import AgentResult, Coordinator, FunctionCallingAgent, function_tool",
    "",
    "",
]
