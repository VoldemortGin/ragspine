"""n8n → dify 转换测试：产物可过 dify parse+lower、节点映射、归并、warning 完备。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import yaml

from ragspine.dify import lower_to_ir, parse_dify_yaml
from ragspine.n8n import n8n_to_dify


def _nodes_by_id(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in doc["workflow"]["graph"]["nodes"]}


def _edges(doc: dict[str, Any]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = doc["workflow"]["graph"]["edges"]
    return edges


@pytest.mark.parametrize("name", ["linear", "branch", "unknown"])
def test_output_passes_dify_parse_and_lower(
    name: str, fixture_text: Callable[[str], str]
) -> None:
    doc, _warnings = n8n_to_dify(fixture_text(name))
    dumped = yaml.safe_dump(doc, allow_unicode=True)
    parsed = parse_dify_yaml(dumped)
    ir = lower_to_ir(parsed)  # 不抛即通过
    assert ir.topo_order


def test_linear_node_mapping(fixture_json: Callable[[str], dict[str, Any]]) -> None:
    src = fixture_json("linear")
    doc, warnings = n8n_to_dify(src)
    nodes = _nodes_by_id(doc)
    types = {nid: n["data"]["type"] for nid, n in nodes.items()}
    assert types["when_clicking_execute_workflow"] == "start"
    assert types["ai_agent"] == "llm"
    assert types["format_output"] == "template-transform"
    # 合成 end：无出边的终端节点统一接入。
    end_nodes = [n for n in nodes.values() if n["data"]["type"] == "end"]
    assert len(end_nodes) == 1
    assert end_nodes[0]["data"]["_n8n"] == {"synthetic": True}
    outputs = end_nodes[0]["data"]["outputs"]
    assert outputs[0]["value_selector"] == ["format_output", "output"]
    # 边：start → llm → template-transform → end。
    pairs = {(e["source"], e["target"]) for e in _edges(doc)}
    assert ("when_clicking_execute_workflow", "ai_agent") in pairs
    assert ("ai_agent", "format_output") in pairs
    assert ("format_output", end_nodes[0]["id"]) in pairs
    # position 带过去。
    assert nodes["ai_agent"]["position"] == {"x": 220, "y": 0}
    # 每个映射节点 data._n8n = 原始 n8n 节点完整 dict（round-trip 机制）。
    src_by_name = {n["name"]: n for n in src["nodes"]}
    agent_n8n = nodes["ai_agent"]["data"]["_n8n"]
    for key, value in src_by_name["AI Agent"].items():
        assert agent_n8n[key] == value


def test_linear_lmchat_merged_into_llm(
    fixture_json: Callable[[str], dict[str, Any]],
) -> None:
    doc, warnings = n8n_to_dify(fixture_json("linear"))
    llm = _nodes_by_id(doc)["ai_agent"]["data"]
    assert llm["model"]["provider"] == "anthropic"
    assert llm["model"]["name"] == "claude-sonnet-4-5"
    assert llm["model"]["completion_params"] == {}
    # attachment 完整原始 JSON 存 _n8n.ai_attachments。
    attachments = llm["_n8n"]["ai_attachments"]
    assert len(attachments) == 1
    assert attachments[0]["connection_type"] == "ai_languageModel"
    assert attachments[0]["node"]["name"] == "Anthropic Chat Model"
    assert attachments[0]["node"]["type"] == "@n8n/n8n-nodes-langchain.lmChatAnthropic"
    assert any("lmChat" in w for w in warnings)
    # prompt_template：system(options.systemMessage) + user(text，经变量转换)。
    prompt = llm["prompt_template"]
    assert prompt[0] == {"role": "system", "text": "You are a helpful assistant."}
    assert prompt[1] == {
        "role": "user",
        "text": "{{#when_clicking_execute_workflow.question#}}",
    }
    # start.variables 后置 pass：被引用的 question 声明进 start。
    start = _nodes_by_id(doc)["when_clicking_execute_workflow"]["data"]
    assert [v["variable"] for v in start["variables"]] == ["question"]


def test_linear_set_becomes_template_transform(
    fixture_json: Callable[[str], dict[str, Any]],
) -> None:
    doc, _ = n8n_to_dify(fixture_json("linear"))
    tt = _nodes_by_id(doc)["format_output"]["data"]
    assert tt["type"] == "template-transform"
    # agent 输出字段换算：n8n "output" → dify llm "text"。
    assert tt["variables"] == [{"variable": "result", "value_selector": ["ai_agent", "text"]}]
    assert tt["template"] == "{{ result }}"


def test_branch_if_and_source_handles(
    fixture_json: Callable[[str], dict[str, Any]],
) -> None:
    doc, _ = n8n_to_dify(fixture_json("branch"))
    nodes = _nodes_by_id(doc)
    assert nodes["webhook"]["data"]["type"] == "start"
    ifelse = nodes["check_score"]["data"]
    assert ifelse["type"] == "if-else"
    case = ifelse["cases"][0]
    assert case["case_id"] == "true"
    assert case["logical_operator"] == "and"
    cond = case["conditions"][0]
    assert cond["variable_selector"] == ["webhook", "score"]
    assert cond["comparison_operator"] == ">"
    assert cond["value"] == "60"
    # sourceHandle：if 端口 0 → "true"、1 → "false"。
    handles = {
        (e["source"], e["target"]): e["sourceHandle"] for e in _edges(doc)
    }
    assert handles[("check_score", "approve")] == "true"
    assert handles[("check_score", "reject")] == "false"
    # 后置 pass：if 条件里对 start 的 value_selector 引用声明进 start.variables。
    start_vars = [v["variable"] for v in nodes["webhook"]["data"]["variables"]]
    assert "score" in start_vars


def test_unknown_passthrough_and_noop_splice(
    fixture_json: Callable[[str], dict[str, Any]],
) -> None:
    src = fixture_json("unknown")
    doc, warnings = n8n_to_dify(src)
    nodes = _nodes_by_id(doc)
    # 未知类型 → "n8n-passthrough"，原始数据保留在 _n8n。
    fetch = nodes["fetch_data"]["data"]
    assert fetch["type"] == "n8n-passthrough"
    assert fetch["title"] == "Fetch Data"
    assert fetch["_n8n"]["type"] == "n8n-nodes-base.httpRequest"
    assert fetch["_n8n"]["parameters"]["url"] == "https://example.com/api/data"
    assert any("n8n-passthrough" in w for w in warnings)
    # code：language python → code_language python3 + code=pythonCode。
    code = nodes["process"]["data"]
    assert code["type"] == "code"
    assert code["code_language"] == "python3"
    assert code["code"] == src["nodes"][2]["parameters"]["pythonCode"]
    # noOp "Done" 被 splice：不出现在节点里，且有 warning。
    titles = {n["data"].get("title") for n in nodes.values()}
    assert "Done" not in titles
    assert any("noOp" in w and "Done" in w for w in warnings)
    # Output 成为终端 → 接合成 end。
    end_id = next(n["id"] for n in nodes.values() if n["data"]["type"] == "end")
    assert ("output", end_id) in {(e["source"], e["target"]) for e in _edges(doc)}


def test_top_level_shape_and_x_n8n(
    fixture_json: Callable[[str], dict[str, Any]],
) -> None:
    src = fixture_json("linear")
    doc, _ = n8n_to_dify(src)
    assert doc["app"] == {"mode": "workflow", "name": "Linear Demo"}
    assert doc["kind"] == "app"
    assert doc["version"] == "0.1.5"
    # workflow 级其余键整体存 x_n8n（round-trip 用）。
    assert doc["x_n8n"]["settings"] == src["settings"]
    assert doc["x_n8n"]["meta"] == src["meta"]
    assert doc["x_n8n"]["pinData"] == {}


def test_warnings_never_silent(fixture_json: Callable[[str], dict[str, Any]]) -> None:
    # 每个无法语义映射处均有对应 warning，绝不静默丢弃。
    _, linear_warnings = n8n_to_dify(fixture_json("linear"))
    assert any("lmChat" in w for w in linear_warnings)
    _, unknown_warnings = n8n_to_dify(fixture_json("unknown"))
    assert any("n8n-passthrough" in w for w in unknown_warnings)  # 未知类型
    assert any("noOp" in w for w in unknown_warnings)  # splice
