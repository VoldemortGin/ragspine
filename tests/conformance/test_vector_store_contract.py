"""VectorStore 合约测试（conformance·TDD 红色阶段，见 src/ragspine/retrieval/docs/vector-store.md）。

参数化到 conftest.VECTOR_STORE_FACTORIES 的每个实现——内存默认实现现在，
Qdrant/pgvector/FAISS 适配器将来——共用这一套行为合约。只验证外部可观测行为，
不绑定任何内部实现（cosine 怎么算、索引怎么存均不约束）。

覆盖（见 PRD「Contract」节）：
    upsert 计数/替换/空输入/维度校验、query 排序/分值/k 上限/默认 k/tie-break、
    where 过滤下推（AND·缺键排除·最近邻也排除·无命中空表）、delete/再入幂等、
    count、零向量不崩、维度不匹配抛错、metadata 血缘回传。

红色预期：ragspine.retrieval.vector.store 尚未实现，conftest import 即 FAIL，
本夹整体 ERROR，直至 VectorStore + InProcessVectorStore 落地转绿。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

# 对 store 的 import 一律延迟到用到它的函数体内（见 conftest「红色策略」）：
# 让本模块在 VectorStore 落地前仍能干净收集，红色仅体现为逐条用例 ERROR，不中断整轮。


# ===========================================================================
# upsert：计数 / 替换 / 空输入
# ===========================================================================

def test_upsert_returns_count(vector_store, make_record):
    """upsert 返回写入条数。"""
    n = vector_store.upsert([
        make_record("a#0", [1.0, 0.0, 0.0]),
        make_record("b#0", [0.0, 1.0, 0.0]),
    ])
    assert n == 2
    assert vector_store.count() == 2


def test_upsert_empty_is_noop(vector_store):
    """空 upsert 返回 0、不报错、不改变库大小。"""
    assert vector_store.upsert([]) == 0
    assert vector_store.count() == 0


def test_upsert_same_id_replaces(vector_store, make_record):
    """同 id 再次 upsert 为替换（不产生重复），count 不变、查询取新向量。"""
    vector_store.upsert([make_record("a#0", [1.0, 0.0, 0.0])])
    vector_store.upsert([make_record("a#0", [0.0, 1.0, 0.0])])
    assert vector_store.count() == 1
    hits = vector_store.query([0.0, 1.0, 0.0], k=5)
    assert [h.id for h in hits] == ["a#0"]
    assert hits[0].score == pytest.approx(1.0, abs=1e-9)


# ===========================================================================
# query：基础 / 排序 / 分值 / k
# ===========================================================================

def test_query_empty_store_returns_empty(vector_store):
    """空库查询返回 []（无维度可校验，也不应抛错）。"""
    assert vector_store.query([1.0, 0.0, 0.0], k=5) == []


def test_query_returns_hit_type_and_id(vector_store, make_record):
    """命中为 VectorHit，且 id 回传无误。"""
    from ragspine.retrieval.vector.store import VectorHit

    vector_store.upsert([make_record("a#0", [1.0, 0.0, 0.0])])
    hits = vector_store.query([1.0, 0.0, 0.0], k=5)
    assert hits and isinstance(hits[0], VectorHit)
    assert hits[0].id == "a#0"


def test_query_ranks_by_descending_cosine(vector_store, make_record):
    """按 cosine 降序：近的在前。query=[1,0,0]，期望 near > mid > far。"""
    vector_store.upsert([
        make_record("near#0", [1.0, 0.0, 0.0]),   # cos 1.0
        make_record("mid#0", [1.0, 1.0, 0.0]),    # cos ~0.707
        make_record("far#0", [0.0, 1.0, 0.0]),    # cos 0.0
    ])
    hits = vector_store.query([1.0, 0.0, 0.0], k=5)
    assert [h.id for h in hits] == ["near#0", "mid#0", "far#0"]
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_identical_vector_scores_one(vector_store, make_record):
    """与查询完全相同的向量 cosine≈1 且居首。"""
    vector_store.upsert([make_record("a#0", [0.0, 3.0, 4.0])])
    hits = vector_store.query([0.0, 3.0, 4.0], k=1)
    assert hits[0].score == pytest.approx(1.0, abs=1e-9)


def test_orthogonal_vector_scores_zero(vector_store, make_record):
    """正交向量 cosine≈0。"""
    vector_store.upsert([make_record("a#0", [0.0, 1.0, 0.0])])
    hits = vector_store.query([1.0, 0.0, 0.0], k=1)
    assert hits[0].score == pytest.approx(0.0, abs=1e-9)


def test_k_caps_results(vector_store, make_record):
    """k 限制返回条数。"""
    vector_store.upsert([make_record(f"d{i}#0", [1.0, float(i), 0.0]) for i in range(5)])
    assert len(vector_store.query([1.0, 0.0, 0.0], k=2)) == 2


def test_k_larger_than_store_returns_all(vector_store, make_record):
    """k 超过库大小则全返回。"""
    vector_store.upsert([make_record(f"d{i}#0", [1.0, float(i), 0.0]) for i in range(3)])
    assert len(vector_store.query([1.0, 0.0, 0.0], k=10)) == 3


def test_default_k_is_fifty(vector_store, make_record):
    """默认 k=DEFAULT_QUERY_K(50)：60 条入库、不传 k 时返回 50。"""
    from ragspine.retrieval.vector.store import DEFAULT_QUERY_K

    assert DEFAULT_QUERY_K == 50
    vector_store.upsert([make_record(f"d{i:03d}#0", [1.0, 0.0, 0.0]) for i in range(60)])
    assert len(vector_store.query([1.0, 0.0, 0.0])) == 50


# ===========================================================================
# tie-break：同分按 id 升序，确定性
# ===========================================================================

def test_tie_break_id_ascending(vector_store, make_record):
    """同分（相同向量）按 id 升序破除平分，确定性。"""
    vector_store.upsert([
        make_record("c#0", [1.0, 0.0, 0.0]),
        make_record("a#0", [1.0, 0.0, 0.0]),
        make_record("b#0", [1.0, 0.0, 0.0]),
    ])
    hits = vector_store.query([1.0, 0.0, 0.0], k=3)
    assert [h.id for h in hits] == ["a#0", "b#0", "c#0"]


# ===========================================================================
# where：过滤下推（AND·缺键排除·最近邻也排除·无命中空表）
# ===========================================================================

def test_where_single_key_exact_match(vector_store, make_record):
    """where 单键精确匹配：只回该键命中的记录。"""
    vector_store.upsert([
        make_record("fin#0", [1.0, 0.0, 0.0], topic="FIN"),
        make_record("reg#0", [1.0, 0.0, 0.0], topic="REG"),
    ])
    hits = vector_store.query([1.0, 0.0, 0.0], k=5, where={"topic": "REG"})
    assert {h.id for h in hits} == {"reg#0"}


def test_where_multi_key_is_and(vector_store, make_record):
    """where 多键为 AND：topic=REG 且 entity=ACME_HK 只剩一条。"""
    vector_store.upsert([
        make_record("x#0", [1.0, 0.0, 0.0], topic="REG", entity="ACME_HK"),
        make_record("y#0", [1.0, 0.0, 0.0], topic="REG", entity="ACME_CN"),
        make_record("z#0", [1.0, 0.0, 0.0], topic="FIN", entity="ACME_HK"),
    ])
    hits = vector_store.query([1.0, 0.0, 0.0], k=5, where={"topic": "REG", "entity": "ACME_HK"})
    assert {h.id for h in hits} == {"x#0"}


def test_where_excludes_filtered_even_if_nearest(vector_store, make_record):
    """过滤下推核心：被 where 排除的记录即便是最近邻也不出现。

    near 与查询完全同向（cos 1.0）但 topic=REG 被排除；只回 topic=FIN 的 far。
    """
    vector_store.upsert([
        make_record("near#0", [1.0, 0.0, 0.0], topic="REG"),   # 最近邻，但被过滤
        make_record("far#0", [0.0, 1.0, 0.0], topic="FIN"),    # 远，但通过过滤
    ])
    hits = vector_store.query([1.0, 0.0, 0.0], k=5, where={"topic": "FIN"})
    assert [h.id for h in hits] == ["far#0"]
    assert "near#0" not in {h.id for h in hits}


def test_where_no_match_returns_empty(vector_store, make_record):
    """where 无任何命中 -> []。"""
    vector_store.upsert([make_record("a#0", [1.0, 0.0, 0.0], topic="FIN")])
    assert vector_store.query([1.0, 0.0, 0.0], k=5, where={"topic": "NOPE"}) == []


def test_where_absent_key_excludes_record(vector_store):
    """记录 metadata 缺少 where 指定的键 -> 该记录被排除（不视为通过）。"""
    from ragspine.retrieval.vector.store import VectorRecord

    vector_store.upsert([VectorRecord(id="a#0", vector=(1.0, 0.0, 0.0), metadata={})])
    assert vector_store.query([1.0, 0.0, 0.0], k=5, where={"topic": "FIN"}) == []


# ===========================================================================
# delete / 再入幂等 / count
# ===========================================================================

def test_delete_by_where_removes_and_counts(vector_store, make_record):
    """delete(where=) 删除命中记录并返回删除条数。"""
    vector_store.upsert([
        make_record("a#0", [1.0, 0.0, 0.0], doc_id="d1"),
        make_record("b#0", [0.0, 1.0, 0.0], doc_id="d1"),
        make_record("c#0", [0.0, 0.0, 1.0], doc_id="d2"),
    ])
    removed = vector_store.delete(where={"doc_id": "d1"})
    assert removed == 2
    assert vector_store.count() == 1
    assert {h.id for h in vector_store.query([1.0, 1.0, 1.0], k=5)} == {"c#0"}


def test_reingest_idempotent_via_delete_then_upsert(vector_store, make_record):
    """再入幂等：delete(doc_id) + upsert 后无重复、count 正确。"""
    vector_store.upsert([
        make_record("doc#0", [1.0, 0.0, 0.0], doc_id="doc"),
        make_record("doc#1", [0.0, 1.0, 0.0], doc_id="doc"),
    ])
    vector_store.delete(where={"doc_id": "doc"})
    vector_store.upsert([make_record("doc#0", [1.0, 0.0, 0.0], doc_id="doc")])
    assert vector_store.count() == 1
    ids = [h.id for h in vector_store.query([1.0, 0.0, 0.0], k=5)]
    assert ids == ["doc#0"]


def test_count_tracks_upsert_and_delete(vector_store, make_record):
    """count 反映 upsert 与 delete 的净效果。"""
    assert vector_store.count() == 0
    vector_store.upsert([make_record(f"d{i}#0", [1.0, float(i), 0.0]) for i in range(4)])
    assert vector_store.count() == 4
    vector_store.delete(where={"doc_id": "d0"})
    assert vector_store.count() == 3


# ===========================================================================
# 维度校验 / 零向量健壮性
# ===========================================================================

def test_mixed_dims_in_one_upsert_raises(vector_store, make_record):
    """同一 upsert 内向量维度不一致 -> ValueError。"""
    with pytest.raises(ValueError):
        vector_store.upsert([
            make_record("a#0", [1.0, 0.0, 0.0]),
            make_record("b#0", [1.0, 0.0]),
        ])


def test_inconsistent_dim_across_upserts_raises(vector_store, make_record):
    """跨 upsert 维度与库内既有不一致 -> ValueError。"""
    vector_store.upsert([make_record("a#0", [1.0, 0.0, 0.0])])
    with pytest.raises(ValueError):
        vector_store.upsert([make_record("b#0", [1.0, 0.0])])


def test_query_dim_mismatch_raises(vector_store, make_record):
    """查询向量维度与库内维度不一致 -> ValueError。"""
    vector_store.upsert([make_record("a#0", [1.0, 0.0, 0.0])])
    with pytest.raises(ValueError):
        vector_store.query([1.0, 0.0], k=5)


def test_zero_query_vector_scores_zero_no_crash(vector_store, make_record):
    """零查询向量：所有 cosine=0，不崩。"""
    vector_store.upsert([
        make_record("a#0", [1.0, 0.0, 0.0]),
        make_record("b#0", [0.0, 1.0, 0.0]),
    ])
    hits = vector_store.query([0.0, 0.0, 0.0], k=5)
    assert len(hits) == 2
    assert all(h.score == 0.0 for h in hits)


def test_zero_stored_vector_no_crash(vector_store, make_record):
    """库内零向量：对其 cosine=0，不崩，仍可被返回。"""
    vector_store.upsert([
        make_record("zero#0", [0.0, 0.0, 0.0]),
        make_record("a#0", [1.0, 0.0, 0.0]),
    ])
    hits = vector_store.query([1.0, 0.0, 0.0], k=5)
    by_id = {h.id: h.score for h in hits}
    assert by_id["zero#0"] == 0.0
    assert by_id["a#0"] == pytest.approx(1.0, abs=1e-9)


# ===========================================================================
# metadata 血缘回传
# ===========================================================================

def test_metadata_round_trips_on_hit(vector_store, make_record):
    """命中回传记录的 metadata（含 doc_id / source_locator 等血缘字段）。"""
    vector_store.upsert([make_record("a#0", [1.0, 0.0, 0.0])])
    [hit] = vector_store.query([1.0, 0.0, 0.0], k=1)
    assert hit.metadata["doc_id"] == "a"
    assert hit.metadata["source_locator"] == "a#0!para1"
    assert hit.metadata["topic"] == "FIN"
