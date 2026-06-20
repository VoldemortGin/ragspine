"""sqlite-vec 适配器的专属测试（conformance 之外：持久化落盘、延迟 import 友好报错、工厂）。

行为合约/不变量由 tests/conformance 参数化覆盖（InProcessVectorStore 与 SqliteVecVectorStore
共用同一套）；这里只测 :memory: 覆盖不到的 sqlite-vec 特有面。
"""

import math
import os
import sys

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

pytest.importorskip("sqlite_vec", reason="sqlite-vec 未装（pip install ragspine[vector]）")

from ragspine.retrieval.vector.adapters.sqlite_vec import SqliteVecVectorStore
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
    """n 条角度清晰可分的单位向量（cosine 到 [1,0,0] 严格单调递减，top-k 无歧义）。"""
    return [
        _rec(f"r{i:03d}#0", [math.cos((i + 1) * 0.03), math.sin((i + 1) * 0.03), 0.0])
        for i in range(n)
    ]


def test_satisfies_protocol():
    """结构上满足 VectorStore 协议（runtime_checkable）。"""
    assert isinstance(SqliteVecVectorStore(), VectorStore)


def test_persistence_survives_reopen(tmp_path):
    """落盘后重开：向量与 metadata 存活、维度恢复、查询照常（持久化即本适配器的核心增量）。"""
    db = str(tmp_path / "vec.db")
    store = SqliteVecVectorStore(db)
    store.upsert([_rec("a#0", [1.0, 0.0, 0.0]), _rec("b#0", [0.0, 1.0, 0.0])])
    assert store.count() == 2
    store.close()

    reopened = SqliteVecVectorStore(db)
    assert reopened.count() == 2  # 跨进程/重开存活
    hits = reopened.query([1.0, 0.0, 0.0], k=5)
    assert [h.id for h in hits] == ["a#0", "b#0"]
    assert hits[0].score == pytest.approx(1.0, abs=1e-9)
    assert hits[0].metadata["source_locator"] == "a#0!para1"  # 血缘存活
    # 重开后维度已恢复：维度不符的查询仍抛错。
    with pytest.raises(ValueError):
        reopened.query([1.0, 0.0], k=5)
    reopened.close()


def test_reopen_empty_table_restores_dim_from_schema(tmp_path):
    """空表落盘重开：从 vec0 schema 的 float[N] 恢复维度（无数据可探时）。"""
    db = str(tmp_path / "vec.db")
    store = SqliteVecVectorStore(db)
    store.upsert([_rec("a#0", [1.0, 2.0, 3.0])])
    store.delete(where={"doc_id": "a"})  # 表在、数据空
    assert store.count() == 0
    store.close()

    reopened = SqliteVecVectorStore(db)
    with pytest.raises(ValueError):  # 维度从 schema 恢复 -> 2 维查询不匹配 3 维
        reopened.query([1.0, 2.0], k=5)
    reopened.close()


def test_factory_resolves_sqlite_vec(tmp_path):
    """make_vector_store('sqlite_vec' / 'sqlite-vec') -> SqliteVecVectorStore（db_path 经 kwargs 透传）。"""
    s1 = make_vector_store("sqlite_vec")
    assert isinstance(s1, SqliteVecVectorStore)
    s2 = make_vector_store("sqlite-vec", db_path=str(tmp_path / "x.db"))
    assert isinstance(s2, SqliteVecVectorStore)
    assert s2.db_path == str(tmp_path / "x.db")


def test_missing_sdk_raises_friendly_error(monkeypatch):
    """未装 sqlite-vec：__init__ 抛友好 ImportError（指向 [vector] extra），而非裸 ModuleNotFoundError。"""
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)  # 模拟未安装
    with pytest.raises(ImportError, match=r"\[vector\]"):
        SqliteVecVectorStore()


# ===========================================================================
# Native ANN/KNN：vec0 KNN MATCH 收窄候选池 + 精确重排（大库仍回精确 top-k）
# ===========================================================================

def test_native_knn_pool_narrows_yet_returns_exact_topk():
    """大库（count > pool_ceiling）触发 vec0 KNN 收窄候选池，精确重排后 top-k 与全量覆盖逐位一致。

    pool_ceiling 小于库 -> pool < count -> 走 native KNN MATCH 收窄（而非覆盖全部行）；其结果须与
    同实现、pool_ceiling 够大（候选池覆盖全部行 = 暴力扫）的兄弟实例【逐位一致】，证明 KNN 收窄
    无损召回真·top-k。同时与 exact 默认实现 InProcessVectorStore 的 id 排序一致（分值因 float32 近似）。
    """
    records = _separated_records(40)

    narrow = SqliteVecVectorStore(":memory:", pool_ceiling=8)  # pool=8 < 40 -> 触发 KNN 收窄
    full = SqliteVecVectorStore(":memory:")  # pool_ceiling 默认 4096 -> 覆盖全部行 = 暴力扫
    reference = InProcessVectorStore()
    for store in (narrow, full, reference):
        store.upsert(records)
    assert narrow.count() == 40  # 库远大于 pool（8），确认收窄路径被触发

    q = [1.0, 0.0, 0.0]
    narrow_hits = narrow.query(q, k=5)
    full_hits = full.query(q, k=5)
    # KNN 收窄 == 全量覆盖，逐位一致（同 float32 实现）：证明候选池无损覆盖真·top-k。
    assert [(h.id, h.score) for h in narrow_hits] == [(h.id, h.score) for h in full_hits]
    # 与暴力扫 exact 默认实现的 id 排序一致（分值因 float32 vs float64 仅近似）。
    assert [h.id for h in narrow_hits] == [h.id for h in reference.query(q, k=5)]
    narrow.close()
    full.close()


def test_native_knn_respects_where_isolation_under_narrowing():
    """KNN 收窄下 where 过滤仍正确、隔离不漏：RESTRICTED 最近邻被精确重排排除，绝不出现在结果里。"""
    records = _separated_records(40)
    # 把与查询最近的那条标成 RESTRICTED（最近邻，最该被过滤挡住的诚实反例）。
    records[0] = _rec("r000#0", [1.0, 0.0, 0.0], sensitivity="RESTRICTED")
    store = SqliteVecVectorStore(":memory:", pool_ceiling=8)
    store.upsert(records)
    hits = store.query([1.0, 0.0, 0.0], k=5, where={"sensitivity": "INTERNAL"})
    assert "r000#0" not in {h.id for h in hits}  # RESTRICTED 最近邻被挡
    assert all(h.metadata.get("sensitivity") == "INTERNAL" for h in hits)
    store.close()
