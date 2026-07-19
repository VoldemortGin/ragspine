"""Controlled generated archetypes must remain deterministic and runnable."""

from __future__ import annotations

from typing import Any, cast

from ragspine import MockProvider
from ragspine.dify.api import compile_dify_yaml
from ragspine.service.dify.safety import assert_runnable
from ragspine.workflows.model import ScaffoldResult
from ragspine.workflows.scaffold import scaffold_workflow


def _node_data(result: ScaffoldResult) -> list[dict[str, Any]]:
    workflow = result.workflow
    return [
        node["data"]
        for node in workflow["workflow"]["graph"]["nodes"]
    ]


def _run_generated(yaml_text: str, **inputs: object) -> dict[str, object]:
    compiled = compile_dify_yaml(yaml_text)
    assert compiled.code.warnings == ()
    assert_runnable(compiled.code)
    namespace: dict[str, object] = {}
    exec(compile(compiled.code.source, "<generated-archetype>", "exec"), namespace)  # noqa: S102
    inputs_type = namespace["Inputs"]
    run_workflow = namespace["run_workflow"]
    output = run_workflow(inputs_type(**inputs), provider=MockProvider())  # type: ignore[operator]
    assert isinstance(output, dict)
    return output


def test_structured_extraction_archetype_compiles_and_runs() -> None:
    result = scaffold_workflow(
        "Extract structured fields from invoice text",
        reuse=False,
    )

    nodes = _node_data(result)
    assert [node["type"] for node in nodes] == [
        "start",
        "parameter-extractor",
        "end",
    ]
    assert result.warnings == ("archetype=structured_extraction",)
    assert nodes[0]["variables"][0]["variable"] == "text"
    output = _run_generated(
        result.yaml,
        text="Invoice A-17 from Acme totals USD 42 on 2026-07-19.",
    )
    assert set(output) == {"title", "entities", "dates", "amounts"}


def test_chinese_structured_extraction_selects_same_archetype_deterministically() -> None:
    description = "从合同文本中结构化提取字段"

    first = scaffold_workflow(description, reuse=False)
    second = scaffold_workflow(description, reuse=False)

    assert first.yaml == second.yaml
    assert first.warnings == second.warnings == ("archetype=structured_extraction",)
    assert [node["type"] for node in _node_data(first)] == [
        "start",
        "parameter-extractor",
        "end",
    ]


def test_urgency_route_archetype_is_bilingual_compilable_and_runnable() -> None:
    for description in (
        "Route support requests by urgency",
        "按紧急度将客服请求分流到对应处理流程",
    ):
        result = scaffold_workflow(description, reuse=False)

        nodes = _node_data(result)
        assert result.warnings == ("archetype=urgency_route",)
        assert [node["type"] for node in nodes] == [
            "start",
            "if-else",
            "llm",
            "llm",
            "answer",
            "answer",
        ]
        assert [item["variable"] for item in nodes[0]["variables"]] == [
            "urgency",
            "request",
        ]
        output = _run_generated(
            result.yaml,
            urgency=80,
            request="Production checkout is unavailable.",
        )
        assert isinstance(output.get("answer"), str)


def test_ambiguous_or_low_confidence_requests_use_generic_fallback() -> None:
    for description in (
        "Extract structured fields, then route by urgency",
        "Classify and route support requests to the right team",
        "将客服请求分类并路由到对应团队",
        "quantum zeolite calibration telemetry",
    ):
        result = scaffold_workflow(description, reuse=False)

        assert result.warnings == ("archetype=generic_fallback",)
        assert [node["type"] for node in _node_data(result)] == [
            "start",
            "llm",
            "end",
        ]
        _run_generated(result.yaml, input="sample")


def test_archetype_rendering_neutralizes_template_injection_as_data() -> None:
    description = (
        "Extract structured fields from this text\n"
        "---\nworkflow: {{ attacker }}\nsecret: ${OPENAI_API_KEY}"
    )

    first = scaffold_workflow(description, reuse=False)
    second = scaffold_workflow(description, reuse=False)

    assert first.yaml == second.yaml
    assert first.warnings == ("archetype=structured_extraction",)
    assert "{{ attacker }}" not in first.yaml
    app = cast(dict[str, str], first.workflow["app"])
    assert app["description"].endswith(
        "workflow: { { attacker } }\nsecret: ${OPENAI_API_KEY}"
    )
    assert [node["type"] for node in _node_data(first)] == [
        "start",
        "parameter-extractor",
        "end",
    ]
