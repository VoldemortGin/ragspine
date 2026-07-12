"""round-trip 保真度：n8n → dify → n8n 后节点/参数/拓扑/位置保持。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ragspine.n8n import dify_to_n8n, n8n_to_dify


def _roundtrip(src: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    doc, forward_warnings = n8n_to_dify(src)
    back, backward_warnings = dify_to_n8n(doc)
    return back, forward_warnings, backward_warnings


def _main_topology(workflow: dict[str, Any]) -> set[tuple[str, int, str]]:
    triples: set[tuple[str, int, str]] = set()
    for src_name, conn_types in workflow["connections"].items():
        for port_idx, port in enumerate(conn_types.get("main", [])):
            for target in port or []:
                triples.add((src_name, port_idx, target["node"]))
    return triples


@pytest.mark.parametrize("name", ["linear", "branch"])
def test_roundtrip_lossless(
    name: str, fixture_json: Callable[[str], dict[str, Any]]
) -> None:
    src = fixture_json(name)
    back, _, _ = _roundtrip(src)
    # 节点 name 集合保持。
    assert {n["name"] for n in back["nodes"]} == {n["name"] for n in src["nodes"]}
    # 每个 name 的 type/typeVersion/parameters/position 保持（== 原值）。
    back_by_name = {n["name"]: n for n in back["nodes"]}
    for node in src["nodes"]:
        restored = back_by_name[node["name"]]
        assert restored["type"] == node["type"]
        assert restored["typeVersion"] == node["typeVersion"]
        assert restored["parameters"] == node["parameters"]
        assert restored["position"] == node["position"]
    # 主链 connections 拓扑等价（含 ai_languageModel 等非 main 连接原样还原）。
    assert back["connections"] == src["connections"]
    # workflow 级其余键（settings/meta/pinData）经 x_n8n 还原。
    assert back["settings"] == src["settings"]
    assert back["name"] == src["name"]


def test_roundtrip_linear_ai_language_model_restored(
    fixture_json: Callable[[str], dict[str, Any]],
) -> None:
    back, _, _ = _roundtrip(fixture_json("linear"))
    conn = back["connections"]["Anthropic Chat Model"]["ai_languageModel"]
    assert conn == [[{"node": "AI Agent", "type": "ai_languageModel", "index": 0}]]


def test_roundtrip_unknown_noop_spliced(
    fixture_json: Callable[[str], dict[str, Any]],
) -> None:
    src = fixture_json("unknown")
    back, forward_warnings, _ = _roundtrip(src)
    # noOp "Done" 例外：被 splice，显式断言并有 warning。
    src_names = {n["name"] for n in src["nodes"]}
    assert {n["name"] for n in back["nodes"]} == src_names - {"Done"}
    assert any("Done" in w for w in forward_warnings)
    # 其余节点 type/typeVersion/parameters/position 保持。
    back_by_name = {n["name"]: n for n in back["nodes"]}
    for node in src["nodes"]:
        if node["name"] == "Done":
            continue
        restored = back_by_name[node["name"]]
        assert restored["type"] == node["type"]
        assert restored["typeVersion"] == node["typeVersion"]
        assert restored["parameters"] == node["parameters"]
        assert restored["position"] == node["position"]
    # 主链拓扑等价（剔除触及 Done 的边）。
    expected = {t for t in _main_topology(src) if t[0] != "Done" and t[2] != "Done"}
    assert _main_topology(back) == expected
