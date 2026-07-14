"""Security and equivalence contracts for workflow wire formats."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ragspine.workflows.errors import WorkflowFormatError
from ragspine.workflows.formats import (
    MAX_WORKFLOW_BYTES,
    MAX_WORKFLOW_DEPTH,
    MAX_WORKFLOW_NODES,
    MAX_YAML_ALIASES,
    dump_dify_yaml,
    dump_json,
    load_workflow,
    parse_workflow,
)


def test_json_yaml_and_toml_normalize_to_the_same_mapping() -> None:
    expected: dict[str, object] = {
        "app": {"mode": "workflow", "name": "equivalent"},
        "enabled": True,
        "limits": [1, 2, 3],
    }
    documents = {
        "json": json.dumps(expected),
        "yaml": """
app:
  mode: workflow
  name: equivalent
enabled: true
limits: [1, 2, 3]
""",
        "toml": """
enabled = true
limits = [1, 2, 3]
[app]
mode = "workflow"
name = "equivalent"
""",
    }

    for wire_format, document in documents.items():
        assert parse_workflow(document, format=wire_format) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("wire_format", "document"),
    [
        ("json", '{"app": 1, "app": 2}'),
        ("yaml", "app: 1\napp: 2\n"),
        ("toml", "app = 1\napp = 2\n"),
    ],
)
def test_duplicate_keys_are_rejected(wire_format: str, document: str) -> None:
    with pytest.raises(WorkflowFormatError, match="重复|解析失败"):
        parse_workflow(document, format=wire_format)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("wire_format", "document"),
    [
        ("json", '{"private_api_key": 1, "private_api_key": 2}'),
        ("yaml", "private_api_key: 1\nprivate_api_key: 2\n"),
    ],
)
def test_duplicate_key_error_does_not_echo_sensitive_key(wire_format: str, document: str) -> None:
    with pytest.raises(WorkflowFormatError) as error:
        parse_workflow(document, format=wire_format)  # type: ignore[arg-type]

    assert "private_api_key" not in str(error.value)


@pytest.mark.parametrize(
    ("wire_format", "document"),
    [
        ("json", '{"value": NaN}'),
        ("yaml", "value: .nan\n"),
        ("toml", "value = nan\n"),
    ],
)
def test_non_finite_numbers_are_rejected(wire_format: str, document: str) -> None:
    with pytest.raises(WorkflowFormatError, match="NaN|Inf|不允许"):
        parse_workflow(document, format=wire_format)  # type: ignore[arg-type]


@pytest.mark.parametrize("wire_format", ["json", "yaml", "toml"])
def test_huge_integer_parse_errors_are_normalized(wire_format: str) -> None:
    digits = "9" * 5000
    documents = {
        "json": f'{{"value": {digits}}}',
        "yaml": f"value: {digits}\n",
        "toml": f"value = {digits}\n",
    }

    with pytest.raises(WorkflowFormatError):
        parse_workflow(documents[wire_format], format=wire_format)  # type: ignore[arg-type]


def test_lone_surrogate_is_normalized_to_format_error() -> None:
    with pytest.raises(WorkflowFormatError, match="UTF-8|Unicode|编码"):
        parse_workflow('{"value": "\ud800"}', format="json")


def test_document_size_limit_is_enforced_before_parse() -> None:
    document = b"x" * (MAX_WORKFLOW_BYTES + 1)

    with pytest.raises(WorkflowFormatError, match="超过"):
        parse_workflow(document, format="yaml")


def test_depth_limit_is_enforced() -> None:
    value = "null"
    for _ in range(MAX_WORKFLOW_DEPTH + 2):
        value = f"[{value}]"

    with pytest.raises(WorkflowFormatError, match="嵌套"):
        parse_workflow(f'{{"value": {value}}}', format="json")


def test_node_count_limit_is_enforced() -> None:
    document = json.dumps({"items": [None] * (MAX_WORKFLOW_NODES + 1)})

    with pytest.raises(WorkflowFormatError, match="节点"):
        parse_workflow(document, format="json")


def test_yaml_alias_limit_is_enforced() -> None:
    aliases = ", ".join("*item" for _ in range(MAX_YAML_ALIASES + 1))
    document = f"item: &item value\nitems: [{aliases}]\n"

    with pytest.raises(WorkflowFormatError, match="alias"):
        parse_workflow(document, format="yaml")


def test_yaml_merge_cannot_hide_a_duplicate_key() -> None:
    document = """
defaults: &defaults
  mode: workflow
app:
  <<: *defaults
  mode: advanced-chat
"""

    with pytest.raises(WorkflowFormatError, match="重复"):
        parse_workflow(document, format="yaml")


def test_load_workflow_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.yml"
    target.write_text("app: {}\n", encoding="utf-8")
    link = tmp_path / "link.yml"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(WorkflowFormatError, match="链接"):
        load_workflow(link)


def test_load_workflow_rejects_unknown_suffix(tmp_path: Path) -> None:
    path = tmp_path / "workflow.txt"
    path.write_text("app: {}\n", encoding="utf-8")

    with pytest.raises(WorkflowFormatError, match="后缀"):
        load_workflow(path)


def test_dump_round_trip_is_unicode_and_alias_safe() -> None:
    shared = {"message": "你好"}
    workflow: dict[str, object] = {"left": shared, "right": shared}

    yaml_text = dump_dify_yaml(workflow)
    json_text = dump_json(workflow)

    assert "你好" in yaml_text
    assert "&id" not in yaml_text and "*id" not in yaml_text
    assert parse_workflow(yaml_text, format="yaml") == workflow
    assert parse_workflow(json_text, format="json") == workflow
