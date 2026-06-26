"""W7c GraphStore 缝 —— 内存默认实现 InProcessGraphStore 的领域语义 + 工厂注册表单测。

机制层的「实现 × 脊柱不变量」笛卡尔积 conformance 在 tests/conformance/test_graph_store.py；
本文件钉死内存默认实现的领域合约（邻居 / 子图 / 多跳遍历 / RESTRICTED 隔离 / 确定性 /
provenance 回传）与 make_graph_store 选型工厂（none / in_process / 别名 / 未知报错 / env）。

TDD 红：GraphStore 落地前 import 即 ModuleNotFoundError（逐条红，不中断整轮收集）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.graph.store import (
    GRAPH_STORE_ENV,
    GraphEdge,
    GraphNode,
    GraphStore,
    InProcessGraphStore,
    Subgraph,
    make_graph_store,
)


def _node(node_id: str, ntype: str = "entity", *, sensitivity: str = "INTERNAL", **md) -> GraphNode:
    meta = {
        "source_doc_id": md.pop("source_doc_id", "profile"),
        "source_locator": md.pop("source_locator", f"config#{node_id}"),
        "sensitivity": sensitivity,
    }
    meta.update({k: str(v) for k, v in md.items()})
    return GraphNode(id=node_id, type=ntype, label=md.get("label", node_id), metadata=meta)


def _edge(src: str, dst: str, etype: str, **md) -> GraphEdge:
    meta = {
        "source_doc_id": md.pop("source_doc_id", "profile"),
        "source_locator": md.pop("source_locator", f"config#{src}->{dst}"),
    }
    meta.update({k: str(v) for k, v in md.items()})
    return GraphEdge(src=src, dst=dst, type=etype, metadata=meta)


# ---------------------------------------------------------------------------
# Protocol / 基本 upsert + count
# ---------------------------------------------------------------------------

def test_in_process_is_runtime_checkable_graph_store():
    assert isinstance(InProcessGraphStore(), GraphStore)


def test_upsert_counts_and_replaces():
    g = InProcessGraphStore()
    assert g.upsert_nodes([_node("A"), _node("B"), _node("C")]) == 3
    assert g.upsert_edges([_edge("A", "B", "parent_of"), _edge("A", "C", "parent_of")]) == 2
    assert g.count_nodes() == 3
    assert g.count_edges() == 2
    # upsert 是替换非追加：同 id 节点 / 同 (src,dst,type) 边覆盖。
    g.upsert_nodes([_node("A", label="A2")])
    g.upsert_edges([_edge("A", "B", "parent_of", note="x")])
    assert g.count_nodes() == 3
    assert g.count_edges() == 2
    assert g.get_node("A").label == "A2"  # 同 id 覆盖
    [edge] = [e for e in g.subgraph(["A"], depth=1).edges if e.dst == "B"]
    assert edge.metadata["note"] == "x"  # 同 (src,dst,type) 覆盖


def test_empty_upsert_is_zero():
    g = InProcessGraphStore()
    assert g.upsert_nodes([]) == 0
    assert g.upsert_edges([]) == 0
    assert g.count_nodes() == 0


# ---------------------------------------------------------------------------
# neighbors / provenance round-trip
# ---------------------------------------------------------------------------

def test_neighbors_out_direction_and_edge_type_filter():
    g = InProcessGraphStore()
    g.upsert_nodes([_node("GROUP"), _node("HK"), _node("CN"), _node("JINGAN", "external_entity")])
    g.upsert_edges([
        _edge("GROUP", "HK", "parent_of"),
        _edge("GROUP", "CN", "parent_of"),
        _edge("GROUP", "JINGAN", "competes_with"),
    ])
    subs = g.neighbors("GROUP", edge_type="parent_of")
    assert [n.id for n in subs] == ["CN", "HK"]  # 按 id 升序，确定性
    peers = g.neighbors("GROUP", edge_type="competes_with")
    assert [n.id for n in peers] == ["JINGAN"]
    # 不限 edge_type：全部出邻
    assert {n.id for n in g.neighbors("GROUP")} == {"CN", "HK", "JINGAN"}


def test_neighbors_in_direction():
    g = InProcessGraphStore()
    g.upsert_nodes([_node("GROUP"), _node("HK")])
    g.upsert_edges([_edge("GROUP", "HK", "parent_of")])
    assert [n.id for n in g.neighbors("HK", direction="in")] == ["GROUP"]
    assert g.neighbors("HK", direction="out") == []


def test_provenance_round_trips_on_nodes_and_edges():
    g = InProcessGraphStore()
    g.upsert_nodes([
        _node("HK", "entity", source_doc_id="HK_FIN.pptx", source_locator="HK_FIN.pptx!slide3"),
    ])
    g.upsert_nodes([_node("GROUP")])
    g.upsert_edges([
        _edge("GROUP", "HK", "parent_of", source_doc_id="company.toml", source_locator="company.toml#home"),
    ])
    hk = g.get_node("HK")
    assert hk.metadata["source_doc_id"] == "HK_FIN.pptx"
    assert hk.metadata["source_locator"] == "HK_FIN.pptx!slide3"
    sub = g.subgraph(["GROUP"], depth=1)
    [e] = [e for e in sub.edges if e.type == "parent_of"]
    assert e.metadata["source_doc_id"] == "company.toml"
    assert e.metadata["source_locator"] == "company.toml#home"


# ---------------------------------------------------------------------------
# RESTRICTED 隔离（新出口，继承两出口的不变量）
# ---------------------------------------------------------------------------

def test_restricted_node_never_surfaces_in_neighbors():
    g = InProcessGraphStore()
    g.upsert_nodes([_node("DOC", "doc"), _node("SECRET", "doc", sensitivity="RESTRICTED"), _node("PUB", "doc")])
    g.upsert_edges([_edge("DOC", "SECRET", "mentions"), _edge("DOC", "PUB", "mentions")])
    out_ids = {n.id for n in g.neighbors("DOC")}
    assert "SECRET" not in out_ids
    assert "PUB" in out_ids


def test_restricted_node_never_surfaces_in_traverse_or_subgraph():
    g = InProcessGraphStore()
    g.upsert_nodes([_node("A"), _node("B"), _node("SECRET", sensitivity="RESTRICTED"), _node("C")])
    # A -> B -> C 链，且 A -> SECRET -> C：SECRET 既不出现、也不作跳板
    g.upsert_edges([
        _edge("A", "B", "rel"), _edge("B", "C", "rel"),
        _edge("A", "SECRET", "rel"), _edge("SECRET", "C", "rel"),
    ])
    reached = {n.id for n in g.traverse("A", max_depth=3)}
    assert "SECRET" not in reached
    assert {"B", "C"} <= reached
    sub = g.subgraph(["A"], depth=3)
    assert "SECRET" not in {n.id for n in sub.nodes}
    # 触及 RESTRICTED 的边也不得出现在子图里
    assert all("SECRET" not in (e.src, e.dst) for e in sub.edges)


def test_get_node_hides_restricted():
    g = InProcessGraphStore()
    g.upsert_nodes([_node("SECRET", sensitivity="RESTRICTED")])
    assert g.get_node("SECRET") is None
    assert g.get_node("missing") is None


def test_cannot_traverse_from_restricted_start():
    g = InProcessGraphStore()
    g.upsert_nodes([_node("SECRET", sensitivity="RESTRICTED"), _node("B")])
    g.upsert_edges([_edge("SECRET", "B", "rel")])
    assert g.neighbors("SECRET") == []
    assert g.traverse("SECRET") == []


# ---------------------------------------------------------------------------
# where 过滤（可选的通用元数据过滤，叠加在隔离之上）
# ---------------------------------------------------------------------------

def test_where_filter_on_neighbors():
    g = InProcessGraphStore()
    g.upsert_nodes([_node("ROOT"), _node("M1", "metric"), _node("E1", "entity")])
    g.upsert_edges([_edge("ROOT", "M1", "mentions"), _edge("ROOT", "E1", "mentions")])
    only_metrics = g.neighbors("ROOT", where={"type": "metric"})
    assert [n.id for n in only_metrics] == ["M1"]


# ---------------------------------------------------------------------------
# 多跳遍历（flat top-k + 精确 SQL 做不到的能力）
# ---------------------------------------------------------------------------

def test_traverse_multi_hop_respects_max_depth_and_edge_types():
    g = InProcessGraphStore()
    g.upsert_nodes([_node(x) for x in ("A", "B", "C", "D")])
    g.upsert_edges([
        _edge("A", "B", "derives"), _edge("B", "C", "derives"), _edge("C", "D", "derives"),
        _edge("A", "D", "other"),
    ])
    assert {n.id for n in g.traverse("A", edge_types=["derives"], max_depth=1)} == {"B"}
    assert {n.id for n in g.traverse("A", edge_types=["derives"], max_depth=2)} == {"B", "C"}
    # other 边被 edge_types 过滤掉，D 仅经 3 跳 derives 才可达
    assert {n.id for n in g.traverse("A", edge_types=["derives"], max_depth=3)} == {"B", "C", "D"}


def test_determinism_traverse_stable_across_instances():
    def build():
        g = InProcessGraphStore()
        g.upsert_nodes([_node(f"n{i}") for i in range(8)])
        g.upsert_edges([_edge("n0", f"n{i}", "rel") for i in range(1, 8)])
        return g
    first = [n.id for n in build().traverse("n0", max_depth=2)]
    second = [n.id for n in build().traverse("n0", max_depth=2)]
    assert first == second == sorted(first)


def test_subgraph_returns_typed_result():
    g = InProcessGraphStore()
    g.upsert_nodes([_node("A"), _node("B")])
    g.upsert_edges([_edge("A", "B", "rel")])
    sub = g.subgraph(["A"], depth=1)
    assert isinstance(sub, Subgraph)
    assert {n.id for n in sub.nodes} == {"A", "B"}
    assert [(e.src, e.dst) for e in sub.edges] == [("A", "B")]


# ---------------------------------------------------------------------------
# make_graph_store 工厂（范式同 make_vector_store）
# ---------------------------------------------------------------------------

def test_make_graph_store_none_returns_none():
    assert make_graph_store(None) is None
    assert make_graph_store("none") is None


def test_make_graph_store_in_process_aliases():
    for spec in ("in_process", "in-process", "memory", "InProcess", " IN_PROCESS "):
        store = make_graph_store(spec)
        assert isinstance(store, InProcessGraphStore)


def test_make_graph_store_unknown_raises():
    with pytest.raises(ValueError, match="未知 graph store"):
        make_graph_store("nonsuch")


def test_make_graph_store_reads_env(monkeypatch):
    monkeypatch.setenv(GRAPH_STORE_ENV, "in_process")
    assert isinstance(make_graph_store(), InProcessGraphStore)
    monkeypatch.delenv(GRAPH_STORE_ENV, raising=False)
    assert make_graph_store() is None
