"""W7a 多跳查询入口 GraphQuery 的单测（TDD 红 → 绿）。

钉死合约：子公司汇总（求和 + 每条贡献事实可引用）/ 同业对比（逐实体可引用）/ 派生追溯
（沿 derives 边确定性、带逐边血缘）；竞品主体被安全门拒答（绝不泄竞品数据）；RESTRICTED
来源 doc 节点继承存储层隔离绝不出域；同输入同输出。

红：query.py 落地前 import 即 ModuleNotFoundError。
"""

import os
from dataclasses import dataclass

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.common.company_profile import DimensionSpec, DomainProfile
from ragspine.graph.query import (
    ComparisonResult,
    DerivationTrace,
    GraphQuery,
    RollupResult,
)
from ragspine.graph.relation import build_relation_graph
from ragspine.storage.fact_store import Fact, FactStore


@dataclass
class FakeChunk:
    doc_id: str
    entity: str = ""
    topic: str = ""
    period: str = ""
    sensitivity: str = "INTERNAL"
    source_locator: str = ""


def _profile() -> DomainProfile:
    return DomainProfile(
        home_company_name="Synth Group",
        home_entity_code="GRP",
        home_entity_synonyms={"grp": "GRP", "suba": "SUBA", "subb": "SUBB"},
        entity_geography={"GRP": "GLOBAL", "SUBA": "NORTH", "SUBB": "SOUTH"},
        external_entities={
            "rival": "RivalCorp",
            "rivalcorp": "RivalCorp",
            "compx": "CompX",
        },
        home_entity_labels={"GRP": "Group", "SUBA": "Sub A", "SUBB": "Sub B"},
        dimensions=(
            DimensionSpec(
                "metric",
                label="Metric",
                synonyms={"rev": "REV"},
                labels={"REV": "Revenue"},
                units={"REV": "USD_M"},
            ),
            DimensionSpec(
                "geography",
                label="Geo",
                identity=False,
                derived_from="entity",
                derivation={"GRP": "GLOBAL", "SUBA": "NORTH", "SUBB": "SOUTH"},
            ),
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
        FakeChunk("news1.pdf", entity="SUBA", source_locator="news1.pdf#c1"),
        FakeChunk("secret.pdf", entity="SUBA", sensitivity="RESTRICTED", source_locator="secret.pdf#c1"),
    ]


@pytest.fixture()
def gq(tmp_path):
    fs = FactStore(str(tmp_path / "facts.db"))
    fs.init_schema()
    fs.upsert_facts(_facts())
    store = build_relation_graph(_profile(), facts=_facts(), chunks=_chunks())
    return GraphQuery(store, fs, profile=_profile())


# ---------------------------------------------------------------------------
# 子公司汇总：母实体 → 子公司事实求和 + 逐条可引用（flat top-k / 精确 SQL 做不到）
# ---------------------------------------------------------------------------
def test_subsidiary_rollup_sums_children_with_citations(gq):
    res = gq.subsidiary_rollup("GRP", "REV", "FY", "2024")
    assert isinstance(res, RollupResult)
    assert res.refused is False
    assert res.found is True
    assert res.total == 30.0  # SUBA(10) + SUBB(20)
    assert {f.entity for f in res.facts} == {"SUBA", "SUBB"}
    # 每条贡献事实带血缘，答案可引用。
    for f in res.facts:
        assert f.source_doc_id
        assert f.source_locator


def test_subsidiary_rollup_not_found_when_no_subsidiary_facts(gq):
    res = gq.subsidiary_rollup("GRP", "REV", "FY", "2099")
    assert res.refused is False
    assert res.found is False
    assert res.total is None
    assert res.facts == ()


# ---------------------------------------------------------------------------
# 同业对比：逐实体可引用事实（兄弟 home 实体并排）
# ---------------------------------------------------------------------------
def test_peer_comparison_per_entity_cited_facts(gq):
    res = gq.peer_comparison(["SUBA", "SUBB"], "REV", "FY", "2024")
    assert isinstance(res, ComparisonResult)
    assert res.refused is False
    assert [r.entity for r in res.rows] == ["SUBA", "SUBB"]
    assert [r.fact.value for r in res.rows] == [10.0, 20.0]
    assert res.rows[0].fact.source_doc_id == "suba_fin.pdf"


# ---------------------------------------------------------------------------
# 派生追溯：沿 derives 边确定性走 + 逐边血缘（entity→geography；metric→派生指标）
# ---------------------------------------------------------------------------
def test_derivation_trace_entity_to_geography(gq):
    trace = gq.derivation_trace("GRP")
    assert isinstance(trace, DerivationTrace)
    assert trace.start == "GRP"
    assert "GLOBAL" in {n.id for n in trace.nodes}
    [edge] = [e for e in trace.edges if e.src == "GRP" and e.dst == "GLOBAL"]
    assert edge.type == "derives"
    assert edge.metadata.get("source_doc_id")
    assert edge.metadata.get("source_locator")


def test_derivation_trace_metric_to_derived_metric(gq):
    trace = gq.derivation_trace("REV")
    assert "CORE_REV" in {n.id for n in trace.nodes}


# ---------------------------------------------------------------------------
# 不变量：竞品/越权拒答（继承安全门；绝不返回竞品数据）
# ---------------------------------------------------------------------------
def test_subsidiary_rollup_refuses_competitor(gq):
    res = gq.subsidiary_rollup("rival", "REV", "FY", "2024")
    assert res.refused is True
    assert res.found is False
    assert res.total is None
    assert res.facts == ()
    assert res.refusal_message


def test_peer_comparison_refuses_when_any_competitor(gq):
    res = gq.peer_comparison(["SUBA", "rival"], "REV", "FY", "2024")
    assert res.refused is True
    assert res.rows == ()
    assert res.refusal_message


# ---------------------------------------------------------------------------
# 不变量：RESTRICTED 隔离（继承存储层；派生/共现遍历绝不触及 RESTRICTED doc）
# ---------------------------------------------------------------------------
def test_restricted_doc_never_surfaces_in_query(gq):
    res = gq.peer_comparison(["SUBA"], "REV", "FY", "2024")
    assert res.refused is False
    assert res.rows[0].fact.value == 10.0
    # 存储层隔离：RESTRICTED doc 节点不可读、不作共现跳板。
    assert gq._store.get_node("secret.pdf") is None


# ---------------------------------------------------------------------------
# 不变量：确定性（同输入同输出 —— 两次调用逐位一致）
# ---------------------------------------------------------------------------
def test_queries_are_deterministic(gq):
    r1 = gq.subsidiary_rollup("GRP", "REV", "FY", "2024")
    r2 = gq.subsidiary_rollup("GRP", "REV", "FY", "2024")
    assert r1 == r2
    t1 = gq.derivation_trace("GRP")
    t2 = gq.derivation_trace("GRP")
    assert t1.edges == t2.edges
    assert t1.nodes == t2.nodes
