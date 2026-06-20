"""Qdrant 适配器专属测试（conformance 之外：local 模式、落盘命名 collection 跨进程持久、工厂、延迟 import）。

行为合约/不变量由 tests/conformance 参数化覆盖（qdrant 走 local 模式、纯进程内、无需 env 门）；这里测
Qdrant 特有面：协议满足、工厂解析、未装驱动的友好报错、path= 落盘命名 collection 重开后数据 + 血缘存活。
需 qdrant-client（[vector]）。
"""

import math
import os
import sys

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

pytest.importorskip("qdrant_client", reason="qdrant-client 未装（pip install ragspine[vector]）")

from ragspine.retrieval.vector.adapters.qdrant import QdrantVectorStore, _point_id
from ragspine.retrieval.vector.store import (
    InProcessVectorStore,
    VectorRecord,
    VectorStore,
    make_vector_store,
)


def _rec(rid: str, vec, **md) -> VectorRecord:
    base = dict(doc_id=rid.split("#")[0], source_locator=f"{rid}!para1", topic="FIN")
    base.update({k: str(v) for k, v in md.items()})
    return VectorRecord(id=rid, vector=tuple(float(x) for x in vec), metadata=base)


def _separated_records(n: int) -> list[VectorRecord]:
    """n 条角度清晰可分的单位向量（Distance.DOT 下点积≡cosine，收窄按 cosine 对齐，top-k 无歧义）。"""
    return [
        _rec(f"r{i:03d}#0", [math.cos((i + 1) * 0.03), math.sin((i + 1) * 0.03), 0.0])
        for i in range(n)
    ]


def test_missing_driver_raises_friendly(monkeypatch):
    """未装 qdrant-client：__init__ 抛友好 ImportError（指向 [vector]），而非裸 ModuleNotFoundError。"""
    monkeypatch.setitem(sys.modules, "qdrant_client", None)
    with pytest.raises(ImportError, match=r"\[vector\]"):
        QdrantVectorStore()


def test_satisfies_protocol():
    """结构上满足 VectorStore 协议（runtime_checkable）。"""
    store = QdrantVectorStore()
    try:
        assert isinstance(store, VectorStore)
    finally:
        store.close()


def test_point_id_is_deterministic():
    """字符串 chunk_id -> 确定性 UUID5 point id（同 id 恒同，不同 id 相异）。"""
    assert _point_id("a#0") == _point_id("a#0")
    assert _point_id("a#0") != _point_id("b#0")


def test_memory_instances_isolated():
    """两个 :memory: 实例各自独立、互不可见（conformance 隔离的基础）。"""
    a = QdrantVectorStore()
    b = QdrantVectorStore()
    try:
        a.upsert([_rec("x#0", [1.0, 0.0, 0.0])])
        assert a.count() == 1
        assert b.count() == 0  # b 的独立 :memory: 看不到 a 的写入
    finally:
        a.close()
        b.close()


def test_named_collection_persists_across_reopen(tmp_path):
    """落盘命名 collection：写入后关闭重开（新 client、同 path），数据 + 血缘存活、维度从 VectorParams 恢复。"""
    path = str(tmp_path / "qdrant_store")
    collection = "rs_test"

    s1 = QdrantVectorStore(path=path, collection=collection)
    s1.upsert([_rec("a#0", [1.0, 0.0, 0.0]), _rec("b#0", [0.0, 1.0, 0.0])])
    assert s1.count() == 2
    s1.close()  # 释放 path 锁，方可重开

    s2 = QdrantVectorStore(path=path, collection=collection)
    try:
        assert s2.count() == 2  # 跨进程持久
        hits = s2.query([1.0, 0.0, 0.0], k=5)
        assert [h.id for h in hits] == ["a#0", "b#0"]
        assert hits[0].score == pytest.approx(1.0, abs=1e-6)
        assert hits[0].metadata["source_locator"] == "a#0!para1"  # 血缘存活
        with pytest.raises(ValueError):  # 维度从 VectorParams.size 恢复 -> 2 维查询不匹配 3 维
            s2.query([1.0, 0.0], k=5)
    finally:
        s2.close()


def test_factory_resolves_qdrant():
    """make_vector_store('qdrant') -> QdrantVectorStore（kwargs 透传，如 path / collection）。"""
    s = make_vector_store("qdrant")
    try:
        assert isinstance(s, QdrantVectorStore)
    finally:
        s.close()


# ===========================================================================
# Native ANN/KNN：native HNSW search 收窄候选池 + 精确重排（大库满足 recall@k 下限）
# ===========================================================================

def test_native_search_pool_narrows_with_recall_floor():
    """大库（count > pool_ceiling）触发 native HNSW search 收窄候选池（limit=pool），精确重排后满足 recall@k 下限。

    pool_ceiling 小于库 -> pool < count -> 走 native query_points(limit=pool) 收窄（而非全量 scroll）；
    对清晰可分的单位向量（DOT≡cosine），收窄按 cosine 对齐，精确重排后召回与 exact 默认实现
    InProcessVectorStore 相同的 top-k id 集合（recall@k 下限——这里 well-separated 故 recall == 1.0）。
    Qdrant 仍是 approximate 后端：这里断言的是【下限】而非逐位一致。
    """
    records = _separated_records(40)
    store = QdrantVectorStore(pool_ceiling=8)  # pool=8 < 40 -> 触发 native search 收窄
    reference = InProcessVectorStore()
    try:
        store.upsert(records)
        reference.upsert(records)
        assert store.count() == 40  # 库远大于 pool（8），确认收窄路径被触发

        q = [1.0, 0.05, 0.0]
        approx_ids = {h.id for h in store.query(q, k=5)}
        exact_ids = {h.id for h in reference.query(q, k=5)}
        recall = len(approx_ids & exact_ids) / len(exact_ids)
        assert recall >= 0.8  # recall@5 下限（well-separated 单位向量下实测为 1.0）
    finally:
        store.close()


def test_native_search_respects_where_isolation_under_narrowing():
    """native search 收窄下 where 下推为 payload 过滤、隔离不漏：RESTRICTED 最近邻被排除，绝不出现在结果里。"""
    records = _separated_records(40)
    records[0] = _rec("r000#0", [1.0, 0.0, 0.0], sensitivity="RESTRICTED")  # 最近邻，应被挡
    store = QdrantVectorStore(pool_ceiling=8)
    try:
        store.upsert(records)
        hits = store.query([1.0, 0.0, 0.0], k=5, where={"sensitivity": "INTERNAL"})
        assert "r000#0" not in {h.id for h in hits}  # RESTRICTED 最近邻被挡
        assert all(h.metadata.get("sensitivity") == "INTERNAL" for h in hits)
    finally:
        store.close()
