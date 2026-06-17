"""VectorStore 不变量绑定测试（conformance·TDD 红色阶段）。

落地 docs/prd-vector-store-seam.md「Invariant binding」与 docs/prd-breadth-via-adapters.md
「Invariant-binding conformance kit」：把 RAGSpine 的代码级不变量【绑死在 VectorStore
这条缝上】，对【每一个注册实现】参数化断言。任何 adapter（含第三方）只要登记进
conftest.VECTOR_STORE_FACTORIES 就必须通过——破坏脊柱的实现直接 CI 红，而非生产事故。

绑定三项：
    P · Provenance —— id 与 doc_id/source_locator 经 upsert→query 全程不丢、不臆造。
    I · Isolation  —— where 过滤下推是「在存储层强制敏感度隔离」的机制：RESTRICTED 记录
        即便是最近邻也被 sensitivity 过滤排除；同时【诚实反证】不带过滤时存储层不自动
        剔除 RESTRICTED（权威剔除仍在 link/rerank 两出口），使隔离用例非空泛。
    D · Determinism —— 同输入跨调用 / 跨独立实例结果逐位一致，平分序稳定。

红色预期：ragspine.retrieval.vector.store 未落地，conftest import 即 FAIL，本夹整体
ERROR，直至实现转绿。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)


# ===========================================================================
# P · Provenance：血缘保真
# ===========================================================================

def test_hit_id_non_empty_and_from_upserted_set(vector_store, make_record):
    """每条命中的 id 非空且确为入库过的 id（不臆造、不丢失）。"""
    ids = {"a#0", "b#0", "c#0"}
    vector_store.upsert([make_record(i, [1.0, float(n), 0.0]) for n, i in enumerate(sorted(ids))])
    hits = vector_store.query([1.0, 0.0, 0.0], k=10)
    assert {h.id for h in hits} == ids
    assert all(h.id for h in hits)  # 无空 id


def test_provenance_doc_id_and_locator_round_trip(vector_store, make_record):
    """provenance 锚点 doc_id + source_locator 经 upsert→query 原样回传。"""
    vector_store.upsert([
        make_record("HK_FIN.pptx#3", [1.0, 0.0, 0.0],
                    doc_id="HK_FIN.pptx", source_locator="HK_FIN.pptx!slide3#para2"),
    ])
    [hit] = vector_store.query([1.0, 0.0, 0.0], k=1)
    assert hit.metadata["doc_id"] == "HK_FIN.pptx"
    assert hit.metadata["source_locator"] == "HK_FIN.pptx!slide3#para2"


# ===========================================================================
# I · Isolation：where 过滤下推作为敏感度隔离机制
# ===========================================================================

def test_restricted_excluded_by_filter_even_as_nearest(vector_store, make_record):
    """RESTRICTED 记录即便是【完全同向的最近邻】，也被 sensitivity 过滤排除。

    这把 RESTRICTED 隔离能力下推到存储层：secret 与查询同向（cos 1.0），但
    where={'sensitivity':'INTERNAL'} 必须把它挡在结果外，只回 INTERNAL 的 pub。
    """
    vector_store.upsert([
        make_record("secret#0", [1.0, 0.0, 0.0], sensitivity="RESTRICTED"),  # 最近邻
        make_record("pub#0", [0.0, 1.0, 0.0], sensitivity="INTERNAL"),       # 远
    ])
    hits = vector_store.query([1.0, 0.0, 0.0], k=5, where={"sensitivity": "INTERNAL"})
    out_ids = {h.id for h in hits}
    assert "secret#0" not in out_ids
    assert "pub#0" in out_ids


def test_restricted_present_without_filter(vector_store, make_record):
    """诚实反证：不带过滤时存储层【不】自动剔除 RESTRICTED（权威剔除在 link/rerank 出口）。

    确保上一条隔离用例验证的是 where 下推机制本身，而非存储层的隐式行为。
    """
    vector_store.upsert([make_record("secret#0", [1.0, 0.0, 0.0], sensitivity="RESTRICTED")])
    hits = vector_store.query([1.0, 0.0, 0.0], k=5)
    assert {h.id for h in hits} == {"secret#0"}


# ===========================================================================
# D · Determinism：可复现
# ===========================================================================

def test_repeated_query_byte_identical(vector_store, make_record):
    """同库同查询两次：命中 (id, score) 序列逐位一致。"""
    vector_store.upsert([make_record(f"d{i}#0", [1.0, float(i) * 0.1, 0.0]) for i in range(8)])
    first = [(h.id, h.score) for h in vector_store.query([1.0, 0.05, 0.0], k=8)]
    second = [(h.id, h.score) for h in vector_store.query([1.0, 0.05, 0.0], k=8)]
    assert first == second


def test_two_independent_instances_agree(vector_store, make_record):
    """两个独立新建实例（同一实现）灌入相同数据、同查询 -> 结果逐位一致（跨进程代理）。

    用 type(vector_store)() 造一个同实现的兄弟实例，随参数化夹具天然覆盖每个注册实现。
    """
    other = type(vector_store)()
    records = [make_record(f"d{i}#0", [1.0, float(i) * 0.1, 0.0]) for i in range(6)]
    vector_store.upsert(records)
    other.upsert(records)
    ra = [(h.id, h.score) for h in vector_store.query([1.0, 0.05, 0.0], k=6)]
    rb = [(h.id, h.score) for h in other.query([1.0, 0.05, 0.0], k=6)]
    assert ra == rb


def test_tie_break_stable_across_runs(vector_store, make_record):
    """同分序稳定：相同向量多条，两次查询的 id 顺序一致且为升序。"""
    vector_store.upsert([make_record(rid, [1.0, 0.0, 0.0]) for rid in ("e#0", "a#0", "d#0", "b#0")])
    run1 = [h.id for h in vector_store.query([1.0, 0.0, 0.0], k=4)]
    run2 = [h.id for h in vector_store.query([1.0, 0.0, 0.0], k=4)]
    assert run1 == run2 == ["a#0", "b#0", "d#0", "e#0"]
