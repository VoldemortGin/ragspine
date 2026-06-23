"""每类 IRNode → 命令式代码片段的 dispatch。纯 stdlib。

约定（与 emitter.py 的 prelude / 组装契约一致）：
- 节点产出统一写入运行期上下文 `_ctx[(node_id, field)] = value`；下游用 `_var(_ctx, node, field)` 读。
- 一个 Value → Python 取值表达式：Literal→repr、VarRef→`_var(...)`、TemplateValue→片段拼接。
- LLM 节点：拼 OpenAI 形状 messages → `provider.chat(messages)` → 取 choices[0].message.content。
- 不支持节点：调用 emitter 注入的 `_hook_<var>(...)`（带 NotImplementedError 的骨架函数）。

每个 emitter 返回 run_workflow 体内的若干行（已含 4 空格基础缩进的相对行，由 emitter 统一再缩进）。
"""

from __future__ import annotations

from ragspine.dify.codegen.naming import NameTable
from ragspine.dify.ir.model import (
    AnswerNode,
    CodeNode,
    EndNode,
    IRNode,
    IterationNode,
    Literal,
    LLMNode,
    StartNode,
    TemplateTransformNode,
    TemplateValue,
    UnsupportedNode,
    Value,
    VarRef,
)


def value_expr(value: Value) -> str:
    """把一个 Value 渲染成 Python 取值表达式（在 run_workflow 体内、_ctx 在作用域内有效）。"""
    if isinstance(value, VarRef):
        return _var_expr(value)
    if isinstance(value, Literal):
        return repr(value.value)
    if isinstance(value, TemplateValue):
        return _template_expr(value)
    raise TypeError(f"未知 Value 类型：{type(value).__name__}")  # pragma: no cover


def _var_expr(ref: VarRef) -> str:
    # 统一 2 参形式：_var 是 run_workflow 内闭包，闭合 _ctx（IR/条件渲染无需知道 _ctx）。
    return f"_var({ref.node_id!r}, {ref.field!r})"


def _template_expr(tpl: TemplateValue) -> str:
    """模板 → 'str(part)+str(part)+...' 拼接表达式；空模板 → ''。"""
    if not tpl.parts:
        return "''"
    chunks: list[str] = []
    for part in tpl.parts:
        if isinstance(part, VarRef):
            chunks.append(f"str({_var_expr(part)})")
        else:
            chunks.append(repr(part))
    return " + ".join(chunks)


# ---------------------------------------------------------------------------
# 各节点 emitter：返回 run_workflow 体内的代码行（无额外缩进，由 emitter 统一缩进）。
# ---------------------------------------------------------------------------


def emit_node(node: IRNode, names: NameTable) -> list[str]:
    """单节点 → 代码行。dispatch by 具体类型。"""
    if isinstance(node, StartNode):
        return _emit_start(node)
    if isinstance(node, EndNode):
        return _emit_end(node)
    if isinstance(node, AnswerNode):
        return _emit_answer(node)
    if isinstance(node, LLMNode):
        return _emit_llm(node, names)
    if isinstance(node, CodeNode):
        return _emit_code(node, names)
    if isinstance(node, TemplateTransformNode):
        return _emit_template_transform(node)
    if isinstance(node, IterationNode):
        return _emit_iteration_serial(node, names)
    if isinstance(node, UnsupportedNode):
        return _emit_unsupported(node, names)
    # if-else 由 emitter 在控制流层处理（需图结构）；其余兜底注释。
    return [f"# 节点 {node.id}（{node.kind}）由控制流层处理。"]


def _emit_start(node: StartNode) -> list[str]:
    lines = [f"# start: {node.id}"]
    for var in node.variables:
        lines.append(f"_ctx[({node.id!r}, {var!r})] = getattr(inputs, {var!r}, None)")
    return lines


def _emit_end(node: EndNode) -> list[str]:
    lines = [f"# end: {node.id}"]
    for name, value in node.outputs:
        lines.append(f"_result[{name!r}] = {value_expr(value)}")
    return lines


def _emit_answer(node: AnswerNode) -> list[str]:
    return [
        f"# answer: {node.id}",
        f"_answer = {value_expr(node.answer)}",
        f"_ctx[({node.id!r}, 'answer')] = _answer",
        "_result['answer'] = _answer",
    ]


def _emit_llm(node: LLMNode, names: NameTable) -> list[str]:
    var = names.var(node.id)
    lines = [f"# llm: {node.id}"]
    msg_var = f"_messages_{var}"
    lines.append(f"{msg_var} = [")
    for msg in node.messages:
        lines.append(
            f"    {{'role': {msg.role!r}, 'content': {value_expr(msg.text)}}},"
        )
    lines.append("]")
    lines.append(f"_resp_{var} = provider.chat({msg_var})")
    lines.append(
        f"_ctx[({node.id!r}, 'text')] = "
        f"(_resp_{var}.choices[0].message.content or '')"
    )
    return lines


def _emit_code(node: CodeNode, names: NameTable) -> list[str]:
    """code 节点 → 内联本地函数（来源信任假设，ADR 0013 默认 3）。

    把用户代码体当作一个名为 main(...) 的函数（Dify code 节点约定 def main(**kwargs) -> dict），
    内联定义后以归一入参调用，产出写回 _ctx。代码体原样内联，标注信任来源。
    """
    var = names.var(node.id)
    fn = f"_code_{var}"
    lines = [
        f"# code: {node.id} —— 内联用户代码（来源信任假设：编译期沿用 Dify 同等信任边界）",
    ]
    # 用户代码体（约定包含 `def main(...)`）原样内联到一个本地命名空间。
    body = node.code.rstrip("\n")
    if body:
        lines.append(f"def {fn}_run():")
        for line in body.splitlines():
            lines.append(f"    {line}")
        # 入参以关键字传入 main。
        kwargs = ", ".join(
            f"{name}={value_expr(value)}" for name, value in node.inputs_map
        )
        lines.append(f"    return main({kwargs})")
        lines.append(f"_out_{var} = {fn}_run()")
    else:
        lines.append(f"_out_{var} = {{}}")
    # 产出字段写回 _ctx（main 返回 dict）。
    for field_name in node.outputs:
        lines.append(
            f"_ctx[({node.id!r}, {field_name!r})] = "
            f"(_out_{var} or {{}}).get({field_name!r})"
        )
    return lines


def _emit_template_transform(node: TemplateTransformNode) -> list[str]:
    return [
        f"# template-transform: {node.id}",
        f"_ctx[({node.id!r}, 'output')] = {value_expr(node.template)}",
    ]


def _emit_iteration_serial(node: IterationNode, names: NameTable) -> list[str]:
    """iteration 的占位串行实现（P4 接管并行/真子图展开）。

    P3 阶段先产一个安全的串行骨架：对 iterator 数组逐项收集 item 本身（恒等），保证
    生成代码可 exec 跑通；真正的子图逐项执行由 P4 实现。
    """
    return [
        f"# iteration: {node.id}（P3 串行占位；P4 接管子图/并行）",
        f"_ctx[({node.id!r}, 'output')] = list({value_expr(node.iterator)} or [])",
    ]


def _emit_unsupported(node: UnsupportedNode, names: NameTable) -> list[str]:
    """不支持的节点 → 调用 emitter 注入的钩子函数（带 NotImplementedError 骨架）。"""
    var = names.var(node.id)
    return [
        f"# {node.node_type}: {node.id} —— 未支持，调用骨架钩子（产可运行骨架，运行到此会抛 NotImplementedError）",
        f"_ctx[({node.id!r}, 'output')] = _hook_{var}(_ctx)",
    ]


def emit_hook_function(node: UnsupportedNode, names: NameTable) -> list[str]:
    """为不支持节点生成一个模块级钩子函数（带详细 docstring + raise NotImplementedError）。"""
    var = names.var(node.id)
    detail = "；".join(f"{k}={v!r}" for k, v in node.raw) or "（无额外配置）"
    return [
        f"def _hook_{var}(_ctx: dict) -> Any:",
        '    """未支持的 Dify 节点占位钩子——请在此补全真实实现。',
        "",
        f"    节点 id：{node.id}",
        f"    节点类型：{node.node_type}（ragspine.dify 暂未内建代码生成，留钩子）",
        f"    原始配置：{detail}",
        "",
        "    家族建议：http-request 用标准库 urllib / 你的 HTTP 客户端；knowledge-retrieval",
        "    接 ragspine 的检索通路；tool / 插件按你的工具实现接入。补全后删除下面的 raise。",
        '    """',
        "    raise NotImplementedError("
        + repr(f"{node.node_type} 节点 {node.id} 尚未实现，请在 _hook_{var} 中补全")
        + ")",
    ]
