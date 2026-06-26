"""W7a 确定性结构关系图建图器 build_relation_graph 的单测（TDD 红 → 绿）。

钉死合约：从【受控维度】（synthetic DomainProfile + synthetic Fact + 鸭子类型 chunk）
确定性建图——实体/指标/外部主体/派生目标/doc 节点 + parent_of/derives/competes_with/
mentions 边，每个节点/边带非空血缘（source_doc_id + source_locator），RESTRICTED 来源
doc 节点继承存储层隔离绝不出域，建两次逐位一致。

红：relation.py 落地前 import 即 ModuleNotFoundError。
"""

import os
from dataclasses import dataclass

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.common.company_profile import DimensionSpec, DomainProfile
from ragspine.graph.relation import build_relation_graph
from ragspine.graph.store import InProcessGraphStore
from ragspine.storage.fact_store import Fact


# ---------------------------------------------------------------------------
# synthetic 物料（不复用任何内置 ACME 默认 —— 证明零硬编码公司）
# ---------------------------------------------------------------------------
@dataclass
class FakeChunk:
    """鸭子类型 chunk：仅暴露建图所读字段（doc_id/entity/topic/period/sensitivity/locator）。"""

    doc_id: str
    entity: str = ""
    topic: str = ""
    period: str = ""
    sensitivity: str = "INTERNAL"
    source_locator: str = ""


def _profile() -> DomainProfile:
    """合成领域 profile：home=GRP，子公司 SUBA/SUBB，地理派生 + 派生指标 + 竞品两家。"""
    return DomainProfile(
        home_company_name="Synth Group",
        home_entity_code="GRP",
        home_entity_synonyms={
            "grp": "GRP",
            "group": "GRP",
            "suba": "SUBA",
            "subb": "SUBB",
        },
        entity_geography={"GRP": "GLOBAL", "SUBA": "NORTH", "SUBB": "SOUTH"},
        external_entities={
            "rival": "RivalCorp",
            "rivalcorp": "RivalCorp",
            "compx": "CompX",
            "competitor x": "CompX",
        },
        home_entity_labels={"GRP": "Group", "SUBA": "Sub A", "SUBB": "Sub B"},
        dimensions=(
            DimensionSpec(
                "metric",
                label="Metric",
                synonyms={"rev": "REV", "new": "NEW"},
                labels={"REV": "Revenue", "NEW": "New Sales"},
                units={"REV": "USD_M", "NEW": "USD_M"},
            ),
            DimensionSpec(
                "geography",
                label="Geo",
                identity=False,
                derived_from="entity",
                derivation={"GRP": "GLOBAL", "SUBA": "NORTH", "SUBB": "SOUTH"},
            ),
            # 合成派生指标维：演示派生机制对【任何】声明维度通用（metric → 派生指标）。
            DimensionSpec(
                "core_metric",
                label="Core Metric",
                identity=False,
                derived_from="metric",
                derivation={"REV": "CORE_REV"},
            ),
        ),
    )


def _facts() -> list[Fact]:
    return [
        Fact("REV", "SUBA", "NORTH", "TOTAL", "FY", "2024", 10.0, "USD_M", "suba_fin.pdf", "suba_fin.pdf#p1"),
        Fact("REV", "SUBB", "SOUTH", "TOTAL", "FY", "2024", 20.0, "USD_M", "subb_fin.pdf", "subb_fin.pdf#p1"),
    ]


def _chunks() -> list[FakeChunk]:
    return [
        FakeChunk("news1.pdf", entity="SUBA", sensitivity="INTERNAL", source_locator="news1.pdf#c1"),
        # RESTRICTED chunk：其 doc 节点须继承存储层隔离，绝不出域。
        FakeChunk("secret.pdf", entity="SUBA", sensitivity="RESTRICTED", source_locator="secret.pdf#c1"),
    ]


# 全部【可见】节点 id（secret.pdf 是 RESTRICTED，不在此列）。
_VISIBLE_IDS = [
    "CORE_REV",
    "CompX",
    "GLOBAL",
    "GRP",
    "NEW",
    "NORTH",
    "REV",
    "RivalCorp",
    "SOUTH",
    "SUBA",
    "SUBB",
    "news1.pdf",
    "suba_fin.pdf",
    "subb_fin.pdf",
]


def _build():
    return build_relation_graph(_profile(), facts=_facts(), chunks=_chunks())


# ---------------------------------------------------------------------------
# 节点：实体 / 指标 / 外部主体 / 派生目标 / doc
# ---------------------------------------------------------------------------
def test_default_store_is_in_process():
    store = build_relation_graph(_profile())
    assert isinstance(store, InProcessGraphStore)


def test_entity_metric_external_geography_nodes_built():
    store = _build()
    assert store.get_node("GRP").type == "entity"
    assert store.get_node("GRP").label == "Group"
    assert store.get_node("REV").type == "metric"
    assert store.get_node("REV").label == "Revenue"
    assert store.get_node("RivalCorp").type == "external_entity"
    # 派生目标节点类型 = 派生维名（geography / core_metric），通用机制。
    assert store.get_node("GLOBAL").type == "geography"
    assert store.get_node("CORE_REV").type == "core_metric"


def test_doc_nodes_from_facts_and_chunks():
    store = _build()
    assert store.get_node("suba_fin.pdf").type == "doc"
    assert store.get_node("news1.pdf").type == "doc"


def test_uses_injected_store():
    g = InProcessGraphStore()
    returned = build_relation_graph(_profile(), facts=_facts(), store=g)
    assert returned is g


# ---------------------------------------------------------------------------
# 边：parent_of / derives / competes_with / mentions
# ---------------------------------------------------------------------------
def test_parent_of_edges_home_to_subsidiaries():
    store = _build()
    subs = {n.id for n in store.neighbors("GRP", edge_type="parent_of")}
    assert subs == {"SUBA", "SUBB"}


def test_derives_edges_entity_to_geography_and_metric_to_derived():
    store = _build()
    geo = {n.id for n in store.neighbors("GRP", edge_type="derives")}
    assert geo == {"GLOBAL"}
    derived_metric = {n.id for n in store.neighbors("REV", edge_type="derives")}
    assert derived_metric == {"CORE_REV"}


def test_competes_with_edges_home_to_externals():
    store = _build()
    peers = {n.id for n in store.neighbors("GRP", edge_type="competes_with")}
    assert peers == {"RivalCorp", "CompX"}


def test_mentions_edges_doc_to_entity_and_metric():
    store = _build()
    mentioned = {n.id for n in store.neighbors("suba_fin.pdf", edge_type="mentions")}
    assert mentioned == {"SUBA", "REV"}
    # chunk 提供 doc → entity 共现边。
    assert {n.id for n in store.neighbors("news1.pdf", edge_type="mentions")} == {"SUBA"}


# ---------------------------------------------------------------------------
# 不变量：provenance（每节点/边带非空 source_doc_id + source_locator）
# ---------------------------------------------------------------------------
def test_every_visible_node_and_edge_carries_nonempty_provenance():
    store = _build()
    sub = store.subgraph(_VISIBLE_IDS, depth=0)
    assert {n.id for n in sub.nodes} == set(_VISIBLE_IDS)
    for node in sub.nodes:
        assert node.metadata.get("source_doc_id")
        assert node.metadata.get("source_locator")
    assert sub.edges  # 确有边被建出
    for edge in sub.edges:
        assert edge.metadata.get("source_doc_id")
        assert edge.metadata.get("source_locator")


def test_mentions_edge_carries_fact_lineage():
    store = _build()
    sub = store.subgraph(["suba_fin.pdf"], depth=1)
    [e] = [e for e in sub.edges if e.type == "mentions" and e.dst == "SUBA"]
    assert e.metadata["source_doc_id"] == "suba_fin.pdf"
    assert e.metadata["source_locator"] == "suba_fin.pdf#p1"


# ---------------------------------------------------------------------------
# 不变量：RESTRICTED 隔离（继承存储层；RESTRICTED chunk 的 doc 节点绝不出域）
# ---------------------------------------------------------------------------
def test_restricted_chunk_doc_node_never_surfaces():
    store = _build()
    assert store.get_node("secret.pdf") is None  # 隔离：RESTRICTED 来源永不出域
    assert store.get_node("news1.pdf") is not None
    # doc 共现遍历（实体的 mentions 入邻）里也绝不出现 RESTRICTED doc。
    in_docs = {n.id for n in store.neighbors("SUBA", edge_type="mentions", direction="in")}
    assert "secret.pdf" not in in_docs
    assert {"news1.pdf", "suba_fin.pdf"} <= in_docs


# ---------------------------------------------------------------------------
# 不变量：确定性（建两次逐位一致）
# ---------------------------------------------------------------------------
def test_build_is_deterministic_byte_identical():
    first = _build().subgraph(_VISIBLE_IDS, depth=0)
    second = _build().subgraph(_VISIBLE_IDS, depth=0)
    assert first.nodes == second.nodes
    assert first.edges == second.edges
