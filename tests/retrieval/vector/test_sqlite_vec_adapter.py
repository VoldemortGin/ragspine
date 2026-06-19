"""sqlite-vec 适配器的专属测试（conformance 之外：持久化落盘、延迟 import 友好报错、工厂）。

行为合约/不变量由 tests/conformance 参数化覆盖（InProcessVectorStore 与 SqliteVecVectorStore
共用同一套）；这里只测 :memory: 覆盖不到的 sqlite-vec 特有面。
"""

import os
import sys

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

pytest.importorskip("sqlite_vec", reason="sqlite-vec 未装（pip install ragspine[vector]）")

from ragspine.retrieval.vector.adapters.sqlite_vec import SqliteVecVectorStore
from ragspine.retrieval.vector.store import VectorRecord, VectorStore, make_vector_store


def _rec(rid: str, vec, **md) -> VectorRecord:
    base = dict(doc_id=rid.split("#")[0], source_locator=f"{rid}!para1", topic="FIN")
    base.update({k: str(v) for k, v in md.items()})
    return VectorRecord(id=rid, vector=tuple(float(x) for x in vec), metadata=base)


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
