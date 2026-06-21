"""VectorStore 不变量绑定测试（conformance·TDD 红色阶段）。

见 src/ragspine/retrieval/docs/vector-store.md「Invariant binding」与 docs/prd-breadth-via-adapters.md
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
# D · Determinism：可复现（按能力旗标分支强弱——exact 全量逐位、approximate 较弱保证）
#
# capability（来自 conftest.VECTOR_STORE_IMPLS 注册表）：
#   - exact       —— 保证逐位 byte-identical + id 升序破平分（全量强度，绝不松动）。
#   - approximate —— 生产保证为近似（HNSW 等）：只断言 PRD「Further notes」给出的较弱保证——
#                    同实例重复调用顺序稳定 + 对 exact 默认实现的 recall@k 下限；
#                    不把逐位一致 / id 升序破平分钉死（避免未来切原生近似 KNN 被合约误伤）。
# 注：provenance / isolation / where 过滤下推三项不变量对【所有】后端照样全量绑定（见上文），
# 这里分支的只是「确定性」这一项的强弱口径。
# ===========================================================================

def test_repeated_query_byte_identical(vector_store, vector_store_capability, make_record):
    """同库同查询两次：exact 断言命中 (id, score) 逐位一致；approximate 断言同实例顺序（id 序）稳定。"""
    vector_store.upsert([make_record(f"d{i}#0", [1.0, float(i) * 0.1, 0.0]) for i in range(8)])
    first = vector_store.query([1.0, 0.05, 0.0], k=8)
    second = vector_store.query([1.0, 0.05, 0.0], k=8)
    if vector_store_capability == "exact":
        assert [(h.id, h.score) for h in first] == [(h.id, h.score) for h in second]
    else:
        # approximate：同实例对相同查询的重复调用，结果【顺序】须稳定（不要求 score 逐位）。
        assert [h.id for h in first] == [h.id for h in second]


def test_two_independent_instances_agree(vector_store, vector_store_capability, make_record):
    """exact：两个独立同实现实例逐位一致（跨进程代理）；approximate：对 exact 默认实现的 recall@k 下限。"""
    if vector_store_capability == "exact":
        # 用 type(vector_store)() 造一个同实现的兄弟实例，随参数化夹具天然覆盖每个 exact 实现。
        other = type(vector_store)()
        records = [make_record(f"d{i}#0", [1.0, float(i) * 0.1, 0.0]) for i in range(6)]
        vector_store.upsert(records)
        other.upsert(records)
        ra = [(h.id, h.score) for h in vector_store.query([1.0, 0.05, 0.0], k=6)]
        rb = [(h.id, h.score) for h in other.query([1.0, 0.05, 0.0], k=6)]
        assert ra == rb
    else:
        # approximate：对【清晰可分】的向量，近似 top-k 须召回与 exact 默认实现相同的 id 集合
        # （recall@k 下限——这里 well-separated 故 recall@2 == 1.0）。
        from ragspine.retrieval.vector.store import InProcessVectorStore

        records = [
            make_record("east#0", [1.0, 0.0, 0.0]),
            make_record("north#0", [0.0, 1.0, 0.0]),
            make_record("up#0", [0.0, 0.0, 1.0]),
            make_record("nw#0", [0.0, 1.0, 1.0]),
        ]
        reference = InProcessVectorStore()
        reference.upsert(records)
        vector_store.upsert(records)
        q = [1.0, 0.2, 0.0]
        exact_ids = {h.id for h in reference.query(q, k=2)}
        approx_ids = {h.id for h in vector_store.query(q, k=2)}
        assert approx_ids == exact_ids


def test_tie_break_stable_across_runs(vector_store, vector_store_capability, make_record):
    """exact：同分按 id 升序且两次一致；approximate：两次顺序稳定 + 全量召回该平分集合（不强求升序）。"""
    vector_store.upsert([make_record(rid, [1.0, 0.0, 0.0]) for rid in ("e#0", "a#0", "d#0", "b#0")])
    run1 = [h.id for h in vector_store.query([1.0, 0.0, 0.0], k=4)]
    run2 = [h.id for h in vector_store.query([1.0, 0.0, 0.0], k=4)]
    if vector_store_capability == "exact":
        assert run1 == run2 == ["a#0", "b#0", "d#0", "e#0"]
    else:
        # approximate：顺序须跨调用稳定，且该平分集合须被全量召回（id 升序破平分非近似后端的保证）。
        assert run1 == run2
        assert set(run1) == {"a#0", "b#0", "d#0", "e#0"}
