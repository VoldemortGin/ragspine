"""GraphStore 缝 conformance（W7c）：把 provenance / isolation / determinism 绑死在缝上。

两层互补（同 VectorStore 缝的范式）：
  - 机制层：corespine.ConformanceSuite 把【每个注册实现】×脊柱不变量绑成笛卡尔积，
    parametrize_kwargs() 两行消费（范围收敛在零依赖 in_process 默认实现，见 conftest 注释）。
  - 领域层：fixture 形态参数化在 graph_store 夹具上（in_process + networkx 两实现各跑一遍），
    用【单一判定核】断言 provenance / isolation / determinism——任何实现只要登记进
    conftest.GRAPH_STORE_IMPLS 就必须证明这三项不变量，不通过的 adapter 直接 CI 红。

非空泛证明（同 Chunker / VectorStore 的「诚实反证」手法）：两个【故意】破不变量的 stub 喂进
【同一判定核】必须 AssertionError——证明 isolation / provenance 断言不是空泛通过：
  - _LeakyGraphStore：neighbors/traverse/subgraph 泄漏 RESTRICTED 节点 -> 触 isolation 判定核 FAIL。
  - _LineageDroppingGraphStore：返回丢血缘的节点/边 -> 触 provenance 判定核 FAIL。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.graph.store import (
    GraphEdge,
    GraphNode,
    InProcessGraphStore,
    Subgraph,
    make_graph_store,
)
from tests.conformance.conftest import GRAPH_STORE_SUITE


def _node(node_id: str, ntype: str = "entity", *, sensitivity: str = "INTERNAL", **md) -> GraphNode:
    meta = {
        "source_doc_id": md.pop("source_doc_id", "profile"),
        "source_locator": md.pop("source_locator", f"config#{node_id}"),
        "sensitivity": sensitivity,
    }
    meta.update({k: str(v) for k, v in md.items()})
    return GraphNode(id=node_id, type=ntype, label=node_id, metadata=meta)


def _edge(src: str, dst: str, etype: str, **md) -> GraphEdge:
    meta = {
        "source_doc_id": md.pop("source_doc_id", "profile"),
        "source_locator": md.pop("source_locator", f"config#{src}->{dst}"),
    }
    meta.update({k: str(v) for k, v in md.items()})
    return GraphEdge(src=src, dst=dst, type=etype, metadata=meta)


# ===========================================================================
# 机制层：corespine 套件消费者（实现 × 脊柱不变量 笛卡尔积；范式同 test_vector_store_suite）
# ===========================================================================

@pytest.mark.parametrize(**GRAPH_STORE_SUITE.parametrize_kwargs())
def test_graph_store_conformance(case):
    """每个 (实现 × 不变量) 格子：调用 thunk，满足静默、违反原样抛。"""
    case()


# ===========================================================================
# 领域层判定核：provenance / isolation / determinism（参数化用例与反证 stub 共用同一核）
# ===========================================================================

def _assert_provenance_complete(store) -> None:
    """判定核：neighbors/traverse/subgraph 返回的每个节点/边都带非空 source_doc_id + source_locator。

    先建一条带血缘的两跳链（GROUP -parent_of-> HK -reports-> METRIC），再从三种遍历出口收集
    返回的节点与边，逐个断言血缘非空；并断言收集集非空，杜绝「无返回的空泛通过」。
    """
    store.upsert_nodes([
        _node("GROUP", source_doc_id="company.toml", source_locator="company.toml#home"),
        _node("HK", source_doc_id="HK_FIN.pptx", source_locator="HK_FIN.pptx!slide3"),
        _node("METRIC", "metric", source_doc_id="HK_FIN.pptx", source_locator="HK_FIN.pptx!slide5"),
    ])
    store.upsert_edges([
        _edge("GROUP", "HK", "parent_of", source_doc_id="company.toml", source_locator="company.toml#home"),
        _edge("HK", "METRIC", "reports", source_doc_id="HK_FIN.pptx", source_locator="HK_FIN.pptx!slide5"),
    ])

    nodes = list(store.neighbors("GROUP")) + list(store.traverse("GROUP", max_depth=2))
    sub = store.subgraph(["GROUP"], depth=2)
    nodes += list(sub.nodes)
    assert nodes, "provenance 判定核未收集到任何节点（应至少回传可达节点）"
    for n in nodes:
        assert n.metadata.get("source_doc_id"), f"节点缺 source_doc_id（血缘根）：{n!r}"
        assert n.metadata.get("source_locator"), f"节点缺 source_locator（citation 回指）：{n!r}"

    assert sub.edges, "provenance 判定核未收集到任何边（应至少回传诱导子图边）"
    for e in sub.edges:
        assert e.metadata.get("source_doc_id"), f"边缺 source_doc_id（血缘根）：{e!r}"
        assert e.metadata.get("source_locator"), f"边缺 source_locator（citation 回指）：{e!r}"


def _assert_restricted_isolated(store) -> None:
    """判定核：RESTRICTED 来源节点绝不出现在 neighbors/traverse/subgraph，触及它的边也绝不出现。

    建 A -rel-> SECRET(RESTRICTED) -rel-> C 与 A -rel-> B -rel-> C：SECRET 既是 A 的直接邻、又是
    A->C 的潜在跳板，故同时压住「邻居泄漏」「跳板泄漏」「子图节点/边泄漏」四面。
    """
    store.upsert_nodes([
        _node("A"), _node("B"),
        _node("SECRET", "doc", sensitivity="RESTRICTED"), _node("C"),
    ])
    store.upsert_edges([
        _edge("A", "B", "rel"), _edge("B", "C", "rel"),
        _edge("A", "SECRET", "rel"), _edge("SECRET", "C", "rel"),
    ])
    assert "SECRET" not in {n.id for n in store.neighbors("A")}, "neighbors 泄漏了 RESTRICTED 节点"
    assert "SECRET" not in {n.id for n in store.traverse("A", max_depth=3)}, "traverse 泄漏了 RESTRICTED 节点"
    sub = store.subgraph(["A"], depth=3)
    assert "SECRET" not in {n.id for n in sub.nodes}, "subgraph.nodes 泄漏了 RESTRICTED 节点"
    assert all("SECRET" not in (e.src, e.dst) for e in sub.edges), "subgraph.edges 触及了 RESTRICTED 节点"


def _assert_traverse_deterministic(store) -> None:
    """判定核：同一遍历跑两次逐位一致（节点对象逐字段相等 ⇒ 含血缘 byte-identical）。"""
    store.upsert_nodes([_node(f"n{i}") for i in range(8)])
    store.upsert_edges([_edge("n0", f"n{i}", "rel") for i in range(1, 8)])
    first = store.traverse("n0", max_depth=2)
    second = store.traverse("n0", max_depth=2)
    assert first == second
    assert [n.id for n in first] == sorted(n.id for n in first)


# ---------------------------------------------------------------------------
# 领域断言：在每个注册实现（in_process + networkx）上各跑一遍
# ---------------------------------------------------------------------------

def test_provenance_round_trips(graph_store):
    """每个注册 GraphStore：遍历出口回传的节点/边都带齐血缘。"""
    _assert_provenance_complete(graph_store)


def test_restricted_isolation_holds(graph_store):
    """每个注册 GraphStore：RESTRICTED 节点绝不出现在任何遍历出口。"""
    _assert_restricted_isolated(graph_store)


def test_traverse_is_deterministic(graph_store):
    """每个注册 GraphStore：同一遍历重复执行 byte-identical。"""
    _assert_traverse_deterministic(graph_store)


# ===========================================================================
# 诚实反证：故意破不变量的 stub 喂进同一判定核必须 FAIL（证明断言非空泛）
# ===========================================================================

class _LeakyGraphStore(InProcessGraphStore):
    """反证 stub：neighbors/traverse/subgraph 【故意】不施 RESTRICTED 隔离，泄漏一切入库节点/边。"""

    def neighbors(self, node_id, **_kw):  # type: ignore[override]
        return [self._nodes[nid] for nid in sorted(self._nodes) if nid != node_id]

    def traverse(self, start, **_kw):  # type: ignore[override]
        return [self._nodes[nid] for nid in sorted(self._nodes) if nid != start]

    def subgraph(self, seeds, **_kw):  # type: ignore[override]
        return Subgraph(
            nodes=tuple(self._nodes[nid] for nid in sorted(self._nodes)),
            edges=tuple(self._edges[k] for k in sorted(self._edges)),
        )


class _LineageDroppingGraphStore(InProcessGraphStore):
    """反证 stub：遍历出口照常返回（隔离仍守），但把节点/边的血缘字段【故意】抹空。"""

    @staticmethod
    def _strip_node(node: GraphNode) -> GraphNode:
        md = dict(node.metadata)
        md["source_doc_id"] = ""
        md["source_locator"] = ""
        return GraphNode(id=node.id, type=node.type, label=node.label, metadata=md)

    @staticmethod
    def _strip_edge(edge: GraphEdge) -> GraphEdge:
        md = dict(edge.metadata)
        md["source_doc_id"] = ""
        md["source_locator"] = ""
        return GraphEdge(src=edge.src, dst=edge.dst, type=edge.type, metadata=md)

    def neighbors(self, node_id, **kw):  # type: ignore[override]
        return [self._strip_node(n) for n in super().neighbors(node_id, **kw)]

    def traverse(self, start, **kw):  # type: ignore[override]
        return [self._strip_node(n) for n in super().traverse(start, **kw)]

    def subgraph(self, seeds, **kw):  # type: ignore[override]
        sub = super().subgraph(seeds, **kw)
        return Subgraph(
            nodes=tuple(self._strip_node(n) for n in sub.nodes),
            edges=tuple(self._strip_edge(e) for e in sub.edges),
        )


def test_leaky_store_fails_isolation():
    """泄漏 RESTRICTED 的 stub 喂进同一隔离判定核必须 AssertionError——证明 isolation 断言非空泛。"""
    with pytest.raises(AssertionError):
        _assert_restricted_isolated(_LeakyGraphStore())


def test_lineage_dropping_store_fails_provenance():
    """丢血缘的 stub 喂进同一 provenance 判定核必须 AssertionError——证明 provenance 断言非空泛。"""
    with pytest.raises(AssertionError):
        _assert_provenance_complete(_LineageDroppingGraphStore())


# ===========================================================================
# Deliverable 3：make_graph_store("networkx" / "nx") 解析到 NetworkxGraphStore
# （in_process 工厂语义已在 tests/graph/test_graph_store.py 覆盖，此处不重复）
# ===========================================================================

def test_make_graph_store_networkx_aliases():
    """make_graph_store('networkx' / 'nx') -> NetworkxGraphStore 实例（networkx 缺则 skip）。"""
    pytest.importorskip("networkx", reason="networkx 未装（pip install ragspine[graph]）")
    from ragspine.graph.adapters.networkx_store import NetworkxGraphStore

    for spec in ("networkx", "nx", " NetworkX "):
        assert isinstance(make_graph_store(spec), NetworkxGraphStore)
