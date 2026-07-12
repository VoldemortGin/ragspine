"""parse 段测试：合法解析（dict / JSON 字符串）与非法输入归一到 N8nConvertError。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ragspine.n8n import N8nConvertError, parse_n8n_workflow


def test_parse_dict_input(fixture_json: Callable[[str], dict[str, Any]]) -> None:
    wf = parse_n8n_workflow(fixture_json("linear"))
    assert wf.name == "Linear Demo"
    names = {n.name for n in wf.nodes}
    assert names == {
        "When clicking 'Execute workflow'",
        "AI Agent",
        "Anthropic Chat Model",
        "Format Output",
    }


def test_parse_json_string_input(fixture_text: Callable[[str], str]) -> None:
    wf = parse_n8n_workflow(fixture_text("branch"))
    assert wf.name == "Branch Demo"
    assert {n.name for n in wf.nodes} == {"Webhook", "Check Score", "Approve", "Reject"}
    # 节点关键字段被保留（含未建模的额外键，如 webhookId）。
    webhook = next(n for n in wf.nodes if n.name == "Webhook")
    assert webhook.type == "n8n-nodes-base.webhook"
    assert webhook.parameters["path"] == "score-check"


def test_parse_missing_nodes_raises() -> None:
    with pytest.raises(N8nConvertError) as exc_info:
        parse_n8n_workflow({"name": "x", "connections": {}})
    assert exc_info.value.code.startswith("n8n.")


def test_parse_connections_unknown_target_raises() -> None:
    source = {
        "name": "bad",
        "nodes": [
            {"id": "1", "name": "A", "type": "n8n-nodes-base.noOp", "typeVersion": 1,
             "position": [0, 0], "parameters": {}},
        ],
        "connections": {"A": {"main": [[{"node": "Ghost", "type": "main", "index": 0}]]}},
    }
    with pytest.raises(N8nConvertError) as exc_info:
        parse_n8n_workflow(source)
    assert exc_info.value.code.startswith("n8n.")
    assert "Ghost" in str(exc_info.value)


def test_parse_connections_unknown_source_raises() -> None:
    source = {
        "name": "bad",
        "nodes": [
            {"id": "1", "name": "A", "type": "n8n-nodes-base.noOp", "typeVersion": 1,
             "position": [0, 0], "parameters": {}},
        ],
        "connections": {"Ghost": {"main": [[{"node": "A", "type": "main", "index": 0}]]}},
    }
    with pytest.raises(N8nConvertError):
        parse_n8n_workflow(source)


@pytest.mark.parametrize("text", ["[1, 2, 3]", "- a\n- b\n", "just a plain scalar"])
def test_parse_non_mapping_top_level_raises(text: str) -> None:
    with pytest.raises(N8nConvertError) as exc_info:
        parse_n8n_workflow(text)
    assert exc_info.value.code.startswith("n8n.")
