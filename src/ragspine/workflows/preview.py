"""Versioned, privacy-minimal workflow graph previews.

The preview is a display projection, not another executable workflow format.
Only graph identity, labels, geometry, containment, and branch labels cross this
boundary; prompts, provider configuration, variables, and credentials do not.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import cast

from ragspine.workflows.errors import WorkflowPreviewError

PREVIEW_SCHEMA_VERSION = 1
MAX_PREVIEW_NODES = 256
MAX_PREVIEW_EDGES = 512
MAX_PREVIEW_STRING_CHARS = 512
MAX_PREVIEW_COORDINATE = 1_000_000
MAX_PREVIEW_DIMENSION = 10_000
DEFAULT_PREVIEW_NODE_WIDTH = 240
DEFAULT_PREVIEW_NODE_HEIGHT = 90
_DEFAULT_LAYOUT_COLUMNS = 4
_DEFAULT_LAYOUT_X = 30
_DEFAULT_LAYOUT_Y = 80
_DEFAULT_LAYOUT_X_STEP = 304
_DEFAULT_LAYOUT_Y_STEP = 170
_DEFAULT_CHILD_X = 24
_DEFAULT_CHILD_Y = 40
_DEFAULT_CHILD_Y_STEP = 110


@dataclass(frozen=True)
class WorkflowPreviewNode:
    """One immutable node in a workflow preview."""

    id: str
    title: str
    type: str
    x: int | float
    y: int | float
    width: int | float
    height: int | float
    parent_id: str | None = None

    def _to_dict(self) -> dict[str, object]:
        row: dict[str, object] = {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }
        if self.parent_id is not None:
            row["parent_id"] = self.parent_id
        return row


@dataclass(frozen=True)
class WorkflowPreviewEdge:
    """One immutable directed edge in a workflow preview."""

    id: str
    source: str
    target: str
    label: str | None = None

    def _to_dict(self) -> dict[str, object]:
        row: dict[str, object] = {
            "id": self.id,
            "source": self.source,
            "target": self.target,
        }
        if self.label is not None:
            row["label"] = self.label
        return row


@dataclass(frozen=True)
class WorkflowPreview:
    """Immutable preview value with a stable JSON-compatible projection."""

    nodes: tuple[WorkflowPreviewNode, ...]
    edges: tuple[WorkflowPreviewEdge, ...]
    preview_schema_version: int = field(default=PREVIEW_SCHEMA_VERSION, init=False)

    def to_dict(self) -> dict[str, object]:
        """Return a fresh JSON-compatible representation of this preview."""

        return {
            "preview_schema_version": self.preview_schema_version,
            "nodes": [node._to_dict() for node in self.nodes],
            "edges": [edge._to_dict() for edge in self.edges],
        }


def build_workflow_preview(workflow: Mapping[str, object]) -> WorkflowPreview:
    """Build a deterministic, privacy-minimal preview from a parsed Dify document.

    ``workflow`` is the JSON-compatible root mapping returned by
    :func:`ragspine.workflows.parse_workflow`.  Input order is retained so
    repeated calls over the same document produce byte-stable JSON when dumped
    with the same serializer settings.
    """

    root = _as_mapping(workflow, "root")
    workflow_body = _as_mapping(root.get("workflow"), "workflow")
    graph = _as_mapping(workflow_body.get("graph"), "workflow.graph")
    raw_nodes = _as_list(graph.get("nodes", []), "workflow.graph.nodes")
    raw_edges = _as_list(graph.get("edges", []), "workflow.graph.edges")

    if len(raw_nodes) > MAX_PREVIEW_NODES:
        raise WorkflowPreviewError(f"workflow preview 节点数超过上限 {MAX_PREVIEW_NODES}")
    if len(raw_edges) > MAX_PREVIEW_EDGES:
        raise WorkflowPreviewError(f"workflow preview 连线数超过上限 {MAX_PREVIEW_EDGES}")

    nodes: list[WorkflowPreviewNode] = []
    node_ids: set[str] = set()
    absolute_node_ids: set[str] = set()
    incomplete_geometry_node_ids: set[str] = set()
    child_layout_slots: dict[str, int] = {}
    for index, raw_node in enumerate(raw_nodes):
        node, has_absolute_position, has_incomplete_geometry = _parse_node(
            raw_node,
            index=index,
            child_layout_slots=child_layout_slots,
        )
        if node.id in node_ids:
            raise WorkflowPreviewError(f"workflow.graph.nodes[{index}] 节点 ID 重复")
        node_ids.add(node.id)
        if has_absolute_position:
            absolute_node_ids.add(node.id)
        if has_incomplete_geometry:
            incomplete_geometry_node_ids.add(node.id)
        nodes.append(node)

    nodes = _resolve_node_positions(nodes, node_ids, absolute_node_ids)
    nodes = _deduplicate_incomplete_top_level_positions(nodes, incomplete_geometry_node_ids)

    edge_mappings: list[Mapping[str, object]] = []
    explicit_edge_ids: dict[int, str] = {}
    reserved_edge_ids: set[str] = set()
    for index, raw_edge in enumerate(raw_edges):
        path = f"workflow.graph.edges[{index}]"
        edge = _as_mapping(raw_edge, path)
        edge_mappings.append(edge)
        if "id" not in edge:
            continue
        explicit_id = _as_string(edge.get("id"), f"{path}.id")
        if explicit_id in reserved_edge_ids:
            raise WorkflowPreviewError(f"{path} 连线 ID 重复")
        explicit_edge_ids[index] = explicit_id
        reserved_edge_ids.add(explicit_id)

    edges: list[WorkflowPreviewEdge] = []
    used_edge_ids = set(reserved_edge_ids)
    for index, edge_mapping in enumerate(edge_mappings):
        selected_id = explicit_edge_ids.get(index)
        if selected_id is None:
            selected_id = _generated_edge_id(index, used_edge_ids)
        parsed_edge = _parse_edge(edge_mapping, index=index, edge_id=selected_id)
        if parsed_edge.source not in node_ids or parsed_edge.target not in node_ids:
            raise WorkflowPreviewError(f"workflow.graph.edges[{index}] 连线端点不存在")
        edges.append(parsed_edge)

    return WorkflowPreview(nodes=tuple(nodes), edges=tuple(edges))


def _parse_node(
    value: object,
    *,
    index: int,
    child_layout_slots: dict[str, int],
) -> tuple[WorkflowPreviewNode, bool, bool]:
    path = f"workflow.graph.nodes[{index}]"
    node = _as_mapping(value, path)
    node_id = _as_string(node.get("id"), f"{path}.id")
    data = _as_mapping(node.get("data"), f"{path}.data")
    node_type = _as_string(data.get("type"), f"{path}.data.type")

    raw_title = data.get("title")
    if raw_title is None or isinstance(raw_title, str) and not raw_title.strip():
        title = node_type.replace("-", " ").replace("_", " ").capitalize()
    else:
        title = _as_string(raw_title, f"{path}.data.title")

    parent_value = node.get("parentId")
    parent_id = None
    if parent_value is not None:
        parent_id = _as_string(parent_value, f"{path}.parentId")

    missing_position = False
    raw_absolute_position = node.get("positionAbsolute")
    if raw_absolute_position is not None:
        has_absolute_position = True
        position_path = f"{path}.positionAbsolute"
        position = _as_mapping(raw_absolute_position, position_path)
        x = _as_number(position.get("x"), f"{position_path}.x")
        y = _as_number(position.get("y"), f"{position_path}.y")
    elif "position" in node:
        raw_position = node.get("position")
        has_absolute_position = False
        position_path = f"{path}.position"
        position = _as_mapping(raw_position, position_path)
        x = _as_number(position.get("x"), f"{position_path}.x")
        y = _as_number(position.get("y"), f"{position_path}.y")
    else:
        if parent_id is None:
            x, y = _default_position(index)
            has_absolute_position = True
        else:
            slot = child_layout_slots.get(parent_id, 0)
            child_layout_slots[parent_id] = slot + 1
            x, y = _default_child_position(slot)
            has_absolute_position = False
        missing_position = True

    missing_width = "width" not in node
    missing_height = "height" not in node
    width = (
        DEFAULT_PREVIEW_NODE_WIDTH
        if missing_width
        else _as_number(
            node.get("width"),
            f"{path}.width",
            positive=True,
            maximum=MAX_PREVIEW_DIMENSION,
        )
    )
    height = (
        DEFAULT_PREVIEW_NODE_HEIGHT
        if missing_height
        else _as_number(
            node.get("height"),
            f"{path}.height",
            positive=True,
            maximum=MAX_PREVIEW_DIMENSION,
        )
    )
    return (
        WorkflowPreviewNode(
            id=node_id,
            title=title,
            type=node_type,
            x=x,
            y=y,
            width=width,
            height=height,
            parent_id=parent_id,
        ),
        has_absolute_position,
        missing_position or missing_width or missing_height,
    )


def _parse_edge(edge: Mapping[str, object], *, index: int, edge_id: str) -> WorkflowPreviewEdge:
    path = f"workflow.graph.edges[{index}]"
    source_handle = edge.get("sourceHandle")
    label = None
    if source_handle is not None:
        handle = _as_string(source_handle, f"{path}.sourceHandle")
        if handle != "source":
            label = {"true": "IF", "false": "ELSE"}.get(handle, handle)
    return WorkflowPreviewEdge(
        id=edge_id,
        source=_as_string(edge.get("source"), f"{path}.source"),
        target=_as_string(edge.get("target"), f"{path}.target"),
        label=label,
    )


def _default_position(index: int) -> tuple[int, int]:
    column = index % _DEFAULT_LAYOUT_COLUMNS
    row = index // _DEFAULT_LAYOUT_COLUMNS
    return (
        _DEFAULT_LAYOUT_X + column * _DEFAULT_LAYOUT_X_STEP,
        _DEFAULT_LAYOUT_Y + row * _DEFAULT_LAYOUT_Y_STEP,
    )


def _default_child_position(slot: int) -> tuple[int, int]:
    return _DEFAULT_CHILD_X, _DEFAULT_CHILD_Y + slot * _DEFAULT_CHILD_Y_STEP


def _generated_edge_id(index: int, used: set[str]) -> str:
    base = f"preview-edge-{index + 1}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _resolve_node_positions(
    nodes: list[WorkflowPreviewNode],
    node_ids: set[str],
    absolute_node_ids: set[str],
) -> list[WorkflowPreviewNode]:
    """Resolve relative child positions without trusting source node order."""

    nodes_by_id = {node.id: node for node in nodes}
    resolved: dict[str, tuple[int | float, int | float]] = {}
    visiting: set[str] = set()

    def resolve(node_id: str) -> tuple[int | float, int | float]:
        cached = resolved.get(node_id)
        if cached is not None:
            return cached
        if node_id in visiting:
            raise WorkflowPreviewError("workflow preview parentId 成环")

        visiting.add(node_id)
        node = nodes_by_id[node_id]
        parent_position: tuple[int | float, int | float] | None = None
        if node.parent_id is not None:
            if node.parent_id not in node_ids:
                raise WorkflowPreviewError("workflow preview parentId 不存在")
            parent_position = resolve(node.parent_id)

        x, y = node.x, node.y
        if node_id not in absolute_node_ids and parent_position is not None:
            x = _as_number(parent_position[0] + x, "workflow preview resolved x")
            y = _as_number(parent_position[1] + y, "workflow preview resolved y")

        visiting.remove(node_id)
        resolved[node_id] = (x, y)
        return x, y

    resolved_nodes: list[WorkflowPreviewNode] = []
    for node in nodes:
        x, y = resolve(node.id)
        resolved_nodes.append(replace(node, x=x, y=y))
    return resolved_nodes


def _deduplicate_incomplete_top_level_positions(
    nodes: list[WorkflowPreviewNode], incomplete_node_ids: set[str]
) -> list[WorkflowPreviewNode]:
    """Move only incomplete top-level nodes when exact source positions overlap."""

    occupied = {
        (node.x, node.y)
        for node in nodes
        if node.parent_id is None and node.id not in incomplete_node_ids
    }
    resolved: list[WorkflowPreviewNode] = []
    for node in nodes:
        if node.parent_id is not None or node.id not in incomplete_node_ids:
            resolved.append(node)
            continue

        position = (node.x, node.y)
        if position in occupied:
            rightmost = max(x for x, _y in occupied)
            candidate_x = rightmost + _DEFAULT_LAYOUT_X_STEP
            if abs(float(candidate_x)) <= MAX_PREVIEW_COORDINATE:
                node = replace(node, x=candidate_x)
                position = (node.x, node.y)
        occupied.add(position)
        resolved.append(node)
    return resolved


def _as_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WorkflowPreviewError(f"{path} 必须是 object")
    if any(not isinstance(key, str) for key in value):
        raise WorkflowPreviewError(f"{path} 的 key 必须是 string")
    return cast("Mapping[str, object]", value)


def _as_list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise WorkflowPreviewError(f"{path} 必须是 list")
    return value


def _as_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowPreviewError(f"{path} 必须是非空 string")
    if len(value) > MAX_PREVIEW_STRING_CHARS:
        raise WorkflowPreviewError(f"{path} 字符数超过上限 {MAX_PREVIEW_STRING_CHARS}")
    return value


def _as_number(
    value: object,
    path: str,
    *,
    positive: bool = False,
    maximum: int = MAX_PREVIEW_COORDINATE,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise WorkflowPreviewError(f"{path} 必须是 number")
    try:
        normalized = float(value)
    except OverflowError as exc:
        raise WorkflowPreviewError(f"{path} 必须是有限数字") from exc
    if not math.isfinite(normalized):
        raise WorkflowPreviewError(f"{path} 必须是有限数字")
    if positive and normalized <= 0:
        raise WorkflowPreviewError(f"{path} 必须是正数")
    if abs(normalized) > maximum:
        raise WorkflowPreviewError(f"{path} 超出范围")
    return value


__all__ = [
    "PREVIEW_SCHEMA_VERSION",
    "WorkflowPreview",
    "WorkflowPreviewEdge",
    "WorkflowPreviewError",
    "WorkflowPreviewNode",
    "build_workflow_preview",
]
