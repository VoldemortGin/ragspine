"""dify → n8n 核心转换：Dify DSL dict → n8n workflow dict（+ warnings）。

还原策略（详见子包宪章 CLAUDE.md）：
- data._n8n 且非 synthetic → 完整还原原 n8n 节点（round-trip 无损），position 从 dify
  节点覆盖回 [x, y]；_n8n.ai_attachments 还原为独立节点 + 对应非 main 连接。
- 带 _n8n.synthetic 标记的合成 start/end → 直接剔除（连其边），不出 warning。
- 无 _n8n → 按映射表新建（start→manualTrigger、if-else→if、llm→agent+配套 lmChat、
  template-transform→set、code→code、question-classifier→switch 近似、answer/end→noOp、
  未知类型→noOp），原始 data 存 notes（JSON 字符串）+ warning。
- connections 反向构建：sourceHandle true/false → if 端口 0/1；question-classifier
  class id → classes 序 index；main 数组按最大端口补齐空 list。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ragspine.n8n.convert.mapping import (
    DEFAULT_LMCHAT,
    DIFY_TO_N8N_OPERATOR,
    DIFY_TO_N8N_TYPE,
    NOOP_TYPE,
    PROVIDER_TO_LMCHAT,
)
from ragspine.n8n.convert.variables import dify_text_to_n8n


@dataclass
class _NodeInfo:
    """一个 dify 节点的转换期视图。"""

    dify_id: str
    dify_type: str
    title: str
    data: dict[str, Any]
    node: dict[str, Any]
    index: int
    n8n_meta: dict[str, Any] | None
    synthetic: bool
    name: str = ""
    class_index: dict[str, int] = field(default_factory=dict)


def convert_to_n8n(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把校验过的 Dify DSL dict 转换为 n8n workflow dict。返回 (n8n dict, warnings)。"""
    warnings: list[str] = []
    graph = _graph_of(doc)
    infos = _collect_infos(graph)
    by_id = {info.dify_id: info for info in infos}

    # 命名：先注册 _n8n 还原节点的原名（原图内本就唯一），再给新建节点分配唯一 name。
    used_names: set[str] = set()
    for info in infos:
        if info.synthetic:
            continue
        if info.n8n_meta is not None:
            base = str(info.n8n_meta.get("name") or info.title or info.dify_id)
            info.name = _unique_name(base, used_names)
    for info in infos:
        if not info.synthetic and not info.name:
            info.name = _unique_name(info.title or info.dify_id, used_names)

    id_to_name = {info.dify_id: info.name for info in infos if not info.synthetic}
    llm_ids = frozenset(
        info.dify_id for info in infos if info.dify_type == "llm" and not info.synthetic
    )

    # 节点构建：还原 / 新建；收集 ai 连接（attachment name, 连接类型, 宿主 name）。
    nodes_out: list[dict[str, Any]] = []
    ai_connections: list[tuple[str, str, str]] = []
    for info in infos:
        if info.synthetic:
            continue
        if info.n8n_meta is not None:
            nodes_out.append(_restore_node(info))
            _restore_attachments(info, nodes_out, ai_connections, used_names)
        else:
            nodes_out.extend(
                _build_new_node(
                    info, id_to_name=id_to_name, llm_ids=llm_ids,
                    ai_connections=ai_connections, used_names=used_names,
                    warnings=warnings,
                )
            )

    # connections 反向构建。
    connections: dict[str, Any] = {}
    edges = graph.get("edges")
    for edge in edges if isinstance(edges, list) else []:
        if not isinstance(edge, dict):
            continue
        source = by_id.get(str(edge.get("source")))
        target = by_id.get(str(edge.get("target")))
        if source is None or target is None or source.synthetic or target.synthetic:
            continue
        port = _port_of(source, str(edge.get("sourceHandle") or "source"))
        conn_types = connections.setdefault(source.name, {})
        main_ports: list[list[dict[str, Any]]] = conn_types.setdefault("main", [])
        while len(main_ports) <= port:
            main_ports.append([])
        main_ports[port].append({"node": target.name, "type": "main", "index": 0})
    for attachment_name, conn_type, host_name in ai_connections:
        connections.setdefault(attachment_name, {})[conn_type] = [[
            {"node": host_name, "type": conn_type, "index": 0}
        ]]

    # 顶层：有 x_n8n 则以其为基底合并还原。
    x_n8n = doc.get("x_n8n")
    result: dict[str, Any] = dict(x_n8n) if isinstance(x_n8n, dict) else {}
    app = doc.get("app")
    result["name"] = str(app.get("name", "") or "") if isinstance(app, dict) else ""
    result["nodes"] = nodes_out
    result["connections"] = connections
    result.setdefault("settings", {"executionOrder": "v1"})
    result.setdefault("pinData", {})
    return result, warnings


# ---------------------------------------------------------------------------
# 收集与还原。
# ---------------------------------------------------------------------------


def _graph_of(doc: dict[str, Any]) -> dict[str, Any]:
    workflow = doc.get("workflow")
    graph = workflow.get("graph") if isinstance(workflow, dict) else None
    return graph if isinstance(graph, dict) else {}


def _collect_infos(graph: dict[str, Any]) -> list[_NodeInfo]:
    infos: list[_NodeInfo] = []
    nodes = graph.get("nodes")
    for index, node in enumerate(nodes if isinstance(nodes, list) else []):
        if not isinstance(node, dict):
            continue
        data = node.get("data")
        if not isinstance(data, dict):
            data = {}
        meta = data.get("_n8n")
        n8n_meta = meta if isinstance(meta, dict) else None
        synthetic = bool(n8n_meta.get("synthetic")) if n8n_meta is not None else False
        info = _NodeInfo(
            dify_id=str(node.get("id", "") or f"node_{index}"),
            dify_type=str(data.get("type", "") or ""),
            title=str(data.get("title", "") or ""),
            data=data,
            node=node,
            index=index,
            n8n_meta=n8n_meta,
            synthetic=synthetic,
        )
        if info.dify_type == "question-classifier":
            classes = data.get("classes")
            for class_idx, cls in enumerate(classes if isinstance(classes, list) else []):
                if isinstance(cls, dict):
                    info.class_index[str(cls.get("id"))] = class_idx
        infos.append(info)
    return infos


def _port_of(source: _NodeInfo, handle: str) -> int:
    """sourceHandle → n8n 输出端口：if 的 true/false → 0/1；classifier 按 classes 序。"""
    if source.dify_type == "if-else":
        return 1 if handle == "false" else 0
    if source.dify_type == "question-classifier":
        return source.class_index.get(handle, 0)
    return 0


def _unique_name(base: str, used: set[str]) -> str:
    name = base or "Node"
    suffix = 2
    while name in used:
        name = f"{base} {suffix}"
        suffix += 1
    used.add(name)
    return name


def _restore_node(info: _NodeInfo) -> dict[str, Any]:
    assert info.n8n_meta is not None
    raw = {
        key: value for key, value in info.n8n_meta.items()
        if key not in ("ai_attachments", "synthetic")
    }
    raw["name"] = info.name
    position = _position_of(info)
    if position is not None:
        raw["position"] = position
    return raw


def _restore_attachments(
    info: _NodeInfo,
    nodes_out: list[dict[str, Any]],
    ai_connections: list[tuple[str, str, str]],
    used_names: set[str],
) -> None:
    assert info.n8n_meta is not None
    attachments = info.n8n_meta.get("ai_attachments")
    for entry in attachments if isinstance(attachments, list) else []:
        if not isinstance(entry, dict):
            continue
        attachment_raw = entry.get("node")
        if not isinstance(attachment_raw, dict):
            continue
        conn_type = str(entry.get("connection_type", "ai_languageModel") or "ai_languageModel")
        restored = dict(attachment_raw)
        restored["name"] = _unique_name(str(restored.get("name") or "Model"), used_names)
        nodes_out.append(restored)
        ai_connections.append((restored["name"], conn_type, info.name))


def _position_of(info: _NodeInfo) -> list[int | float] | None:
    position = info.node.get("position")
    if isinstance(position, dict) and "x" in position and "y" in position:
        x, y = position["x"], position["y"]
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            return [x, y]
    return None


# ---------------------------------------------------------------------------
# 新建节点（无 _n8n 时按映射表）。
# ---------------------------------------------------------------------------


def _build_new_node(
    info: _NodeInfo,
    *,
    id_to_name: dict[str, str],
    llm_ids: frozenset[str],
    ai_connections: list[tuple[str, str, str]],
    used_names: set[str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """按映射表新建 n8n 节点；llm 可能额外产一个配套 lmChat 节点。"""
    label = info.title or info.dify_id
    position = _position_of(info) or [info.index * 220, 0]
    mapped = DIFY_TO_N8N_TYPE.get(info.dify_type)
    if mapped is None:
        warnings.append(
            f"未知 dify 节点类型 {info.dify_type!r}（{label!r}），"
            f"映射为 noOp，原始 data 存于 notes"
        )
        return [_noop_node(info, position)]
    n8n_type, type_version = mapped

    node: dict[str, Any] = {
        "id": info.dify_id,
        "name": info.name,
        "type": n8n_type,
        "typeVersion": type_version,
        "position": position,
        "parameters": {},
    }
    extra_nodes: list[dict[str, Any]] = []

    def _to_expr(text: str) -> str:
        converted, warns = dify_text_to_n8n(
            text, id_to_name=id_to_name, llm_node_ids=llm_ids
        )
        warnings.extend(warns)
        return converted

    if info.dify_type in ("answer", "end"):
        warnings.append(
            f"dify {info.dify_type} 节点 {label!r} 映射为 noOp，原始 data 存于 notes"
        )
        return [_noop_node(info, position)]
    if info.dify_type == "if-else":
        node["parameters"] = _if_parameters(info, _to_expr, warnings)
    elif info.dify_type == "question-classifier":
        node["parameters"] = _switch_parameters(info)
        warnings.append(
            f"question-classifier 节点 {label!r} 近似映射为 switch，分支语义可能有出入"
        )
    elif info.dify_type == "code":
        node["parameters"] = _code_parameters(info)
        warnings.append(f"code 节点 {label!r}：代码原文还原，语义可能需人工调整")
    elif info.dify_type == "template-transform":
        node["parameters"] = _set_parameters(info, _to_expr)
    elif info.dify_type == "llm":
        node["parameters"] = _agent_parameters(info, _to_expr)
        companion = _companion_lmchat(info, used_names)
        if companion is not None:
            extra_nodes.append(companion)
            ai_connections.append((companion["name"], "ai_languageModel", info.name))
    # start → manualTrigger：parameters 留空即可。
    return [node, *extra_nodes]


def _noop_node(info: _NodeInfo, position: list[int | float]) -> dict[str, Any]:
    return {
        "id": info.dify_id,
        "name": info.name,
        "type": NOOP_TYPE,
        "typeVersion": 1,
        "position": position,
        "parameters": {},
        "notes": json.dumps(info.data, ensure_ascii=False),
    }


def _if_parameters(
    info: _NodeInfo, to_expr: Any, warnings: list[str]
) -> dict[str, Any]:
    cases = info.data.get("cases")
    case = next(
        (c for c in (cases if isinstance(cases, list) else []) if isinstance(c, dict)), {}
    )
    conditions_out: list[dict[str, Any]] = []
    raw_conditions = case.get("conditions")
    for index, condition in enumerate(
        raw_conditions if isinstance(raw_conditions, list) else []
    ):
        if not isinstance(condition, dict):
            continue
        selector = condition.get("variable_selector")
        if isinstance(selector, list) and len(selector) >= 2:
            ref = "{{#" + str(selector[0]) + "." + ".".join(str(s) for s in selector[1:]) + "#}}"
            left_value: Any = to_expr(ref)
        else:
            left_value = selector  # 正向转换时保留的 Literal（原 n8n 表达式/字面量）
        operator = str(condition.get("comparison_operator", "==") or "==")
        mapped_operator = DIFY_TO_N8N_OPERATOR.get(operator)
        if mapped_operator is None:
            mapped_operator = ("string", operator)
            warnings.append(
                f"if-else 节点 {info.title or info.dify_id!r} 的算子 {operator!r} "
                f"无对应 n8n operation，原样保留"
            )
        operator_type, operation = mapped_operator
        conditions_out.append({
            "id": f"cond-{index + 1}",
            "leftValue": left_value,
            "rightValue": _typed_value(condition.get("value", ""), operator_type),
            "operator": {"type": operator_type, "operation": operation},
        })
    return {
        "conditions": {
            "options": {"caseSensitive": True, "typeValidation": "loose", "version": 2},
            "conditions": conditions_out,
            "combinator": str(case.get("logical_operator", "and") or "and"),
        },
        "options": {},
    }


def _typed_value(value: Any, operator_type: str) -> Any:
    """数值算子的右值尽力转数（int 优先），转不动原样返回。"""
    if operator_type != "number" or not isinstance(value, str):
        return value
    try:
        return int(value.strip())
    except ValueError:
        try:
            return float(value.strip())
        except ValueError:
            return value


def _switch_parameters(info: _NodeInfo) -> dict[str, Any]:
    classes = info.data.get("classes")
    values = [
        {
            "outputKey": str(cls.get("name") or cls.get("id") or f"branch_{index}"),
            "renameOutput": True,
        }
        for index, cls in enumerate(classes if isinstance(classes, list) else [])
        if isinstance(cls, dict)
    ]
    return {"rules": {"values": values}, "options": {}}


def _code_parameters(info: _NodeInfo) -> dict[str, Any]:
    language = str(info.data.get("code_language", "python3") or "python3")
    code = str(info.data.get("code", "") or "")
    if language == "python3":
        return {"language": "python", "pythonCode": code}
    return {"jsCode": code}


def _set_parameters(info: _NodeInfo, to_expr: Any) -> dict[str, Any]:
    template = str(info.data.get("template", "") or "")
    variables = info.data.get("variables")
    text = template
    for variable in variables if isinstance(variables, list) else []:
        if not isinstance(variable, dict):
            continue
        var_name = str(variable.get("variable", "") or "")
        selector = variable.get("value_selector")
        if not var_name or not (isinstance(selector, list) and len(selector) >= 2):
            continue
        ref = "{{#" + str(selector[0]) + "." + ".".join(str(s) for s in selector[1:]) + "#}}"
        text = text.replace("{{ " + var_name + " }}", ref)
    value = to_expr(text)
    return {
        "assignments": {
            "assignments": [
                {"id": "assign-1", "name": "output", "value": value, "type": "string"}
            ]
        },
        "options": {},
    }


def _agent_parameters(info: _NodeInfo, to_expr: Any) -> dict[str, Any]:
    prompt = info.data.get("prompt_template")
    system_text = ""
    user_text = ""
    for message in prompt if isinstance(prompt, list) else []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "user") or "user")
        text = str(message.get("text", "") or "")
        if role == "system" and not system_text:
            system_text = text
        elif role != "system" and not user_text:
            user_text = text
    options: dict[str, Any] = {}
    if system_text:
        options["systemMessage"] = to_expr(system_text)
    return {"promptType": "define", "text": to_expr(user_text), "options": options}


def _companion_lmchat(info: _NodeInfo, used_names: set[str]) -> dict[str, Any] | None:
    """llm 的 model 信息非空时，产一个配套 lmChat* 节点（provider → 节点型号）。"""
    model = info.data.get("model")
    if not isinstance(model, dict):
        return None
    provider = str(model.get("provider", "") or "").lower()
    model_name = str(model.get("name", "") or "")
    if not provider and not model_name:
        return None
    lmchat_type, type_version = PROVIDER_TO_LMCHAT.get(provider, DEFAULT_LMCHAT)
    position = _position_of(info) or [info.index * 220, 0]
    return {
        "id": f"{info.dify_id}_model",
        "name": _unique_name(f"{info.name} Model", used_names),
        "type": lmchat_type,
        "typeVersion": type_version,
        "position": [position[0], position[1] + 180],
        "parameters": {"model": model_name, "options": {}},
    }
