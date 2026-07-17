"""lower 段：DifyDoc → WorkflowIR（去 Dify 化）。纯 stdlib（不含 pydantic 之外，pydantic 仅读 DifyDoc）。

三件事：
1. 节点归一：按 data['type'] 把每个 DifyNode 分派成对应 IRNode 子类；未建模类型 → UnsupportedNode。
2. 变量引用归一：把 Dify 的 value_selector（["nodeId","field"]）与模板里的 {{#nodeId.field#}}
   统一成 VarRef / TemplateValue。
3. 拓扑：把控制流边 + 数据依赖摊平成有向边，算 topo_order 与 parallel_layers（环 → CyclicGraph）。
   iteration 节点的子图（带 iteration_id 的内层节点）抽出、各自 lower 成嵌套 WorkflowIR；
   Dify 画布专用的 iteration-start 结构节点与 custom-note 便签不进入可执行 IR。
"""

from __future__ import annotations

import re
from typing import Any

from ragspine.dify.ir.model import (
    AggregatorGroup,
    AnswerNode,
    AssignItem,
    CodeNode,
    DocumentExtractorNode,
    EndNode,
    ExtractParam,
    HttpRequestNode,
    IfBranch,
    IfElseNode,
    IREdge,
    IRGraph,
    IRNode,
    IterationNode,
    KnowledgeRetrievalNode,
    Literal,
    LLMMessage,
    LLMNode,
    LoopNode,
    ParameterExtractorNode,
    StartNode,
    TemplateTransformNode,
    TemplateValue,
    ToolNode,
    UnsupportedNode,
    Value,
    VariableAggregatorNode,
    VariableAssignerNode,
    VarRef,
    WorkflowIR,
)
from ragspine.dify.ir.topo import parallel_layers, topo_order
from ragspine.dify.parse.schema import DifyDoc, DifyEdge, DifyNode

# 已建模、走真实代码生成的节点类型（其余落 UnsupportedNode 留钩子，不抛异常）。
# P7 新增 knowledge-retrieval / parameter-extractor / tool 三类真实生成。
# P9 扩展节点：variable-aggregator（历史别名 variable-assigner，对齐 Dify 上游）/
# assigner / document-extractor / http-request / loop。
_MODELED_TYPES: frozenset[str] = frozenset({
    "start", "end", "answer", "llm", "code", "if-else",
    "question-classifier", "iteration", "template-transform",
    "knowledge-retrieval", "parameter-extractor", "tool",
    "variable-aggregator", "variable-assigner", "assigner",
    "document-extractor", "http-request", "loop",
})

# React Flow 节点顶层 type：普通可执行节点是容器类型 "custom"（语义类型放 data.type，
# 绝不能把 "custom" 当语义类型用）；"custom-note" 是画布便签（data 里只有文本/主题等
# UI 字段，不在执行图里），与 iteration-start 同等对待——不进 IR、不产钩子。
_CANVAS_NOTE_TYPES: frozenset[str] = frozenset({"custom-note"})
_NON_SEMANTIC_TOP_TYPES: frozenset[str] = frozenset({"custom"}) | _CANVAS_NOTE_TYPES

# assigner v2 已建模的操作符集合；操作不在集合内 → 整节点落 UnsupportedNode（不猜语义）。
_ASSIGN_OPERATIONS: frozenset[str] = frozenset({
    "over-write", "set", "clear", "append", "extend",
    "remove-first", "remove-last", "+=", "-=", "*=", "/=",
})
# 无取值的操作（value 为 None）。
_ASSIGN_VALUELESS: frozenset[str] = frozenset({"clear", "remove-first", "remove-last"})

# http-request 已建模的方法 / body 类型（form-data 多部分与 binary/file 不支持，落钩子）。
_HTTP_METHODS: frozenset[str] = frozenset({
    "get", "post", "put", "delete", "patch", "head", "options",
})
_HTTP_BODY_TYPES: frozenset[str] = frozenset({
    "none", "raw-text", "json", "x-www-form-urlencoded",
})

# loop 最大轮数护栏（对齐 Dify 画布 loop_count 上限；编译期钳制，杜绝死循环）。
_LOOP_MAX_ROUNDS: int = 100

# 模板里的变量引用：{{#nodeId.field#}}（field 可含点，如 {{#sys.query#}}）。
_TEMPLATE_REF = re.compile(r"\{\{#\s*([^#}]+?)\s*#\}\}")
# Jinja 风格 {{ var }}（template-transform 节点用，配 variables 表把 var 映射到 value_selector）。
_JINJA_VAR = re.compile(r"\{\{\s*([a-zA-Z_][\w]*)\s*\}\}")


def lower_to_ir(doc: DifyDoc) -> WorkflowIR:
    """把校验过的 DifyDoc 降为去 Dify 化的 WorkflowIR（含拓扑与并行分层）。"""
    # 先按 iteration_id / loop_id 把内层子节点从主图剥离（它们属于某容器节点的子图）。
    body_nodes: dict[str, list[DifyNode]] = {}
    top_nodes: list[DifyNode] = []
    structural_node_ids: set[str] = set()
    for node in doc.nodes:
        # iteration-start / loop-start 是 Dify React Flow 画布中的容器入口锚点。运行时
        # 迭代项/循环轮次由父容器节点提供，它们没有可执行语义，也不应生成 unsupported 钩子。
        # custom-note（顶层 type）画布便签同样无执行语义，一并剔除（关联边随 top_edges 过滤）。
        if (
            node.node_type in ("iteration-start", "loop-start")
            or _top_level_type(node) in _CANVAS_NOTE_TYPES
        ):
            structural_node_ids.add(node.id)
            continue
        container_id = node.data.get("iteration_id") or node.data.get("loop_id")
        if isinstance(container_id, str) and container_id:
            body_nodes.setdefault(container_id, []).append(node)
        else:
            top_nodes.append(node)

    # 主图的边：剔除两端都在某子图内的边（那些边属于子图内部）。
    body_node_ids = {n.id for ns in body_nodes.values() for n in ns}
    top_edges = [
        e for e in doc.edges
        if e.source not in body_node_ids
        and e.target not in body_node_ids
        and e.source not in structural_node_ids
        and e.target not in structural_node_ids
    ]

    ir_nodes = tuple(
        _lower_node(n, body_nodes.get(n.id, []), doc.edges) for n in top_nodes
    )
    return _assemble(
        doc.mode, ir_nodes, top_edges,
        conversation_defaults=_conversation_defaults(doc),
    )


def _conversation_defaults(doc: DifyDoc) -> tuple[tuple[str, Any], ...]:
    """workflow.conversation_variables → (名, 默认值) 序列（缺失 → 空；顺序保留声明序）。"""
    raw = getattr(doc.workflow, "conversation_variables", None)
    if not isinstance(raw, list):
        return ()
    out: list[tuple[str, Any]] = []
    for v in raw:
        if isinstance(v, dict) and v.get("name"):
            out.append((str(v["name"]), v.get("value")))
    return tuple(out)


def _assemble(
    mode: str,
    ir_nodes: tuple[IRNode, ...],
    dify_edges: list[DifyEdge],
    *,
    conversation_defaults: tuple[tuple[str, Any], ...] = (),
) -> WorkflowIR:
    """由归一后的节点 + 控制流边组装 IRGraph，并算拓扑/分层。"""
    ir_edges = tuple(
        IREdge(source=e.source, target=e.target, source_handle=e.source_handle)
        for e in dify_edges
    )
    node_ids = [n.id for n in ir_nodes]
    edge_pairs = [(e.source, e.target) for e in ir_edges]
    order = topo_order(node_ids, edge_pairs)
    layers = parallel_layers(node_ids, edge_pairs)
    graph = IRGraph(nodes=ir_nodes, edges=ir_edges)
    return WorkflowIR(
        mode=mode, graph=graph, topo_order=order, parallel_layers=layers,
        conversation_defaults=conversation_defaults,
    )


# ---------------------------------------------------------------------------
# 节点归一：按 data.type 分派。
# ---------------------------------------------------------------------------


def _top_level_type(node: DifyNode) -> str:
    """节点顶层 type（React Flow 字段，经 extra='allow' 透传保留在 model_extra；缺失 → 空串）。"""
    raw = (node.model_extra or {}).get("type")
    return str(raw) if raw is not None else ""


def _effective_node_type(node: DifyNode) -> str:
    """节点语义类型：data.type 优先；缺失时回退顶层 type（排除 React Flow 容器类型）。

    Dify 导出的普通可执行节点顶层 type 是 "custom"、语义类型在 data.type；个别导出把语义
    类型只写在顶层，此时宽容回退使用。两处都无有效类型 → 空串，由 _lower_node 归一成
    UnsupportedNode 骨架（Dify 能导出的工作流即合法输入，不因个别节点缺类型整体失败）。
    """
    if node.node_type:
        return node.node_type
    top = _top_level_type(node)
    return "" if top in _NON_SEMANTIC_TOP_TYPES else top


def _lower_node(
    node: DifyNode, body: list[DifyNode], all_edges: list[DifyEdge]
) -> IRNode:
    """单节点 DifyNode → 对应 IRNode 子类。未建模/缺失类型落 UnsupportedNode（不抛异常）。"""
    node_type = _effective_node_type(node)
    data = node.data
    title = str(data.get("title", "") or "")

    if node_type == "start":
        return _lower_start(node.id, title, data)
    if node_type == "end":
        return _lower_end(node.id, title, data)
    if node_type == "answer":
        return _lower_answer(node.id, title, data)
    if node_type == "llm":
        return _lower_llm(node.id, title, data)
    if node_type == "code":
        return _lower_code(node.id, title, data)
    if node_type in ("if-else", "question-classifier"):
        return _lower_if_else(node.id, title, data, all_edges)
    if node_type == "template-transform":
        return _lower_template_transform(node.id, title, data)
    if node_type == "iteration":
        return _lower_iteration(node.id, title, data, body, all_edges)
    if node_type == "knowledge-retrieval":
        return _lower_knowledge_retrieval(node.id, title, data)
    if node_type == "parameter-extractor":
        return _lower_parameter_extractor(node.id, title, data)
    if node_type == "tool":
        return _lower_tool(node.id, title, data)
    if node_type in ("variable-aggregator", "variable-assigner"):
        # Dify 上游把历史类型名 "variable-assigner" 保留为 variable-aggregator 的改名遗留
        # 别名（真正的写变量节点是 v2 的 "assigner"）——对齐上游，别名同样落 aggregator。
        return _lower_variable_aggregator(node.id, title, data)
    if node_type == "assigner":
        return _lower_assigner(node.id, title, data)
    if node_type == "document-extractor":
        return _lower_document_extractor(node.id, title, data)
    if node_type == "http-request":
        return _lower_http_request(node.id, title, data)
    if node_type == "loop":
        return _lower_loop(node.id, title, data, body, all_edges)
    # 已知但留钩子、完全未知、或缺失类型（node_type 空串）：统一落 UnsupportedNode
    # （生成骨架 + warning，不整体失败）。
    return UnsupportedNode(
        id=node.id,
        title=title,
        node_type=node_type,
        raw=tuple(sorted((k, v) for k, v in data.items() if k != "type")),
    )


def _lower_start(node_id: str, title: str, data: dict[str, Any]) -> StartNode:
    raw_vars = data.get("variables", []) or []
    names: list[str] = []
    for v in raw_vars:
        if isinstance(v, dict):
            name = v.get("variable") or v.get("name")
            if name:
                names.append(str(name))
    return StartNode(id=node_id, title=title, variables=tuple(names))


def _lower_end(node_id: str, title: str, data: dict[str, Any]) -> EndNode:
    outputs: list[tuple[str, Value]] = []
    for o in data.get("outputs", []) or []:
        if not isinstance(o, dict):
            continue
        name = str(o.get("variable") or o.get("value") or "output")
        sel = o.get("value_selector")
        outputs.append((name, _selector_to_value(sel)))
    return EndNode(id=node_id, title=title, outputs=tuple(outputs))


def _lower_answer(node_id: str, title: str, data: dict[str, Any]) -> AnswerNode:
    template = _template_from_text(str(data.get("answer", "") or ""))
    return AnswerNode(id=node_id, title=title, answer=template)


def _lower_llm(node_id: str, title: str, data: dict[str, Any]) -> LLMNode:
    messages: list[LLMMessage] = []
    for m in data.get("prompt_template", []) or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "user") or "user")
        text = str(m.get("text", "") or "")
        messages.append(LLMMessage(role=role, text=_template_from_text(text)))
    model = data.get("model", {}) or {}
    model_name = str(model.get("name", "") or "") if isinstance(model, dict) else ""
    max_tokens = _extract_max_tokens(model)
    # context（Dify advanced-chat 把上游检索结果作为外部知识喂给 LLM）：context.variable_selector。
    context_ref = _extract_context_ref(data.get("context"))
    return LLMNode(
        id=node_id, title=title, messages=tuple(messages),
        model_name=model_name, max_tokens=max_tokens, context_ref=context_ref,
    )


def _extract_context_ref(context: Any) -> VarRef | None:
    """从 llm 节点 context 配置取 variable_selector → VarRef（缺失/非引用 → None）。"""
    if not isinstance(context, dict):
        return None
    if not context.get("enabled", True):
        return None
    sel = context.get("variable_selector")
    ref = _selector_to_value(sel)
    return ref if isinstance(ref, VarRef) else None


def _lower_knowledge_retrieval(
    node_id: str, title: str, data: dict[str, Any]
) -> KnowledgeRetrievalNode:
    """knowledge-retrieval：query_variable_selector → query，dataset_ids/top_k 归一。"""
    query = _selector_to_value(data.get("query_variable_selector"))
    datasets = data.get("dataset_ids")
    if datasets is None:
        single = data.get("dataset_id")
        datasets = [single] if single is not None else []
    dataset_ids = tuple(str(d) for d in datasets) if isinstance(datasets, (list, tuple)) else ()
    # top_k 藏在 multiple_retrieval_config / single_retrieval_config / 顶层 top_k 之一。
    top_k = _extract_top_k(data)
    return KnowledgeRetrievalNode(
        id=node_id, title=title, query=query,
        dataset_ids=dataset_ids, top_k=top_k, output_field="result",
    )


def _extract_top_k(data: dict[str, Any]) -> int:
    """从 knowledge-retrieval 的若干可能位置取 top_k（缺失 → 4）。"""
    for cfg_key in ("multiple_retrieval_config", "single_retrieval_config"):
        cfg = data.get(cfg_key)
        if isinstance(cfg, dict) and "top_k" in cfg:
            return _coerce_int(cfg.get("top_k"), default=4) or 4
    if "top_k" in data:
        return _coerce_int(data.get("top_k"), default=4) or 4
    return 4


def _lower_parameter_extractor(
    node_id: str, title: str, data: dict[str, Any]
) -> ParameterExtractorNode:
    """parameter-extractor：query 选择器 + parameters 列表（name/type/description/required）归一。"""
    query = _selector_to_value(data.get("query"))
    params: list[ExtractParam] = []
    for p in data.get("parameters", []) or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "")
        if not name:
            continue
        params.append(ExtractParam(
            name=name,
            type=str(p.get("type", "string") or "string"),
            description=str(p.get("description", "") or ""),
            required=bool(p.get("required", False)),
        ))
    instruction = str(data.get("instruction", "") or "")
    model = data.get("model", {}) or {}
    model_name = str(model.get("name", "") or "") if isinstance(model, dict) else ""
    return ParameterExtractorNode(
        id=node_id, title=title, query=query,
        parameters=tuple(params), instruction=instruction, model_name=model_name,
    )


def _lower_tool(node_id: str, title: str, data: dict[str, Any]) -> ToolNode:
    """tool：tool_name + 入参表（tool_parameters / tool_configurations 的 value_selector）归一。"""
    tool_name = str(
        data.get("tool_name") or data.get("provider_id") or data.get("provider_name") or "tool"
    )
    inputs_map: list[tuple[str, Value]] = []
    params = data.get("tool_parameters")
    if isinstance(params, dict):
        for name, spec in params.items():
            inputs_map.append((str(name), _tool_param_value(spec)))
    return ToolNode(
        id=node_id, title=title, tool_name=tool_name,
        inputs_map=tuple(inputs_map), output_field="text",
    )


def _tool_param_value(spec: Any) -> Value:
    """tool 入参取值：{type:'variable', value:[...]} → VarRef；{type:'constant', value:x} → Literal。"""
    if isinstance(spec, dict):
        kind = str(spec.get("type", "") or "")
        value = spec.get("value")
        if kind in ("variable", "mixed") and isinstance(value, (list, tuple)):
            return _selector_to_value(value)
        return Literal(value=value)
    return Literal(value=spec)


def _lower_code(node_id: str, title: str, data: dict[str, Any]) -> CodeNode:
    inputs_map: list[tuple[str, Value]] = []
    raw_vars = data.get("variables", []) or []
    for v in raw_vars:
        if isinstance(v, dict):
            name = str(v.get("variable") or v.get("name") or "")
            if name:
                inputs_map.append((name, _selector_to_value(v.get("value_selector"))))
    outputs = tuple(str(k) for k in (data.get("outputs", {}) or {}).keys())
    return CodeNode(
        id=node_id, title=title,
        code=str(data.get("code", "") or ""),
        code_language=str(data.get("code_language", "python3") or "python3"),
        inputs_map=tuple(inputs_map), outputs=outputs,
    )


def _lower_if_else(
    node_id: str, title: str, data: dict[str, Any], all_edges: list[DifyEdge]
) -> IfElseNode:
    """if-else / question-classifier：把每个 case 的条件渲染成 Python 表达式 + 记录 handle。"""
    branches: list[IfBranch] = []
    cases = data.get("cases")
    if isinstance(cases, list):
        for case in cases:
            if not isinstance(case, dict):
                continue
            handle = str(case.get("case_id") or "true")
            expr, refs = _render_conditions(case)
            branches.append(IfBranch(handle=handle, condition_expr=expr, refs=refs))
    elif isinstance(data.get("classes"), list):
        # question-classifier：每个 class 是一条分支，条件留给 codegen 用 LLM/规则占位。
        for cls in data["classes"]:
            if isinstance(cls, dict):
                handle = str(cls.get("id") or cls.get("name") or "class")
                branches.append(IfBranch(handle=handle, condition_expr=None, refs=()))
    return IfElseNode(id=node_id, title=title, branches=tuple(branches))


def _lower_template_transform(
    node_id: str, title: str, data: dict[str, Any]
) -> TemplateTransformNode:
    """template-transform：把 Jinja {{ var }} 按 variables 表替换成 {{#nodeId.field#}} 再归一。"""
    template_text = str(data.get("template", "") or "")
    var_map: dict[str, VarRef] = {}
    for v in data.get("variables", []) or []:
        if isinstance(v, dict):
            name = str(v.get("variable") or v.get("name") or "")
            ref = _selector_to_value(v.get("value_selector"))
            if name and isinstance(ref, VarRef):
                var_map[name] = ref
    template = _template_from_jinja(template_text, var_map)
    return TemplateTransformNode(id=node_id, title=title, template=template)


def _lower_iteration(
    node_id: str, title: str, data: dict[str, Any],
    body: list[DifyNode], all_edges: list[DifyEdge],
) -> IterationNode:
    """iteration：iterator/output 选择器归一 + 内层子图各自 lower 成嵌套 WorkflowIR。"""
    iterator = _selector_to_value(data.get("iterator_selector"))
    iterator_ref = iterator if isinstance(iterator, VarRef) else VarRef("", "")
    output_sel = _selector_to_value(data.get("output_selector"))
    output_ref = output_sel if isinstance(output_sel, VarRef) else None

    sub_ir: WorkflowIR | None = None
    if body:
        sub_node_ids = {n.id for n in body}
        sub_edges = [
            e for e in all_edges
            if e.source in sub_node_ids and e.target in sub_node_ids
        ]
        sub_nodes = tuple(_lower_node(n, [], all_edges) for n in body)
        sub_ir = _assemble("workflow", sub_nodes, sub_edges)

    is_parallel = bool(data.get("is_parallel", False))
    parallel_nums = _coerce_int(data.get("parallel_nums"), default=1)
    return IterationNode(
        id=node_id, title=title, iterator=iterator_ref, body=sub_ir,
        output=output_ref, is_parallel=is_parallel, parallel_nums=parallel_nums,
    )


def _lower_variable_aggregator(
    node_id: str, title: str, data: dict[str, Any]
) -> VariableAggregatorNode:
    """variable-aggregator：variables 选择器列表归一；分组模式逐组归一。"""
    groups: list[AggregatorGroup] = []
    adv = data.get("advanced_settings")
    if isinstance(adv, dict) and adv.get("group_enabled"):
        for g in adv.get("groups", []) or []:
            if not isinstance(g, dict):
                continue
            name = str(g.get("group_name") or g.get("groupId") or "")
            if name:
                groups.append(
                    AggregatorGroup(name=name, items=_selector_refs(g.get("variables")))
                )
    return VariableAggregatorNode(
        id=node_id, title=title,
        items=_selector_refs(data.get("variables")), groups=tuple(groups),
    )


def _selector_refs(raw: Any) -> tuple[VarRef, ...]:
    """一组 value_selector → VarRef 元组（非法项跳过，顺序保留声明序）。"""
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[VarRef] = []
    for sel in raw:
        ref = _selector_to_value(sel)
        if isinstance(ref, VarRef):
            out.append(ref)
    return tuple(out)


def _lower_assigner(node_id: str, title: str, data: dict[str, Any]) -> IRNode:
    """assigner（v2 items；兼容 v1 单条形状）：逐条赋值归一。

    未知操作符不猜语义：整节点落 UnsupportedNode（编译不失败，L0 闸拒跑）。
    """
    raw_items = data.get("items")
    if raw_items is None and data.get("assigned_variable_selector") is not None:
        # v1 旧形状：assigned_variable_selector + write_mode + input_variable_selector
        # → 归一成等价的单条 v2 item。
        raw_items = [{
            "variable_selector": data.get("assigned_variable_selector"),
            "operation": data.get("write_mode", "over-write"),
            "input_type": "variable",
            "value": data.get("input_variable_selector"),
        }]
    items: list[AssignItem] = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        target = _selector_to_value(raw.get("variable_selector"))
        if not isinstance(target, VarRef):
            continue  # 无合法写入目标的赋值项跳过（无处可写）
        operation = str(raw.get("operation", "over-write") or "over-write")
        if operation not in _ASSIGN_OPERATIONS:
            return UnsupportedNode(
                id=node_id, title=title, node_type="assigner",
                raw=tuple(sorted((k, v) for k, v in data.items() if k != "type")),
            )
        value: Value | None = None
        if operation not in _ASSIGN_VALUELESS:
            if str(raw.get("input_type", "variable") or "variable") == "constant":
                value = Literal(value=raw.get("value"))
            else:
                value = _selector_to_value(raw.get("value"))
        items.append(AssignItem(target=target, operation=operation, value=value))
    return VariableAssignerNode(id=node_id, title=title, items=tuple(items))


def _lower_document_extractor(
    node_id: str, title: str, data: dict[str, Any]
) -> DocumentExtractorNode:
    """document-extractor：variable_selector → source，is_array_file 透传。"""
    return DocumentExtractorNode(
        id=node_id, title=title,
        source=_selector_to_value(data.get("variable_selector")),
        is_array_file=bool(data.get("is_array_file", False)),
    )


def _lower_http_request(node_id: str, title: str, data: dict[str, Any]) -> IRNode:
    """http-request：url/headers/params 模板归一 + authorization 并入 headers。

    不支持的方法 / body 类型（form-data 多部分、binary、file 项）落 UnsupportedNode
    （编译不失败，L0 闸拒跑），不静默错发请求。
    """
    method = str(data.get("method", "get") or "get").lower()
    body_raw = data.get("body")
    body_type = "none"
    body_text = ""
    body_ok = True
    if isinstance(body_raw, dict):
        body_type = str(body_raw.get("type", "none") or "none") or "none"
        body_text, body_ok = _http_body_text(body_type, body_raw.get("data"))
    if method not in _HTTP_METHODS or body_type not in _HTTP_BODY_TYPES or not body_ok:
        return UnsupportedNode(
            id=node_id, title=title, node_type="http-request",
            raw=tuple(sorted((k, v) for k, v in data.items() if k != "type")),
        )
    headers_text = str(data.get("headers", "") or "")
    auth_line = _http_auth_line(data.get("authorization"))
    if auth_line:
        headers_text = f"{headers_text}\n{auth_line}" if headers_text else auth_line
    return HttpRequestNode(
        id=node_id, title=title, method=method,
        url=_template_from_text(str(data.get("url", "") or "")),
        headers=_template_from_text(headers_text),
        params=_template_from_text(str(data.get("params", "") or "")),
        body_type=body_type,
        body=_template_from_text(body_text) if body_type != "none" else None,
        timeout_s=_extract_http_timeout(data.get("timeout")),
        ssl_verify=bool(data.get("ssl_verify", True)),
    )


def _http_body_text(body_type: str, data: Any) -> tuple[str, bool]:
    """body 配置 → (正文文本, 是否可支持)。

    raw-text/json：字符串原样；新 DSL 的 items 取 text 项拼接。
    x-www-form-urlencoded：items 归一成与 headers/params 同款的 'k: v' 行（运行期 urlencode）。
    含 file 项 / 其它类型 → 不支持。
    """
    if body_type in ("", "none"):
        return "", True
    if body_type in ("raw-text", "json"):
        if isinstance(data, str):
            return data, True
        if isinstance(data, list):
            texts: list[str] = []
            for item in data:
                if not isinstance(item, dict) or str(item.get("type", "text")) != "text":
                    return "", False
                texts.append(str(item.get("value", "") or ""))
            return "\n".join(texts), True
        return "", True
    if body_type == "x-www-form-urlencoded":
        if isinstance(data, str):
            return data, True
        if isinstance(data, list):
            lines: list[str] = []
            for item in data:
                if not isinstance(item, dict) or str(item.get("type", "text")) != "text":
                    return "", False
                lines.append(f"{item.get('key', '')}: {item.get('value', '') or ''}")
            return "\n".join(lines), True
        return "", True
    return "", False  # form-data / binary：多部分编码与文件上传不支持


def _http_auth_line(auth: Any) -> str:
    """authorization 配置 → 一行 'Header: Value'（no-auth/缺失/无 key → ''）。"""
    if not isinstance(auth, dict) or str(auth.get("type", "no-auth")) != "api-key":
        return ""
    cfg = auth.get("config")
    if not isinstance(cfg, dict):
        return ""
    api_key = str(cfg.get("api_key", "") or "")
    if not api_key:
        return ""
    kind = str(cfg.get("type", "bearer") or "bearer")
    if kind == "basic":
        return f"Authorization: Basic {api_key}"
    if kind == "custom":
        header = str(cfg.get("header", "") or "") or "Authorization"
        return f"{header}: {api_key}"
    return f"Authorization: Bearer {api_key}"


def _extract_http_timeout(raw: Any) -> float | None:
    """http-request 超时配置 → 秒（缺失/非法 → None，由受控客户端用默认并统一钳制 ≤30s）。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    if isinstance(raw, dict):
        for key in ("max_read_timeout", "read", "read_timeout"):
            v = raw.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return float(v)
    return None


def _lower_loop(
    node_id: str, title: str, data: dict[str, Any],
    body: list[DifyNode], all_edges: list[DifyEdge],
) -> LoopNode:
    """loop：内层子图 lower + loop_count 钳制 + break 条件渲染 + 循环变量初值归一。"""
    sub_ir: WorkflowIR | None = None
    if body:
        sub_node_ids = {n.id for n in body}
        sub_edges = [
            e for e in all_edges
            if e.source in sub_node_ids and e.target in sub_node_ids
        ]
        sub_nodes = tuple(_lower_node(n, [], all_edges) for n in body)
        sub_ir = _assemble("workflow", sub_nodes, sub_edges)

    loop_count = _coerce_int(data.get("loop_count"), default=10)
    loop_count = max(0, min(loop_count, _LOOP_MAX_ROUNDS))
    break_expr, break_refs = _render_conditions({
        "conditions": data.get("break_conditions"),
        "logical_operator": data.get("logical_operator", "and"),
    })
    loop_vars: list[tuple[str, Value]] = []
    for v in data.get("loop_variables", []) or []:
        if not isinstance(v, dict):
            continue
        name = str(v.get("label") or v.get("name") or "")
        if not name:
            continue
        if str(v.get("value_type", "constant") or "constant") == "variable":
            loop_vars.append((name, _selector_to_value(v.get("value"))))
        else:
            loop_vars.append((name, Literal(value=v.get("value"))))
    return LoopNode(
        id=node_id, title=title, body=sub_ir, loop_count=loop_count,
        break_expr=break_expr, break_refs=break_refs, loop_vars=tuple(loop_vars),
    )


# ---------------------------------------------------------------------------
# 值/模板归一 helper。
# ---------------------------------------------------------------------------


def _selector_to_value(selector: Any) -> Value:
    """Dify value_selector（["nodeId","field", ...]）→ VarRef；非数组 → Literal。

    Dify 选择器首元素是来源节点 id（或 'sys'/'env' 等系统命名空间），其余拼成 field。
    """
    if isinstance(selector, (list, tuple)) and len(selector) >= 2:
        node_id = str(selector[0])
        field_name = ".".join(str(x) for x in selector[1:])
        return VarRef(node_id=node_id, field=field_name)
    if isinstance(selector, (list, tuple)) and len(selector) == 1:
        return VarRef(node_id=str(selector[0]), field="output")
    return Literal(value=selector)


def _template_from_text(text: str) -> TemplateValue:
    """把含 {{#nodeId.field#}} 的文本拆成 (str | VarRef) 序列。"""
    parts: list[str | VarRef] = []
    pos = 0
    for m in _TEMPLATE_REF.finditer(text):
        if m.start() > pos:
            parts.append(text[pos:m.start()])
        parts.append(_ref_from_token(m.group(1)))
        pos = m.end()
    if pos < len(text):
        parts.append(text[pos:])
    return TemplateValue(parts=tuple(parts))


def _template_from_jinja(text: str, var_map: dict[str, VarRef]) -> TemplateValue:
    """把 Jinja {{ var }} 按 var_map 替换成 VarRef，未在表中的 {{ var }} 原样保留为文本。"""
    parts: list[str | VarRef] = []
    pos = 0
    for m in _JINJA_VAR.finditer(text):
        name = m.group(1)
        if name not in var_map:
            continue  # 不是已知变量，留作普通文本（连同 {{ }}）
        if m.start() > pos:
            parts.append(text[pos:m.start()])
        parts.append(var_map[name])
        pos = m.end()
    if pos < len(text):
        parts.append(text[pos:])
    return TemplateValue(parts=tuple(parts))


def _ref_from_token(token: str) -> VarRef:
    """{{#nodeId.field#}} 内的 'nodeId.field' → VarRef（首段是节点 id，其余拼 field）。"""
    segments = [s for s in token.split(".") if s]
    if len(segments) >= 2:
        return VarRef(node_id=segments[0], field=".".join(segments[1:]))
    if len(segments) == 1:
        return VarRef(node_id=segments[0], field="output")
    return VarRef(node_id="", field="")


def _render_conditions(case: dict[str, Any]) -> tuple[str | None, tuple[VarRef, ...]]:
    """把一个 case 的 conditions 渲染成 Python 布尔表达式文本 + 收集 VarRef。

    生成形如 `_v("nodeId","field") > 60 and ...`；codegen 段把 `_v(...)` 实现为取变量。
    无 conditions（兜底 else 分支）→ (None, ())。
    """
    conditions = case.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return None, ()
    op_join = " and " if str(case.get("logical_operator", "and")) == "and" else " or "
    exprs: list[str] = []
    refs: list[VarRef] = []
    for cond in conditions:
        if not isinstance(cond, dict):
            continue
        sel = cond.get("variable_selector")
        ref = _selector_to_value(sel)
        if isinstance(ref, VarRef):
            refs.append(ref)
            lhs = f'_var({ref.node_id!r}, {ref.field!r})'
        else:
            lhs = repr(getattr(ref, "value", None))
        op = str(cond.get("comparison_operator", "=="))
        rhs_raw = cond.get("value", "")
        exprs.append(_condition_expr(lhs, op, rhs_raw))
    return (op_join.join(exprs) if exprs else None), tuple(refs)


def _condition_expr(lhs: str, op: str, rhs_raw: Any) -> str:
    """渲染单个比较为 Python 表达式（数值/字符串/包含等常见算子）。"""
    rhs = _render_rhs(rhs_raw)
    mapping = {
        "==": f"{lhs} == {rhs}", "is": f"{lhs} == {rhs}", "equals": f"{lhs} == {rhs}",
        "!=": f"{lhs} != {rhs}", "is not": f"{lhs} != {rhs}",
        ">": f"{_num(lhs)} > {_num(rhs)}", "<": f"{_num(lhs)} < {_num(rhs)}",
        "≥": f"{_num(lhs)} >= {_num(rhs)}", ">=": f"{_num(lhs)} >= {_num(rhs)}",
        "≤": f"{_num(lhs)} <= {_num(rhs)}", "<=": f"{_num(lhs)} <= {_num(rhs)}",
        "contains": f"{rhs} in {lhs}", "not contains": f"{rhs} not in {lhs}",
        "empty": f"not {lhs}", "not empty": f"bool({lhs})",
        "start with": f"str({lhs}).startswith({rhs})",
        "end with": f"str({lhs}).endswith({rhs})",
    }
    return mapping.get(op, f"{lhs} == {rhs}")


def _num(expr: str) -> str:
    """把一个表达式包成 float()（数值比较容错 str 输入）。"""
    return f"_as_num({expr})"


def _render_rhs(rhs_raw: Any) -> str:
    """右值字面量渲染：数字原样，其余按字符串字面量。"""
    if isinstance(rhs_raw, bool):
        return repr(rhs_raw)
    if isinstance(rhs_raw, (int, float)):
        return repr(rhs_raw)
    return repr(str(rhs_raw))


def _extract_max_tokens(model: Any) -> int | None:
    """从 llm 节点 model 配置取 completion_params.max_tokens（缺失 → None）。"""
    if not isinstance(model, dict):
        return None
    params = model.get("completion_params")
    if isinstance(params, dict) and "max_tokens" in params:
        return _coerce_int(params.get("max_tokens"), default=0) or None
    return None


def _coerce_int(value: Any, *, default: int) -> int:
    """把 value 容错转 int（None/非数 → default）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default
