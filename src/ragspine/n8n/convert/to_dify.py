"""n8n → dify 核心转换：N8nWorkflow → Dify DSL dict（+ warnings）。

算法要点（详见子包宪章 CLAUDE.md）：
1. ai attachment 归并：经非 main 连接（ai_languageModel 等）指向他节点的节点从主图剥离，
   lmChat* 的 model 信息并入宿主 llm 节点，原始 JSON 存宿主 data._n8n["ai_attachments"]。
2. noOp splice：剔除节点、上游直连下游（保留上游端口）。
3. 节点映射：dify node id = name 的确定性 snake_case；data._n8n = 原始 n8n 节点完整 dict
   （round-trip 无损机制）；未知类型落自定义 dify type "n8n-passthrough"。
4. 终端接合成 end（data._n8n={"synthetic": True}）；无 trigger 时合成 start。
5. 后置 pass：全 dict 收集对 start 节点的引用（模板 {{#id.field#}} 与 value_selector），
   声明进 start.variables，编译后 Inputs 才有对应字段。
"""

from __future__ import annotations

import re
from typing import Any

from ragspine.n8n.convert.mapping import (
    DEFAULT_OUTPUT_FIELD,
    DIFY_OUTPUT_FIELD,
    LMCHAT_PREFIX,
    LMCHAT_TO_PROVIDER,
    N8N_TO_DIFY_OPERATOR,
    N8N_TO_DIFY_TYPE,
    NOOP_TYPE,
    PASSTHROUGH_DIFY_TYPE,
)
from ragspine.n8n.convert.variables import (
    n8n_expr_to_selector,
    n8n_value_to_dify,
)
from ragspine.n8n.parse.schema import N8nNode, N8nWorkflow

_DIFY_REF = re.compile(r"\{\{#\s*([^#}]+?)\s*#\}\}")
_SELECTOR_KEYS = frozenset({"value_selector", "variable_selector"})


def convert_to_dify(workflow: N8nWorkflow) -> tuple[dict[str, Any], list[str]]:
    """把校验过的 N8nWorkflow 转换为 Dify DSL dict。返回 (dify dict, warnings)。"""
    warnings: list[str] = []
    nodes_by_name: dict[str, N8nNode] = {node.name: node for node in workflow.nodes}
    raw_by_name: dict[str, dict[str, Any]] = {
        node.name: node.to_raw() for node in workflow.nodes
    }

    # 1. connections 摊平：主链边 (source, 端口, target) + 非 main 的 attachment 归并表。
    main_edges: list[tuple[str, int, str]] = []
    attachments: dict[str, list[tuple[str, str]]] = {}  # 宿主 name → [(连接类型, 附属 name)]
    attached_names: set[str] = set()
    for source_name, conn_types in workflow.connections.items():
        if not isinstance(conn_types, dict):
            continue
        for conn_type, ports in conn_types.items():
            if not isinstance(ports, list):
                continue
            for port_idx, port in enumerate(ports):
                for target in port or []:
                    target_name = str(target["node"])
                    if conn_type == "main":
                        main_edges.append((source_name, port_idx, target_name))
                    else:
                        attachments.setdefault(target_name, []).append(
                            (str(conn_type), source_name)
                        )
                        attached_names.add(source_name)

    # 2. noOp splice：剔除节点，上游（保端口）直连下游。
    main_nodes = [
        node for node in workflow.nodes
        if node.name not in attached_names and node.type != NOOP_TYPE
    ]
    for node in workflow.nodes:
        if node.type == NOOP_TYPE and node.name not in attached_names:
            main_edges = _splice_out(main_edges, node.name)
            warnings.append(f"noOp 节点 {node.name!r} 已剔除，上游直连下游")

    # 3. dify id 分配（确定性 snake_case，冲突追加 _2/_3）。
    used_ids: set[str] = set()
    name_to_id: dict[str, str] = {}
    for index, node in enumerate(main_nodes):
        name_to_id[node.name] = _assign_id(node, index, used_ids)
    dify_type_by_name = {node.name: _dify_type(node.type) for node in main_nodes}
    llm_ids = {
        name_to_id[name] for name, dtype in dify_type_by_name.items() if dtype == "llm"
    }

    # 每个节点的唯一主链上游（$json 引用解析用）。
    incoming: dict[str, set[str]] = {}
    for source_name, _port, target_name in main_edges:
        incoming.setdefault(target_name, set()).add(source_name)

    # 4. 节点映射。
    dify_nodes: list[dict[str, Any]] = []
    for index, node in enumerate(main_nodes):
        sources = incoming.get(node.name, set())
        upstream_id = name_to_id[next(iter(sources))] if len(sources) == 1 else None
        data = _build_data(
            node,
            dify_type=dify_type_by_name[node.name],
            upstream_id=upstream_id,
            name_to_id=name_to_id,
            llm_ids=llm_ids,
            warnings=warnings,
        )
        n8n_raw = dict(raw_by_name[node.name])
        _merge_attachments(
            data, n8n_raw, node, attachments.get(node.name, []),
            nodes_by_name=nodes_by_name, raw_by_name=raw_by_name, warnings=warnings,
        )
        data["_n8n"] = n8n_raw
        dify_nodes.append({
            "id": name_to_id[node.name],
            "position": _position(node, index),
            "data": data,
        })

    # 5. 边（sourceHandle：if 端口 0/1 → true/false；switch 端口 i → branch_i）。
    dify_edges: list[dict[str, Any]] = []
    for source_name, port, target_name in main_edges:
        dify_edges.append({
            "source": name_to_id[source_name],
            "target": name_to_id[target_name],
            "sourceHandle": _source_handle(nodes_by_name[source_name].type, port),
        })

    # 6. 无 trigger 合成 start；终端接合成 end。
    _add_synthetic_start(dify_nodes, dify_edges, used_ids, warnings)
    _add_synthetic_end(dify_nodes, dify_edges, used_ids)

    # 7. 顶层组装 + start.variables 后置 pass。
    doc: dict[str, Any] = {
        "app": {"mode": "workflow", "name": workflow.name or "n8n-import"},
        "kind": "app",
        "version": "0.1.5",
        "workflow": {"graph": {"nodes": dify_nodes, "edges": dify_edges}},
    }
    extras = dict(workflow.model_extra or {})
    if extras:
        doc["x_n8n"] = extras
    _declare_start_variables(doc, dify_nodes)
    return doc, warnings


# ---------------------------------------------------------------------------
# 图变换 helper。
# ---------------------------------------------------------------------------


def _splice_out(
    edges: list[tuple[str, int, str]], name: str
) -> list[tuple[str, int, str]]:
    """把 name 从边表中剔除：每条入边 ×（每条出边的 target）直连，保留入边端口。"""
    incoming = [(s, p) for (s, p, t) in edges if t == name]
    outgoing_targets = [t for (s, _p, t) in edges if s == name]
    kept = [(s, p, t) for (s, p, t) in edges if s != name and t != name]
    for source, port in incoming:
        for target in outgoing_targets:
            kept.append((source, port, target))
    return kept


def _assign_id(node: N8nNode, index: int, used: set[str]) -> str:
    """name 的确定性 snake_case → dify id；非 ASCII 兜底 n8n 原 id 或序号；冲突 _2/_3。"""
    slug = _slugify(node.name) or _slugify(node.id) or f"node_{index + 1}"
    candidate, suffix = slug, 2
    while candidate in used:
        candidate = f"{slug}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _dify_type(n8n_type: str) -> str:
    mapped = N8N_TO_DIFY_TYPE.get(n8n_type)
    if mapped is not None:
        return mapped
    if n8n_type.startswith(LMCHAT_PREFIX):
        return "llm"  # lmChat 独立出现时按 llm 处理
    return PASSTHROUGH_DIFY_TYPE


def _position(node: N8nNode, index: int) -> dict[str, Any]:
    if node.position is not None and len(node.position) >= 2:
        return {"x": node.position[0], "y": node.position[1]}
    return {"x": index * 220, "y": 0}


def _source_handle(source_type: str, port: int) -> str:
    if source_type == "n8n-nodes-base.if":
        return "true" if port == 0 else "false"
    if source_type == "n8n-nodes-base.switch":
        return f"branch_{port}"
    return "source"


# ---------------------------------------------------------------------------
# 各类型节点 data 构建。
# ---------------------------------------------------------------------------


def _build_data(
    node: N8nNode,
    *,
    dify_type: str,
    upstream_id: str | None,
    name_to_id: dict[str, str],
    llm_ids: set[str],
    warnings: list[str],
) -> dict[str, Any]:
    data: dict[str, Any] = {"type": dify_type, "title": node.name}
    params = node.parameters
    if dify_type == "start":
        data["variables"] = []
    elif dify_type == "llm":
        _fill_llm(data, node, upstream_id, name_to_id, llm_ids, warnings)
    elif dify_type == "if-else":
        data["cases"] = _convert_if_cases(
            node, params, upstream_id, name_to_id, llm_ids, warnings
        )
    elif dify_type == "question-classifier":
        data["classes"] = _convert_switch_classes(node, params, warnings)
    elif dify_type == "code":
        _fill_code(data, node, params, warnings)
    elif dify_type == "template-transform":
        _fill_template_transform(
            data, node, params, upstream_id, name_to_id, llm_ids, warnings
        )
    else:  # n8n-passthrough
        warnings.append(
            f"未知 n8n 节点类型 {node.type!r}（{node.name!r}），"
            f"映射为 {PASSTHROUGH_DIFY_TYPE} 并保留原始数据"
        )
    return data


def _fill_llm(
    data: dict[str, Any],
    node: N8nNode,
    upstream_id: str | None,
    name_to_id: dict[str, str],
    llm_ids: set[str],
    warnings: list[str],
) -> None:
    params = node.parameters

    def _convert(value: object) -> str:
        converted, warns = n8n_value_to_dify(
            value, upstream_id=upstream_id, name_to_id=name_to_id, llm_node_ids=llm_ids
        )
        warnings.extend(warns)
        return "" if converted is None else str(converted)

    prompt: list[dict[str, str]] = []
    if node.type == "@n8n/n8n-nodes-langchain.openAi":
        # openAi 节点：model 名从 modelId，消息从 messages.values 尽力取。
        data["model"] = {
            "provider": "openai",
            "name": _model_name_from(params, keys=("modelId", "model")),
            "completion_params": {},
        }
        messages = params.get("messages")
        values = messages.get("values") if isinstance(messages, dict) else None
        for message in values if isinstance(values, list) else []:
            if not isinstance(message, dict):
                continue
            text = message.get("content") or message.get("message") or ""
            prompt.append({
                "role": str(message.get("role", "user") or "user"),
                "text": _convert(text),
            })
    elif node.type.startswith(LMCHAT_PREFIX):
        # lmChat 独立出现：只有 model 信息。
        data["model"] = {
            "provider": _provider_from_type(node.type),
            "name": _model_name_from(params, keys=("model", "modelId")),
            "completion_params": {},
        }
    else:
        # agent：system(options.systemMessage) + user(text)。
        options = params.get("options")
        system_message = options.get("systemMessage") if isinstance(options, dict) else None
        if isinstance(system_message, str) and system_message:
            prompt.append({"role": "system", "text": _convert(system_message)})
        prompt.append({"role": "user", "text": _convert(params.get("text", ""))})
    data["prompt_template"] = prompt


def _convert_if_cases(
    node: N8nNode,
    params: dict[str, Any],
    upstream_id: str | None,
    name_to_id: dict[str, str],
    llm_ids: set[str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    config = params.get("conditions")
    if not isinstance(config, dict):
        config = {}
    conditions_out: list[dict[str, Any]] = []
    raw_conditions = config.get("conditions")
    for condition in raw_conditions if isinstance(raw_conditions, list) else []:
        if not isinstance(condition, dict):
            continue
        left_value = condition.get("leftValue")
        selector = n8n_expr_to_selector(
            left_value, upstream_id=upstream_id, name_to_id=name_to_id,
            llm_node_ids=llm_ids,
        )
        operator = condition.get("operator")
        operation = (
            str(operator.get("operation", "equals") or "equals")
            if isinstance(operator, dict) else "equals"
        )
        dify_operator = N8N_TO_DIFY_OPERATOR.get(operation)
        if dify_operator is None:
            dify_operator = operation
            warnings.append(
                f"if 节点 {node.name!r} 的 operation {operation!r} 无对应 dify 算子，原样保留"
            )
        right_value = condition.get("rightValue", "")
        entry: dict[str, Any] = {
            "comparison_operator": dify_operator,
            "value": "" if right_value is None else str(right_value),
        }
        if selector is not None:
            entry["variable_selector"] = selector
        else:
            entry["variable_selector"] = left_value  # 保留 Literal
            warnings.append(
                f"if 节点 {node.name!r} 的 leftValue 无法解析为变量引用，"
                f"保留字面量：{left_value!r}"
            )
        conditions_out.append(entry)
    return [{
        "case_id": "true",
        "logical_operator": str(config.get("combinator", "and") or "and"),
        "conditions": conditions_out,
    }]


def _convert_switch_classes(
    node: N8nNode, params: dict[str, Any], warnings: list[str]
) -> list[dict[str, Any]]:
    rules = params.get("rules")
    values = rules.get("values") if isinstance(rules, dict) else None
    classes: list[dict[str, Any]] = []
    for index, rule in enumerate(values if isinstance(values, list) else []):
        label = ""
        if isinstance(rule, dict):
            for key in ("outputKey", "renameOutput"):
                candidate = rule.get(key)
                if isinstance(candidate, str) and candidate:
                    label = candidate
                    break
            if not label:
                label = str(rule.get("conditions", "") or "")[:40]
        classes.append({"id": f"branch_{index}", "name": label or f"branch_{index}"})
    warnings.append(
        f"switch 节点 {node.name!r} 近似映射为 question-classifier，分支语义可能有出入"
    )
    return classes


def _fill_code(
    data: dict[str, Any], node: N8nNode, params: dict[str, Any], warnings: list[str]
) -> None:
    language = str(params.get("language", "") or "")
    if language == "python":
        data["code_language"] = "python3"
        data["code"] = str(params.get("pythonCode", "") or "")
    else:
        data["code_language"] = "javascript"
        data["code"] = str(params.get("jsCode", "") or "")
        warnings.append(f"code 节点 {node.name!r}：JavaScript 代码保留原文，未转 Python")
    data["variables"] = []
    data["outputs"] = {"result": {"type": "string"}}


def _fill_template_transform(
    data: dict[str, Any],
    node: N8nNode,
    params: dict[str, Any],
    upstream_id: str | None,
    name_to_id: dict[str, str],
    llm_ids: set[str],
    warnings: list[str],
) -> None:
    assignments = _extract_assignments(params)
    variables: list[dict[str, Any]] = []
    rendered: list[tuple[str, str]] = []
    for assign_name, value in assignments:
        if isinstance(value, str) and value.startswith("="):
            selector = n8n_expr_to_selector(
                value, upstream_id=upstream_id, name_to_id=name_to_id,
                llm_node_ids=llm_ids,
            )
            if selector is not None:
                variables.append({"variable": assign_name, "value_selector": selector})
                rendered.append((assign_name, "{{ " + assign_name + " }}"))
            else:
                rendered.append((assign_name, value))  # 原文内联
                warnings.append(
                    f"set 节点 {node.name!r} 的赋值 {assign_name!r} 表达式无法解析，原文内联"
                )
        else:
            rendered.append((assign_name, "" if value is None else str(value)))
    if len(rendered) == 1:
        template = rendered[0][1]
    else:
        template = "\n".join(f"{name}: {text}" for name, text in rendered)
    data["template"] = template
    data["variables"] = variables


def _extract_assignments(params: dict[str, Any]) -> list[tuple[str, Any]]:
    """set 节点赋值表：v3 parameters.assignments.assignments；兼容 legacy values.string。"""
    result: list[tuple[str, Any]] = []
    assignments = params.get("assignments")
    if isinstance(assignments, dict):
        for item in assignments.get("assignments") or []:
            if isinstance(item, dict) and item.get("name") is not None:
                result.append((str(item["name"]), item.get("value")))
    if not result:
        legacy = params.get("values")
        if isinstance(legacy, dict):
            for item in legacy.get("string") or []:
                if isinstance(item, dict) and item.get("name") is not None:
                    result.append((str(item["name"]), item.get("value")))
    return result


# ---------------------------------------------------------------------------
# ai attachment 归并。
# ---------------------------------------------------------------------------


def _merge_attachments(
    data: dict[str, Any],
    n8n_raw: dict[str, Any],
    host: N8nNode,
    entries: list[tuple[str, str]],
    *,
    nodes_by_name: dict[str, N8nNode],
    raw_by_name: dict[str, dict[str, Any]],
    warnings: list[str],
) -> None:
    if not entries:
        return
    stored: list[dict[str, Any]] = []
    for conn_type, attachment_name in entries:
        attachment = nodes_by_name[attachment_name]
        stored.append({"connection_type": conn_type, "node": raw_by_name[attachment_name]})
        if (
            conn_type == "ai_languageModel"
            and data.get("type") == "llm"
            and attachment.type.startswith(LMCHAT_PREFIX)
        ):
            data["model"] = {
                "provider": _provider_from_type(attachment.type),
                "name": _model_name_from(attachment.parameters, keys=("model", "modelId")),
                "completion_params": {},
            }
            warnings.append(
                f"lmChat 节点 {attachment_name!r} 已并入 llm 节点 {host.name!r} 的 model 配置"
            )
        else:
            warnings.append(
                f"n8n {conn_type} 附属节点 {attachment_name!r} 无 dify 对应概念，"
                f"原始数据存于宿主节点 _n8n.ai_attachments"
            )
    n8n_raw["ai_attachments"] = stored


def _provider_from_type(n8n_type: str) -> str:
    """lmChat 型号 → provider：查表优先，兜底取 lmChat 后缀小写。"""
    suffix = n8n_type.rsplit(".", 1)[-1]
    mapped = LMCHAT_TO_PROVIDER.get(suffix)
    if mapped is not None:
        return mapped
    return suffix.removeprefix("lmChat").lower() or "openai"


def _model_name_from(params: dict[str, Any], *, keys: tuple[str, ...]) -> str:
    """model 名尽力取：str 直取；resource-locator dict 取 .value。"""
    for key in keys:
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            inner = value.get("value")
            if isinstance(inner, str) and inner:
                return inner
    return ""


# ---------------------------------------------------------------------------
# 合成 start / end 与后置 start.variables pass。
# ---------------------------------------------------------------------------


def _add_synthetic_start(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    used_ids: set[str],
    warnings: list[str],
) -> None:
    if any(node["data"].get("type") == "start" for node in nodes):
        return
    if not nodes:
        return
    start_id = _fresh_id("start", used_ids)
    has_incoming = {edge["target"] for edge in edges}
    roots = [node["id"] for node in nodes if node["id"] not in has_incoming]
    nodes.insert(0, {
        "id": start_id,
        "position": {"x": -220, "y": 0},
        "data": {"type": "start", "title": "Start", "variables": [],
                 "_n8n": {"synthetic": True}},
    })
    for root in roots:
        edges.append({"source": start_id, "target": root, "sourceHandle": "source"})
    warnings.append("工作流缺少 trigger 节点，已合成 start 节点")


def _add_synthetic_end(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], used_ids: set[str]
) -> None:
    has_outgoing = {edge["source"] for edge in edges}
    terminals = [
        node for node in nodes
        if node["id"] not in has_outgoing and node["data"].get("type") != "start"
    ]
    if not terminals:
        return
    end_id = _fresh_id("end", used_ids)
    outputs = [
        {
            "variable": terminal["id"],
            "value_selector": [
                terminal["id"],
                DIFY_OUTPUT_FIELD.get(str(terminal["data"].get("type")), DEFAULT_OUTPUT_FIELD),
            ],
        }
        for terminal in terminals
    ]
    nodes.append({
        "id": end_id,
        "position": {"x": 0, "y": 0},
        "data": {"type": "end", "title": "End", "outputs": outputs,
                 "_n8n": {"synthetic": True}},
    })
    for terminal in terminals:
        edges.append({"source": terminal["id"], "target": end_id, "sourceHandle": "source"})


def _fresh_id(base: str, used: set[str]) -> str:
    suffix = 1
    while f"{base}_{suffix}" in used:
        suffix += 1
    fresh = f"{base}_{suffix}"
    used.add(fresh)
    return fresh


def _declare_start_variables(doc: dict[str, Any], nodes: list[dict[str, Any]]) -> None:
    """后置 pass：收集全 dict 对 start 的引用，声明进对应 start.variables。"""
    start_nodes = [node for node in nodes if node["data"].get("type") == "start"]
    if not start_nodes:
        return
    start_ids = {node["id"] for node in start_nodes}
    referenced: dict[str, set[str]] = {start_id: set() for start_id in start_ids}
    _collect_start_refs(doc, start_ids, referenced)
    for node in start_nodes:
        fields = referenced[node["id"]]
        if not fields:
            continue
        variables = node["data"].setdefault("variables", [])
        existing = {
            str(variable.get("variable"))
            for variable in variables if isinstance(variable, dict)
        }
        for field in sorted(fields - existing):
            variables.append({
                "variable": field,
                "label": field,
                "type": "text-input",
                "required": False,
            })


def _collect_start_refs(
    obj: object, start_ids: set[str], referenced: dict[str, set[str]]
) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "_n8n":
                continue  # 原始 n8n 数据里没有 dify 引用
            if (
                key in _SELECTOR_KEYS
                and isinstance(value, list)
                and len(value) >= 2
                and str(value[0]) in start_ids
            ):
                referenced[str(value[0])].add(str(value[1]))
                continue
            _collect_start_refs(value, start_ids, referenced)
    elif isinstance(obj, list):
        for item in obj:
            _collect_start_refs(item, start_ids, referenced)
    elif isinstance(obj, str):
        for match in _DIFY_REF.finditer(obj):
            node_id, _, field = match.group(1).partition(".")
            if node_id in start_ids and field:
                referenced[node_id].add(field.split(".", 1)[0])
