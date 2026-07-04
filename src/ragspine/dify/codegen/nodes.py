"""每类 IRNode → 命令式代码片段的 dispatch。纯 stdlib。

约定（与 emitter.py 的 prelude / 组装契约一致）：
- 节点产出统一写入运行期上下文 `_ctx[(node_id, field)] = value`；下游用 `_var(_ctx, node, field)` 读。
- 一个 Value → Python 取值表达式：Literal→repr、VarRef→`_var(...)`、TemplateValue→片段拼接。
- LLM 节点：拼 OpenAI 形状 messages → `provider.chat(messages)` → 取 choices[0].message.content。
- 不支持节点：调用 emitter 注入的 `_hook_<var>(...)`（带 NotImplementedError 的骨架函数）。

每个 emitter 返回 run_workflow 体内的若干行（已含 4 空格基础缩进的相对行，由 emitter 统一再缩进）。
"""

from __future__ import annotations

from ragspine.dify.codegen.fold import FoldPlan
from ragspine.dify.codegen.naming import NameTable
from ragspine.dify.ir.model import (
    AnswerNode,
    CodeNode,
    EndNode,
    IRNode,
    KnowledgeRetrievalNode,
    Literal,
    LLMNode,
    ParameterExtractorNode,
    StartNode,
    TemplateTransformNode,
    TemplateValue,
    ToolNode,
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
    if isinstance(node, KnowledgeRetrievalNode):
        return _emit_knowledge_retrieval(node, names)
    if isinstance(node, ParameterExtractorNode):
        return _emit_parameter_extractor(node, names)
    if isinstance(node, ToolNode):
        return _emit_tool(node, names)
    if isinstance(node, TemplateTransformNode):
        return _emit_template_transform(node)
    if isinstance(node, UnsupportedNode):
        return _emit_unsupported(node, names)
    # if-else / iteration 由 emitter 在控制流层处理（需图结构/子图递归）；其余兜底注释。
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


def _emit_knowledge_retrieval(
    node: KnowledgeRetrievalNode, names: NameTable
) -> list[str]:
    """knowledge-retrieval → ragspine 叙事检索原语（build_narrative_retriever + retrieve）。

    chunk_db 默认 KNOWLEDGE_CHUNK_DB（生成代码顶部一个可改的模块常量，默认 ':memory:'——离线空库
    可跑、无文件副作用；把它指向你真实灌好 chunk 的 sqlite 即生效）。Dify dataset_ids 作为注释提示
    保留（它是 Dify 内部 id，非本地路径）。检索片段列表写回 _ctx 的 (id,'result')；同时把片段文本
    拼成一段 (id,'text') 便于 LLM 节点 context 引用。
    """
    var = names.var(node.id)
    datasets = ", ".join(node.dataset_ids) or "（未指定）"
    retr = f"_retriever_{var}"
    store = f"_store_{var}"
    snippets = f"_snippets_{var}"
    return [
        f"# knowledge-retrieval: {node.id} —— ragspine 叙事检索（离线 BM25+RRF，provider 复用 LLM 缝）",
        f"# Dify 数据集：{datasets}（把 KNOWLEDGE_CHUNK_DB 指向你灌好该数据集 chunk 的 sqlite）",
        f"{retr}, {store} = build_narrative_retriever(KNOWLEDGE_CHUNK_DB, provider=provider)",
        "try:",
        f"    {snippets} = {retr}.retrieve({value_expr(node.query)}, top_k={node.top_k})",
        "finally:",
        f"    {store}.close()",
        f"_ctx[({node.id!r}, {node.output_field!r})] = {snippets}",
        f"_ctx[({node.id!r}, 'text')] = "
        f"'\\n\\n'.join(str(_s.get('text', '')) for _s in {snippets})",
    ]


def _emit_parameter_extractor(
    node: ParameterExtractorNode, names: NameTable
) -> list[str]:
    """parameter-extractor → corespine function-calling 形状（provider.chat(tools=[schema])）。

    生成一个 OpenAI function-tool schema dict（参数 → JSON-schema properties + required），调
    provider.chat(messages, tools=[schema])，从 choices[0].message.tool_calls 取首个调用的
    arguments（JSON）解析成 dict，按参数名逐一写回 _ctx。无 tool_calls（如 MockProvider 不触发
    抽取）时回退空 dict，不崩。
    """
    var = names.var(node.id)
    schema = f"_pe_schema_{var}"
    msgs = f"_pe_messages_{var}"
    resp = f"_pe_resp_{var}"
    args = f"_pe_args_{var}"
    properties = ", ".join(
        f"{p.name!r}: {{'type': {p.type!r}, 'description': {p.description!r}}}"
        for p in node.parameters
    )
    required = [p.name for p in node.parameters if p.required]
    prompt = node.instruction or "请从下面文本中抽取所需参数。"
    lines = [
        f"# parameter-extractor: {node.id} —— LLM function-calling 抽取结构化参数",
        f"{schema} = {{",
        "    'type': 'function',",
        "    'function': {",
        f"        'name': 'extract_{var}',",
        f"        'description': {node.instruction or '抽取结构化参数'!r},",
        "        'parameters': {",
        "            'type': 'object',",
        f"            'properties': {{{properties}}},",
        f"            'required': {required!r},",
        "        },",
        "    },",
        "}",
        f"{msgs} = [",
        f"    {{'role': 'system', 'content': {prompt!r}}},",
        f"    {{'role': 'user', 'content': str({value_expr(node.query)})}},",
        "]",
        f"{resp} = provider.chat({msgs}, tools=[{schema}])",
        f"_tcs_{var} = {resp}.choices[0].message.tool_calls",
        f"if _tcs_{var}:",
        f"    {args} = json.loads(_tcs_{var}[0].function.arguments or '{{}}')",
        "else:",
        f"    {args} = {{}}",
    ]
    for p in node.parameters:
        lines.append(
            f"_ctx[({node.id!r}, {p.name!r})] = {args}.get({p.name!r})"
        )
    return lines


def _emit_tool(node: ToolNode, names: NameTable) -> list[str]:
    """tool → 调用模块级 spineagent @function_tool 占位（函数本身由 emit_tool_function 生成）。

    工具副作用无法由编译器凭空生成，故函数体留 NotImplementedError 待补全；但落成家族标准
    FunctionTool 形状（生成代码 import spineagent），便于接 spineagent 编排。调用点把入参取值
    传入并写回 _ctx。
    """
    var = names.var(node.id)
    args = ", ".join(
        f"{name!r}: {value_expr(value)}" for name, value in node.inputs_map
    )
    return [
        f"# tool: {node.id}（{node.tool_name}）—— spineagent FunctionTool 形状，函数体待补全",
        f"_ctx[({node.id!r}, {node.output_field!r})] = _tool_{var}.invoke({{{args}}})",
    ]


def emit_tool_function(node: ToolNode, names: NameTable) -> list[str]:
    """为 tool 节点生成一个模块级 spineagent @function_tool 占位（带 NotImplementedError）。"""
    var = names.var(node.id)
    params = ", ".join(f"{name}: Any = None" for name, _ in node.inputs_map)
    return [
        "@function_tool",
        f"def _tool_{var}({params}) -> str:",
        f'    """Dify tool 节点 {node.id}（{node.tool_name}）的占位实现——请补全真实工具逻辑。',
        "",
        "    已落成家族标准 spineagent FunctionTool 形状（@function_tool 自动派生 schema），",
        "    可直接挂到 spineagent 的 FunctionCallingAgent / Coordinator 编排。补全后删除下面的 raise。",
        '    """',
        "    raise NotImplementedError("
        + repr(f"tool 节点 {node.id}（{node.tool_name}）尚未实现，请补全 _tool_{var}")
        + ")",
    ]


def emit_answer_question_fold(plan: FoldPlan, names: NameTable) -> list[str]:
    """折叠后的问答调用：一次 ragspine.answer_question 替代 kr.retrieve + llm.chat 手拼。

    构建叙事检索器（接 KNOWLEDGE_CHUNK_DB）+ 一个空的 FactStore（结构化通路，离线 ':memory:' 可跑），
    调 answer_question(question, store, provider, narrative_retriever=...)，自带反幻觉/provenance。
    结果 .answer 写回 llm 的输出字段（下游引用零改动）；.sources 写回 kr 的 result 字段。
    """
    kr_var = names.var(plan.kr_id)
    retr = f"_aq_retriever_{kr_var}"
    store = f"_aq_store_{kr_var}"
    facts = f"_aq_facts_{kr_var}"
    result = f"_aq_result_{kr_var}"
    datasets = ", ".join(plan.dataset_ids) or "（未指定）"
    return [
        f"# answer_question 折叠：{plan.kr_id} + {plan.llm_id} —— 一次 ragspine.answer_question",
        f"#   （自带反幻觉/ provenance；Dify 数据集：{datasets}）",
        f"{retr}, {store} = build_narrative_retriever(KNOWLEDGE_CHUNK_DB, provider=provider)",
        f"{facts} = SqliteFactStore(ANSWER_QUESTION_FACT_DB)",
        f"{facts}.init_schema()",
        "try:",
        f"    {result} = answer_question(",
        f"        str({value_expr(plan.question)}), {facts}, provider,",
        f"        narrative_retriever={retr},",
        "    )",
        "finally:",
        f"    {store}.close()",
        f"    {facts}.close()",
        f"_ctx[({plan.kr_id!r}, 'result')] = {result}.sources",
        f"_ctx[({plan.llm_id!r}, {plan.answer_field!r})] = {result}.answer",
    ]


def _emit_template_transform(node: TemplateTransformNode) -> list[str]:
    return [
        f"# template-transform: {node.id}",
        f"_ctx[({node.id!r}, 'output')] = {value_expr(node.template)}",
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
