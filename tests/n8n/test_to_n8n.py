"""dify → n8n 转换测试：用 tests/dify/fixtures 的 seq.yml / branch.yml（文本入参）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ragspine.n8n import N8nConvertError, dify_to_n8n


def _by_name(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["name"]: n for n in result["nodes"]}


def test_seq_shape_and_mapping(dify_fixture_text: Callable[[str], str]) -> None:
    result, warnings = dify_to_n8n(dify_fixture_text("seq"))
    nodes = _by_name(result)
    # connections 键是节点 name，且都指向存在的节点。
    names = set(nodes)
    assert set(result["connections"]) <= names
    for conn_types in result["connections"].values():
        for ports in conn_types.values():
            for port in ports:
                for target in port:
                    assert target["node"] in names
    # start → manualTrigger。
    assert nodes["开始"]["type"] == "n8n-nodes-base.manualTrigger"
    # llm → agent + 配套 lmChat 节点（model 信息非空，provider anthropic）。
    agent = nodes["应答模型"]
    assert agent["type"] == "@n8n/n8n-nodes-langchain.agent"
    assert agent["parameters"]["options"]["systemMessage"] == "你是一个有帮助的助手。"
    assert agent["parameters"]["text"] == '={{ $node["开始"].json["question"] }}'
    lmchats = [n for n in nodes.values() if ".lmChatAnthropic" in n["type"]]
    assert len(lmchats) == 1
    conn = result["connections"][lmchats[0]["name"]]["ai_languageModel"]
    assert conn[0][0]["node"] == "应答模型"
    # template-transform → set：变量引用换算（dify llm "text" → n8n agent "output"）。
    set_node = nodes["包装输出"]
    assert set_node["type"] == "n8n-nodes-base.set"
    assignments = set_node["parameters"]["assignments"]["assignments"]
    assert assignments[0]["value"] == '=回答：{{ $node["应答模型"].json["output"] }}'
    # end → noOp（name 保留原 title）+ 原始 data 存 notes + warning。
    end_node = nodes["结束"]
    assert end_node["type"] == "n8n-nodes-base.noOp"
    assert '"type": "end"' in end_node["notes"]
    assert any("noOp" in w and "end" in w for w in warnings)
    # 每个节点都有 position（[x, y] 双元素）。
    for node in nodes.values():
        assert isinstance(node["position"], list) and len(node["position"]) == 2


def test_branch_if_ports_and_answer(dify_fixture_text: Callable[[str], str]) -> None:
    result, warnings = dify_to_n8n(dify_fixture_text("branch"))
    nodes = _by_name(result)
    ifnode = nodes["阈值判断"]
    assert ifnode["type"] == "n8n-nodes-base.if"
    cond = ifnode["parameters"]["conditions"]["conditions"][0]
    assert cond["leftValue"] == '={{ $node["开始"].json["score"] }}'
    assert cond["operator"]["operation"] == "gt"
    assert cond["operator"]["type"] == "number"
    # if-else 的 true/false → 端口 0/1。
    main = result["connections"]["阈值判断"]["main"]
    assert main[0][0]["node"] == "通过应答"
    assert main[1][0]["node"] == "未过应答"
    # answer → noOp + warning。
    answer_node = nodes["回复"]
    assert answer_node["type"] == "n8n-nodes-base.noOp"
    assert any("noOp" in w for w in warnings)
    # 两个 llm 各自带配套 lmChat。
    lmchats = [n for n in nodes.values() if ".lmChat" in n["type"]]
    assert len(lmchats) == 2


def test_position_carried_over_from_dify() -> None:
    doc = {
        "app": {"mode": "workflow", "name": "pos-demo"},
        "workflow": {
            "graph": {
                "nodes": [
                    {
                        "id": "start_1",
                        "position": {"x": 100, "y": 50},
                        "data": {"type": "start", "title": "开始", "variables": []},
                    }
                ],
                "edges": [],
            }
        },
    }
    result, _ = dify_to_n8n(doc)
    assert result["nodes"][0]["position"] == [100, 50]


def test_top_level_defaults() -> None:
    doc = {
        "app": {"mode": "workflow", "name": "empty"},
        "workflow": {"graph": {"nodes": [], "edges": []}},
    }
    result, _ = dify_to_n8n(doc)
    assert result["name"] == "empty"
    assert result["settings"] == {"executionOrder": "v1"}
    assert result["pinData"] == {}


def test_invalid_dify_doc_raises() -> None:
    with pytest.raises(N8nConvertError) as exc_info:
        dify_to_n8n({"workflow": {"graph": {"nodes": [], "edges": []}}})  # 缺 app
    assert exc_info.value.code.startswith("n8n.")
