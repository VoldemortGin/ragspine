"""Dependency-free structural gate for current Dify 0.6 workflow exports."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, cast

import pytest

from ragspine import MockProvider
from ragspine.dify.api import compile_dify_yaml
from ragspine.workflows.catalog import load_builtin_catalog
from ragspine.workflows.model import WorkflowTemplate
from ragspine.workflows.scaffold import scaffold_workflow

OPENAI_DEPENDENCY = (
    "langgenius/openai:0.3.8@592c8252795b5f75807de2d609a03196ed02596b409f7642b4a07548c7ff57ef"
)
SAFE_NODE_TYPES = frozenset(
    {
        "answer",
        "end",
        "if-else",
        "iteration",
        "iteration-start",
        "knowledge-retrieval",
        "llm",
        "parameter-extractor",
        "start",
        "template-transform",
    }
)
START_VARIABLE_TYPES = frozenset(
    {
        "checkbox",
        "external_data_tool",
        "file",
        "file-list",
        "json_object",
        "number",
        "paragraph",
        "select",
        "text-input",
    }
)
COMPARISON_OPERATORS = frozenset(
    {
        "contains",
        "not contains",
        "start with",
        "end with",
        "is",
        "is not",
        "empty",
        "not empty",
        "in",
        "not in",
        "all of",
        "=",
        "≠",
        ">",
        "<",
        "≥",
        "≤",
        "null",
        "not null",
        "exists",
        "not exists",
    }
)


def _mapping(value: object, label: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{label} must be a mapping"
    assert all(isinstance(key, str) for key in value), f"{label} keys must be strings"
    return cast(dict[str, object], value)


def _list(value: object, label: str) -> list[object]:
    assert isinstance(value, list), f"{label} must be a list"
    return value


def _documents() -> Iterable[tuple[str, dict[str, object], str]]:
    for template in load_builtin_catalog().runnable():
        yield template.id, template.workflow, template.yaml
    generated = scaffold_workflow("Build a safe custom analysis workflow", reuse=False)
    yield "generated-fallback", generated.workflow, generated.yaml


def _graph(document: Mapping[str, object], label: str) -> dict[str, object]:
    workflow = _mapping(document.get("workflow"), f"{label}.workflow")
    return _mapping(workflow.get("graph"), f"{label}.workflow.graph")


def _nodes(document: Mapping[str, object], label: str) -> list[dict[str, object]]:
    return [
        _mapping(node, f"{label}.node[{index}]")
        for index, node in enumerate(_list(_graph(document, label).get("nodes"), "nodes"))
    ]


def _node_data(node: Mapping[str, object], label: str) -> dict[str, object]:
    return _mapping(node.get("data"), f"{label}.data")


@pytest.mark.parametrize("template", load_builtin_catalog().runnable(), ids=lambda item: item.id)
def test_catalog_templates_compile_without_local_skeleton_warnings(
    template: WorkflowTemplate,
) -> None:
    result = compile_dify_yaml(template.yaml)

    assert result.code.warnings == ()


def test_generated_fallback_compiles_without_local_skeleton_warnings() -> None:
    result = scaffold_workflow("Build a safe custom analysis workflow", reuse=False)

    assert compile_dify_yaml(result.yaml).code.warnings == ()


def test_all_documents_have_current_dify_envelope_and_openai_dependency() -> None:
    for label, document, _ in _documents():
        assert document.get("kind") == "app", label
        assert document.get("version") == "0.6.0", label
        app = _mapping(document.get("app"), f"{label}.app")
        assert {
            "description",
            "icon",
            "icon_background",
            "mode",
            "name",
            "use_icon_as_answer_icon",
        }.issubset(app), label
        assert app["mode"] in {"workflow", "advanced-chat"}, label

        dependencies = _list(document.get("dependencies"), f"{label}.dependencies")
        assert len(dependencies) == 1, label
        dependency = _mapping(dependencies[0], f"{label}.dependency")
        assert dependency.get("type") == "marketplace", label
        assert dependency.get("current_identifier") is None, label
        value = _mapping(dependency.get("value"), f"{label}.dependency.value")
        assert value.get("marketplace_plugin_unique_identifier") == OPENAI_DEPENDENCY, label

        workflow = _mapping(document.get("workflow"), f"{label}.workflow")
        assert workflow.get("conversation_variables") == [], label
        assert workflow.get("environment_variables") == [], label
        assert isinstance(workflow.get("features"), dict), label
        viewport = _mapping(_graph(document, label).get("viewport"), f"{label}.viewport")
        assert set(viewport) >= {"x", "y", "zoom"}, label


def test_all_nodes_have_reactflow_wrapper_and_valid_node_data() -> None:
    for label, document, _ in _documents():
        nodes = _nodes(document, label)
        ids: set[str] = set()
        for node in nodes:
            node_id = node.get("id")
            assert isinstance(node_id, str) and node_id, label
            assert node_id not in ids, f"{label}: duplicate node id {node_id}"
            ids.add(node_id)
            data = _node_data(node, f"{label}.{node_id}")
            node_type = data.get("type")
            assert node_type in SAFE_NODE_TYPES, f"{label}.{node_id}: {node_type}"
            assert isinstance(data.get("title"), str), f"{label}.{node_id}"
            assert isinstance(data.get("desc"), str), f"{label}.{node_id}"
            assert data.get("selected") is False, f"{label}.{node_id}"

            for numeric_field in ("height", "width"):
                assert isinstance(node.get(numeric_field), int | float), (
                    f"{label}.{node_id}.{numeric_field}"
                )
            for position_field in ("position", "positionAbsolute"):
                position = _mapping(node.get(position_field), f"{label}.{node_id}.{position_field}")
                assert isinstance(position.get("x"), int | float)
                assert isinstance(position.get("y"), int | float)
            assert node.get("sourcePosition") == "right", f"{label}.{node_id}"
            assert node.get("targetPosition") == "left", f"{label}.{node_id}"
            if node_type == "iteration-start":
                assert node.get("type") == "custom-iteration-start", f"{label}.{node_id}"
                assert node.get("draggable") is False, f"{label}.{node_id}"
                assert node.get("selectable") is False, f"{label}.{node_id}"
            else:
                assert node.get("type") == "custom", f"{label}.{node_id}"
                assert node.get("selected") is False, f"{label}.{node_id}"


def test_all_edges_have_reactflow_wrapper_and_valid_endpoints() -> None:
    for label, document, _ in _documents():
        graph = _graph(document, label)
        nodes = _nodes(document, label)
        node_types = {
            cast(str, node["id"]): cast(str, _node_data(node, label)["type"]) for node in nodes
        }
        edge_ids: set[str] = set()
        for index, raw_edge in enumerate(_list(graph.get("edges"), f"{label}.edges")):
            edge = _mapping(raw_edge, f"{label}.edge[{index}]")
            edge_id = edge.get("id")
            assert isinstance(edge_id, str) and edge_id, label
            assert edge_id not in edge_ids, f"{label}: duplicate edge id {edge_id}"
            edge_ids.add(edge_id)
            source = edge.get("source")
            target = edge.get("target")
            assert source in node_types, f"{label}.{edge_id}: missing source {source}"
            assert target in node_types, f"{label}.{edge_id}: missing target {target}"
            assert isinstance(edge.get("sourceHandle"), str), f"{label}.{edge_id}"
            assert edge.get("targetHandle") == "target", f"{label}.{edge_id}"
            assert edge.get("type") == "custom", f"{label}.{edge_id}"
            assert isinstance(edge.get("zIndex"), int), f"{label}.{edge_id}"
            data = _mapping(edge.get("data"), f"{label}.{edge_id}.data")
            assert data.get("sourceType") == node_types[source], f"{label}.{edge_id}"
            assert data.get("targetType") == node_types[target], f"{label}.{edge_id}"
            assert isinstance(data.get("isInIteration"), bool), f"{label}.{edge_id}"
            assert isinstance(data.get("isInLoop"), bool), f"{label}.{edge_id}"


def test_node_specific_fields_match_current_graphon_and_dify_contracts() -> None:
    for label, document, _ in _documents():
        for node in _nodes(document, label):
            node_id = cast(str, node["id"])
            data = _node_data(node, f"{label}.{node_id}")
            node_type = data["type"]
            if node_type == "start":
                for raw_variable in _list(data.get("variables"), f"{label}.{node_id}.variables"):
                    variable = _mapping(raw_variable, f"{label}.{node_id}.variable")
                    assert variable.get("type") in START_VARIABLE_TYPES
                    assert isinstance(variable.get("variable"), str)
                    assert isinstance(variable.get("label"), str)
                    assert isinstance(variable.get("required"), bool)
            elif node_type == "llm":
                model = _mapping(data.get("model"), f"{label}.{node_id}.model")
                assert model.get("mode") == "chat"
                assert isinstance(model.get("provider"), str)
                assert isinstance(model.get("name"), str)
                assert isinstance(model.get("completion_params"), dict)
                context = _mapping(data.get("context"), f"{label}.{node_id}.context")
                assert isinstance(context.get("enabled"), bool)
                assert isinstance(context.get("variable_selector"), list)
            elif node_type == "parameter-extractor":
                model = _mapping(data.get("model"), f"{label}.{node_id}.model")
                assert model.get("mode") == "chat"
                assert data.get("reasoning_mode") in {"function_call", "prompt"}
                assert len(_list(data.get("query"), f"{label}.{node_id}.query")) >= 2
                assert _list(data.get("parameters"), f"{label}.{node_id}.parameters")
            elif node_type == "knowledge-retrieval":
                assert data.get("retrieval_mode") in {"single", "multiple"}
                assert _list(data.get("dataset_ids"), f"{label}.{node_id}.dataset_ids")
                assert (
                    len(
                        _list(
                            data.get("query_variable_selector"),
                            f"{label}.{node_id}.query_variable_selector",
                        )
                    )
                    >= 2
                )
                if data.get("retrieval_mode") == "multiple":
                    config = _mapping(
                        data.get("multiple_retrieval_config"),
                        f"{label}.{node_id}.multiple_retrieval_config",
                    )
                    assert isinstance(config.get("top_k"), int)
            elif node_type == "if-else":
                for raw_case in _list(data.get("cases"), f"{label}.{node_id}.cases"):
                    case = _mapping(raw_case, f"{label}.{node_id}.case")
                    for raw_condition in _list(case.get("conditions"), "conditions"):
                        condition = _mapping(raw_condition, "condition")
                        assert condition.get("comparison_operator") in COMPARISON_OPERATORS
                        assert isinstance(condition.get("id"), str)
                        assert isinstance(condition.get("varType"), str)
            elif node_type == "template-transform":
                for raw_variable in _list(data.get("variables"), "template variables"):
                    variable = _mapping(raw_variable, "template variable")
                    assert isinstance(variable.get("value_type"), str)
                    assert len(_list(variable.get("value_selector"), "value_selector")) >= 2
            elif node_type == "end":
                for raw_output in _list(data.get("outputs"), f"{label}.{node_id}.outputs"):
                    output = _mapping(raw_output, f"{label}.{node_id}.output")
                    assert isinstance(output.get("value_type"), str)
                    assert len(_list(output.get("value_selector"), "value_selector")) >= 2
            elif node_type == "answer":
                assert isinstance(data.get("answer"), str)


def test_batch_template_has_complete_json_object_iteration_subgraph() -> None:
    document = load_builtin_catalog().get("batch-content-processing").workflow
    nodes = _nodes(document, "batch")
    by_id = {cast(str, node["id"]): node for node in nodes}
    start = next(node for node in nodes if _node_data(node, "batch")["type"] == "start")
    variables = [
        _mapping(item, "batch.start.variable")
        for item in _list(_node_data(start, "batch.start").get("variables"), "variables")
    ]
    batch_variable = next(item for item in variables if item.get("variable") == "batch")
    assert batch_variable.get("type") == "json_object"
    raw_schema = batch_variable.get("json_schema")
    schema = (
        json.loads(raw_schema) if isinstance(raw_schema, str) else _mapping(raw_schema, "schema")
    )
    assert schema.get("type") == "object"
    properties = _mapping(schema.get("properties"), "schema.properties")
    items_property = _mapping(properties.get("items"), "schema.properties.items")
    assert items_property.get("type") == "array"
    assert _mapping(items_property.get("items"), "items.items").get("type") == "string"

    iteration = next(node for node in nodes if _node_data(node, "batch")["type"] == "iteration")
    iteration_id = cast(str, iteration["id"])
    iteration_data = _node_data(iteration, "batch.iteration")
    assert iteration_data.get("iterator_selector") == [cast(str, start["id"]), "batch", "items"]
    iteration_start_id = cast(str, iteration_data.get("start_node_id"))
    iteration_start = by_id[iteration_start_id]
    assert _node_data(iteration_start, "batch.iteration-start").get("type") == "iteration-start"
    assert iteration_start.get("parentId") == iteration_id

    body_nodes = [
        node
        for node in nodes
        if node.get("parentId") == iteration_id and node["id"] != iteration_start_id
    ]
    assert body_nodes
    for node in body_nodes:
        assert _node_data(node, "batch.body").get("iteration_id") == iteration_id

    output_selector = _list(iteration_data.get("output_selector"), "output_selector")
    assert output_selector[0] in {node["id"] for node in body_nodes}
    graph = _graph(document, "batch")
    inner_edges = [
        _mapping(edge, "batch.inner-edge")
        for edge in _list(graph.get("edges"), "batch.edges")
        if _mapping(edge, "batch.edge").get("source") == iteration_start_id
    ]
    assert inner_edges
    inner_edge = inner_edges[0]
    assert inner_edge.get("target") in {node["id"] for node in body_nodes}
    edge_data = _mapping(inner_edge.get("data"), "batch.inner-edge.data")
    assert edge_data.get("isInIteration") is True
    assert edge_data.get("iteration_id") == iteration_id


def test_batch_compiled_runtime_traverses_nested_json_selector() -> None:
    template = load_builtin_catalog().get("batch-content-processing")
    generated = compile_dify_yaml(template.yaml).code.source
    namespace: dict[str, object] = {}
    exec(compile(generated, "<batch-workflow>", "exec"), namespace)  # noqa: S102
    inputs_type = namespace["Inputs"]
    run_workflow = namespace["run_workflow"]

    inputs = inputs_type(  # type: ignore[operator]
        batch={"items": ["alpha", "beta"]},
        instruction="Return each item unchanged",
    )
    result = run_workflow(inputs, provider=MockProvider())  # type: ignore[operator]

    assert isinstance(result, dict)
    assert len(result["results"]) == 2


def test_parameter_extractor_emits_valid_array_item_schemas_and_runs() -> None:
    template = load_builtin_catalog().get("structured-information-extraction")
    generated = compile_dify_yaml(template.yaml).code.source
    namespace: dict[str, object] = {}
    exec(compile(generated, "<structured-extraction>", "exec"), namespace)  # noqa: S102

    class SchemaValidatingProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.fallback = MockProvider()

        def chat(
            self,
            messages: list[dict[str, Any]],
            *,
            tools: list[dict[str, Any]] | None = None,
        ) -> Any:
            assert tools is not None and len(tools) == 1
            self.calls += 1
            function = _mapping(tools[0].get("function"), "tool.function")
            schema = _mapping(function.get("parameters"), "tool.parameters")
            properties = _mapping(schema.get("properties"), "tool.properties")
            for name in ("entities", "dates", "amounts"):
                parameter = _mapping(properties.get(name), f"tool.properties.{name}")
                assert parameter.get("type") == "array"
                assert _mapping(parameter.get("items"), f"tool.properties.{name}.items") == {
                    "type": "string"
                }
            return self.fallback.chat(messages, tools=tools)

    inputs_type = namespace["Inputs"]
    run_workflow = namespace["run_workflow"]
    provider = SchemaValidatingProvider()
    result = run_workflow(  # type: ignore[operator]
        inputs_type(text="Alice signed the agreement on 2026-07-14"),  # type: ignore[operator]
        provider=provider,
    )

    assert provider.calls == 1
    assert isinstance(result, dict)
