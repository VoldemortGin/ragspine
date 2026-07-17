"""Public contract and safety tests for workflow graph previews."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

import pytest

from ragspine.n8n import n8n_to_dify
from ragspine.workflows import (
    PREVIEW_SCHEMA_VERSION,
    WorkflowPreview,
    WorkflowPreviewError,
    build_workflow_preview,
)
from ragspine.workflows.preview import MAX_PREVIEW_EDGES, MAX_PREVIEW_NODES


def _node(
    node_id: str,
    node_type: str,
    *,
    title: object = "Node",
    x: int | float = 0,
    y: int | float = 0,
    width: int | float = 240,
    height: int | float = 90,
    parent_id: str | None = None,
    include_absolute_position: bool = True,
    **data_fields: object,
) -> dict[str, object]:
    data = {"type": node_type, "title": title, **data_fields}
    node: dict[str, object] = {
        "id": node_id,
        "data": data,
        "position": {
            "x": x + 1000 if include_absolute_position else x,
            "y": y + 1000 if include_absolute_position else y,
        },
        "width": width,
        "height": height,
    }
    if include_absolute_position:
        node["positionAbsolute"] = {"x": x, "y": y}
    if parent_id is not None:
        node["parentId"] = parent_id
    return node


def _edge(
    edge_id: str,
    source: str,
    target: str,
    *,
    source_handle: object = "source",
    **fields: object,
) -> dict[str, object]:
    return {
        "id": edge_id,
        "source": source,
        "target": target,
        "sourceHandle": source_handle,
        **fields,
    }


def _document(
    nodes: list[object],
    edges: list[object],
    **workflow_fields: object,
) -> dict[str, object]:
    return {
        "kind": "app",
        "workflow": {
            "graph": {"nodes": nodes, "edges": edges},
            **workflow_fields,
        },
    }


def test_builds_versioned_immutable_preview_for_simple_chain() -> None:
    document = _document(
        [
            _node("start", "start", title="Start", x=30, y=220),
            _node(
                "llm",
                "llm",
                title="   ",
                x=334,
                y=220,
                prompt_template="private prompt",
                model={"provider": "private-provider", "api_key": "private-key"},
            ),
            _node("answer", "answer", title="Answer", x=638, y=220),
        ],
        [_edge("start-llm", "start", "llm"), _edge("llm-answer", "llm", "answer")],
        environment_variables=[{"name": "PRIVATE", "value": "secret-value"}],
        conversation_variables=[{"name": "history", "value": "private-history"}],
    )

    preview = build_workflow_preview(MappingProxyType(document))

    assert isinstance(preview, WorkflowPreview)
    assert preview.preview_schema_version == PREVIEW_SCHEMA_VERSION == 1
    assert preview.nodes[1].title == "Llm"
    assert preview.nodes[0].x == 30
    assert preview.edges[0].label is None
    with pytest.raises(FrozenInstanceError):
        preview.nodes[0].title = "mutated"  # type: ignore[misc]
    assert preview.to_dict() == {
        "preview_schema_version": 1,
        "nodes": [
            {
                "id": "start",
                "title": "Start",
                "type": "start",
                "x": 30,
                "y": 220,
                "width": 240,
                "height": 90,
            },
            {
                "id": "llm",
                "title": "Llm",
                "type": "llm",
                "x": 334,
                "y": 220,
                "width": 240,
                "height": 90,
            },
            {
                "id": "answer",
                "title": "Answer",
                "type": "answer",
                "x": 638,
                "y": 220,
                "width": 240,
                "height": 90,
            },
        ],
        "edges": [
            {"id": "start-llm", "source": "start", "target": "llm"},
            {"id": "llm-answer", "source": "llm", "target": "answer"},
        ],
    }
    serialized = json.dumps(preview.to_dict(), ensure_ascii=False, allow_nan=False)
    for private_value in (
        "private prompt",
        "private-provider",
        "private-key",
        "secret-value",
        "private-history",
    ):
        assert private_value not in serialized


def test_if_else_handles_become_display_labels() -> None:
    preview = build_workflow_preview(
        _document(
            [
                _node("condition", "if-else", title="Route"),
                _node("yes", "answer", title="Matched"),
                _node("no", "answer", title="Fallback"),
            ],
            [
                _edge("yes-edge", "condition", "yes", source_handle="true"),
                _edge("no-edge", "condition", "no", source_handle="false"),
            ],
        )
    )

    assert [edge.label for edge in preview.edges] == ["IF", "ELSE"]
    assert [edge["label"] for edge in preview.to_dict()["edges"]] == ["IF", "ELSE"]  # type: ignore[index]


def test_iteration_parent_relationship_is_retained() -> None:
    preview = build_workflow_preview(
        _document(
            [
                _node("iteration", "iteration", title="For each", width=420, height=260),
                _node(
                    "iteration-start",
                    "iteration-start",
                    title=None,
                    parent_id="iteration",
                ),
                _node("worker", "llm", title="Worker", parent_id="iteration"),
            ],
            [_edge("inside", "iteration-start", "worker", isInIteration=True)],
        )
    )

    assert preview.nodes[1].title == "Iteration start"
    assert preview.nodes[1].parent_id == "iteration"
    assert preview.nodes[2].parent_id == "iteration"
    nodes = preview.to_dict()["nodes"]
    assert isinstance(nodes, list)
    assert nodes[1]["parent_id"] == "iteration"  # type: ignore[index]


def test_relative_child_positions_are_resolved_through_parent_chain() -> None:
    preview = build_workflow_preview(
        _document(
            [
                _node(
                    "outer",
                    "iteration",
                    x=100,
                    y=50,
                    include_absolute_position=False,
                ),
                _node(
                    "inner",
                    "iteration",
                    x=20,
                    y=30,
                    parent_id="outer",
                    include_absolute_position=False,
                ),
                _node(
                    "worker",
                    "llm",
                    x=5,
                    y=7,
                    parent_id="inner",
                    include_absolute_position=False,
                ),
                _node(
                    "absolute-worker",
                    "llm",
                    x=900,
                    y=800,
                    parent_id="inner",
                ),
            ],
            [],
        )
    )

    positions = {node.id: (node.x, node.y) for node in preview.nodes}
    assert positions == {
        "outer": (100, 50),
        "inner": (120, 80),
        "worker": (125, 87),
        "absolute-worker": (900, 800),
    }


def test_missing_geometry_and_edges_use_deterministic_defaults() -> None:
    document = {
        "workflow": {
            "graph": {
                "nodes": [
                    {"id": f"node-{index}", "data": {"type": "llm", "title": ""}}
                    for index in range(5)
                ]
            }
        }
    }

    first = build_workflow_preview(document)
    second = build_workflow_preview(document)

    assert first == second
    assert first.edges == ()
    assert [(node.x, node.y) for node in first.nodes] == [
        (30, 80),
        (334, 80),
        (638, 80),
        (942, 80),
        (30, 250),
    ]
    assert {(node.width, node.height) for node in first.nodes} == {(240, 90)}
    assert all(node.title == "Llm" for node in first.nodes)


def test_null_absolute_position_falls_back_to_valid_relative_position() -> None:
    node = {
        "id": "node",
        "data": {"type": "llm", "title": "Node"},
        "positionAbsolute": None,
        "position": {"x": 12, "y": 34},
    }

    preview = build_workflow_preview(_document([node], []))

    assert (preview.nodes[0].x, preview.nodes[0].y) == (12, 34)


def test_missing_child_position_uses_relative_parent_layout() -> None:
    parent = _node("parent", "iteration", x=100, y=100, width=420, height=260)
    child = {
        "id": "child",
        "parentId": "parent",
        "data": {"type": "llm", "title": "Child"},
    }

    preview = build_workflow_preview(_document([parent, child], []))
    nodes = {node.id: node for node in preview.nodes}

    assert (nodes["child"].x, nodes["child"].y) == (124, 140)
    assert nodes["child"].parent_id == "parent"


def test_incomplete_overlap_near_coordinate_limit_remains_valid() -> None:
    nodes = [
        {
            "id": node_id,
            "data": {"type": "llm", "title": node_id},
            "position": {"x": 1_000_000, "y": 0},
        }
        for node_id in ("one", "two")
    ]

    preview = build_workflow_preview(_document(nodes, []))

    assert [(node.x, node.y) for node in preview.nodes] == [
        (1_000_000, 0),
        (1_000_000, 0),
    ]


def test_empty_graph_has_an_empty_versioned_preview() -> None:
    preview = build_workflow_preview({"workflow": {"graph": {}}})

    assert preview.nodes == ()
    assert preview.edges == ()
    assert preview.to_dict() == {
        "preview_schema_version": PREVIEW_SCHEMA_VERSION,
        "nodes": [],
        "edges": [],
    }


def test_missing_edge_ids_are_stable_and_avoid_explicit_id_collisions() -> None:
    nodes = [_node("one", "start"), _node("two", "end")]
    document = _document(
        nodes,
        [
            {"source": "one", "target": "two"},
            _edge("preview-edge-1", "one", "two"),
            _edge("preview-edge-1-2", "one", "two"),
            {"source": "one", "target": "two"},
            _edge("preview-edge-4", "one", "two"),
        ],
    )

    first = build_workflow_preview(document)
    second = build_workflow_preview(document)

    assert first == second
    assert [edge.id for edge in first.edges] == [
        "preview-edge-1-3",
        "preview-edge-1",
        "preview-edge-1-2",
        "preview-edge-4-2",
        "preview-edge-4",
    ]
    assert len({edge.id for edge in first.edges}) == len(first.edges)


def test_real_n8n_fixture_converts_to_a_preview() -> None:
    fixture_path = Path(__file__).parents[1] / "n8n" / "fixtures" / "branch.json"
    fixture = fixture_path.read_text(encoding="utf-8")
    document, warnings = n8n_to_dify(fixture)
    graph = document["workflow"]["graph"]

    assert warnings == []
    assert all("width" not in node and "height" not in node for node in graph["nodes"])
    assert all("id" not in edge for edge in graph["edges"])

    preview = build_workflow_preview(document)
    nodes = {node.id: node for node in preview.nodes}

    assert len(preview.nodes) == 5
    assert len(preview.edges) == 5
    assert (nodes["webhook"].x, nodes["webhook"].y) == (0, 0)
    assert (nodes["check_score"].x, nodes["check_score"].y) == (220, 0)
    assert nodes["end_1"].x > nodes["approve"].x
    assert (nodes["end_1"].x, nodes["end_1"].y) != (
        nodes["webhook"].x,
        nodes["webhook"].y,
    )
    assert all(node.width == 240 and node.height == 90 for node in preview.nodes)
    assert {(edge.source, edge.target, edge.label) for edge in preview.edges} >= {
        ("check_score", "approve", "IF"),
        ("check_score", "reject", "ELSE"),
    }
    assert len({edge.id for edge in preview.edges}) == len(preview.edges)
    assert preview == build_workflow_preview(document)


def test_untrusted_titles_and_branch_handles_remain_plain_text() -> None:
    title = "<img src=x onerror=alert(1)>"
    handle = "</text><script>alert(2)</script>"
    preview = build_workflow_preview(
        _document(
            [_node("one", "custom", title=title), _node("two", "end", title="End")],
            [_edge("unsafe", "one", "two", source_handle=handle)],
        )
    )

    assert preview.nodes[0].title == title
    assert preview.edges[0].label == handle
    assert set(preview.to_dict()) == {"preview_schema_version", "nodes", "edges"}


def test_output_is_deterministic_json_compatible_and_fresh() -> None:
    document: Mapping[str, object] = _document(
        [_node("one", "start", title="开始"), _node("two", "end", title="结束")],
        [_edge("edge", "one", "two")],
    )

    first = build_workflow_preview(document)
    second = build_workflow_preview(document)
    first_dict = first.to_dict()

    assert first == second
    assert json.loads(json.dumps(first_dict, allow_nan=False)) == first_dict
    nodes = first_dict["nodes"]
    assert isinstance(nodes, list)
    nodes.clear()
    assert len(first.nodes) == 2
    assert len(first.to_dict()["nodes"]) == 2  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            _document(
                [_node("same", "start"), _node("same", "end")],
                [],
            ),
            "节点 ID 重复",
        ),
        (
            _document(
                [_node("one", "start"), _node("two", "end")],
                [_edge("same", "one", "two"), _edge("same", "one", "two")],
            ),
            "连线 ID 重复",
        ),
        (
            _document(
                [_node("one", "start")],
                [_edge("dangling", "one", "missing")],
            ),
            "连线端点不存在",
        ),
        (
            _document(
                [_node("one", "start", parent_id="missing")],
                [],
            ),
            "parentId 不存在",
        ),
        (
            _document(
                [
                    _node("one", "iteration", parent_id="two"),
                    _node("two", "iteration", parent_id="one"),
                ],
                [],
            ),
            "parentId 成环",
        ),
        (
            _document([_node("one", "start", x=float("nan"))], []),
            "有限数字",
        ),
        (
            _document([_node("one", "start", width=0)], []),
            "正数",
        ),
        (
            _document([_node("one", "start", height=True)], []),
            "number",
        ),
        (
            _document(
                [
                    _node(
                        "parent",
                        "iteration",
                        x=1_000_000,
                        include_absolute_position=False,
                    ),
                    _node(
                        "child",
                        "llm",
                        x=1,
                        parent_id="parent",
                        include_absolute_position=False,
                    ),
                ],
                [],
            ),
            "resolved x.*超出范围",
        ),
    ],
)
def test_invalid_graph_boundaries_are_rejected(
    document: Mapping[str, object], message: str
) -> None:
    with pytest.raises(WorkflowPreviewError, match=message):
        build_workflow_preview(document)


@pytest.mark.parametrize(
    ("node_patch", "message"),
    [
        ({"position": None}, "position.*object"),
        ({"positionAbsolute": "invalid"}, "positionAbsolute.*object"),
        ({"width": None}, "width.*number"),
        ({"height": "90"}, "height.*number"),
    ],
)
def test_explicit_invalid_geometry_is_not_replaced_by_defaults(
    node_patch: dict[str, object], message: str
) -> None:
    node: dict[str, object] = {
        "id": "bad",
        "data": {"type": "llm", "title": "Bad"},
        "position": {"x": 0, "y": 0},
    }
    node.update(node_patch)

    with pytest.raises(WorkflowPreviewError, match=message):
        build_workflow_preview(_document([node], []))


def test_explicit_invalid_edge_id_is_not_generated() -> None:
    edge = {"id": None, "source": "one", "target": "two"}

    with pytest.raises(WorkflowPreviewError, match=r"edges\[0\].id.*string"):
        build_workflow_preview(_document([_node("one", "start"), _node("two", "end")], [edge]))


def test_graph_size_limits_are_enforced() -> None:
    too_many_nodes = [_node(f"node-{index}", "start") for index in range(MAX_PREVIEW_NODES + 1)]
    with pytest.raises(WorkflowPreviewError, match="节点数超过上限"):
        build_workflow_preview(_document(too_many_nodes, []))

    nodes = [_node("one", "start"), _node("two", "end")]
    too_many_edges = [
        _edge(f"edge-{index}", "one", "two") for index in range(MAX_PREVIEW_EDGES + 1)
    ]
    with pytest.raises(WorkflowPreviewError, match="连线数超过上限"):
        build_workflow_preview(_document(nodes, too_many_edges))


def test_error_does_not_echo_untrusted_values() -> None:
    private_value = "super-secret-invalid-node-id"
    document = _document(
        [_node(private_value, "start"), _node(private_value, "end")],
        [],
    )

    with pytest.raises(WorkflowPreviewError) as error:
        build_workflow_preview(document)

    assert private_value not in str(error.value)
