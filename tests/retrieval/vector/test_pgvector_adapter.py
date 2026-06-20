"""pgvector 适配器专属测试（conformance 之外：命名表跨连接持久化、TEMP 隔离、工厂、延迟 import）。

行为合约/不变量由 tests/conformance 参数化覆盖（需 RAGSPINE_PG_URL）；这里测连接/表生命周期等
pgvector 特有面。需 pg8000（[vector]）；跨连接用例另需 RAGSPINE_PG_URL 指向带 pgvector 的 Postgres。
"""

import math
import os
import sys
import uuid

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

pytest.importorskip("pg8000", reason="pg8000 未装（pip install ragspine[vector]）")

from ragspine.retrieval.vector.adapters.pgvector import PG_URL_ENV, PgVectorVectorStore
from ragspine.retrieval.vector.store import (
    InProcessVectorStore,
    VectorRecord,
    VectorStore,
    make_vector_store,
)


@pytest.fixture
def pg_url():
    url = os.environ.get(PG_URL_ENV)
    if not url:
        pytest.skip(f"{PG_URL_ENV} 未设（需带 pgvector 扩展的 Postgres）")
    return url


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


def test_missing_url_raises(monkeypatch):
    """未传 dsn 且未设 RAGSPINE_PG_URL -> ValueError（指向连接配置），不裸连默认。"""
    monkeypatch.delenv(PG_URL_ENV, raising=False)
    with pytest.raises(ValueError, match=PG_URL_ENV):
        PgVectorVectorStore()


def test_missing_driver_raises_friendly(monkeypatch):
    """未装 pg8000：__init__ 抛友好 ImportError（指向 [vector]），而非裸 ModuleNotFoundError。"""
    monkeypatch.setitem(sys.modules, "pg8000", None)
    monkeypatch.setitem(sys.modules, "pg8000.native", None)
    with pytest.raises(ImportError, match=r"\[vector\]"):
        PgVectorVectorStore(dsn="postgresql://x@localhost:1/x")


def test_satisfies_protocol(pg_url):
    """结构上满足 VectorStore 协议（runtime_checkable）。"""
    store = PgVectorVectorStore(dsn=pg_url)
    try:
        assert isinstance(store, VectorStore)
    finally:
        store.close()


def test_temp_table_instances_isolated(pg_url):
    """两个无表名实例各建会话级 TEMP 表，互不可见（conformance 隔离的基础）。"""
    a = PgVectorVectorStore(dsn=pg_url)
    b = PgVectorVectorStore(dsn=pg_url)
    try:
        a.upsert([_rec("x#0", [1.0, 0.0, 0.0])])
        assert a.count() == 1
        assert b.count() == 0  # b 的 TEMP 表看不到 a 的写入
    finally:
        a.close()
        b.close()


def test_named_table_persists_across_reopen(pg_url):
    """命名表：落库后断连重开（新连接），数据与血缘存活、维度从 atttypmod 恢复。"""
    table = "rs_test_" + uuid.uuid4().hex[:12]
    try:
        s1 = PgVectorVectorStore(dsn=pg_url, table=table)
        s1.upsert([_rec("a#0", [1.0, 0.0, 0.0]), _rec("b#0", [0.0, 1.0, 0.0])])
        assert s1.count() == 2
        s1.close()

        s2 = PgVectorVectorStore(dsn=pg_url, table=table)
        try:
            assert s2.count() == 2  # 跨连接持久
            hits = s2.query([1.0, 0.0, 0.0], k=5)
            assert [h.id for h in hits] == ["a#0", "b#0"]
            assert hits[0].score == pytest.approx(1.0, abs=1e-9)
            assert hits[0].metadata["source_locator"] == "a#0!para1"  # 血缘存活
            with pytest.raises(ValueError):  # 维度从 vector(N) 列恢复 -> 2 维查询不匹配 3 维
                s2.query([1.0, 0.0], k=5)
        finally:
            s2.close()
    finally:
        # 兜底清理命名表（即便上面任一步失败也不留孤儿表）：重开一个 store 借连接 DROP。
        cleanup = PgVectorVectorStore(dsn=pg_url, table=table)
        cleanup._conn.run(f"DROP TABLE IF EXISTS {table}")
        cleanup.close()


def test_factory_resolves_pgvector(pg_url):
    """make_vector_store('pgvector' / 'pg_vector') -> PgVectorVectorStore（dsn 经 kwargs 透传）。"""
    s = make_vector_store("pgvector", dsn=pg_url)
    try:
        assert isinstance(s, PgVectorVectorStore)
    finally:
        s.close()


# ===========================================================================
# Native ANN/KNN：HNSW 索引 `ORDER BY <=> LIMIT pool` 收窄 + 精确重排（大库仍回精确 top-k）
# ===========================================================================

def test_native_hnsw_pool_narrows_yet_returns_exact_topk(pg_url):
    """大库（count > pool_ceiling）触发 HNSW `ORDER BY <=> LIMIT pool` 收窄，精确重排后 top-k 与全量覆盖逐位一致。

    pool_ceiling 小于库 -> pool < count -> 走 HNSW 索引收窄（而非全表扫覆盖全部行）；其结果须与
    同实现、pool_ceiling 够大（全表扫 = 暴力扫）的兄弟实例【逐位一致】，证明 HNSW 收窄无损召回真·top-k。
    同时与 exact 默认实现 InProcessVectorStore 的 id 排序一致（分值因 float4 近似）。
    """
    records = _separated_records(40)
    narrow = PgVectorVectorStore(dsn=pg_url, pool_ceiling=8)  # pool=8 < 40 -> 触发 HNSW 收窄
    full = PgVectorVectorStore(dsn=pg_url)  # pool_ceiling 默认 4096 -> 全表扫 = 暴力扫
    reference = InProcessVectorStore()
    try:
        narrow.upsert(records)
        full.upsert(records)
        reference.upsert(records)
        assert narrow.count() == 40  # 库远大于 pool（8），确认收窄路径被触发

        q = [1.0, 0.0, 0.0]
        narrow_hits = narrow.query(q, k=5)
        full_hits = full.query(q, k=5)
        # HNSW 收窄 == 全表扫，逐位一致（同 float4 实现）：证明候选池无损覆盖真·top-k。
        assert [(h.id, h.score) for h in narrow_hits] == [(h.id, h.score) for h in full_hits]
        # 与暴力扫 exact 默认实现的 id 排序一致（分值因 float4 vs float64 仅近似）。
        assert [h.id for h in narrow_hits] == [h.id for h in reference.query(q, k=5)]
    finally:
        narrow.close()
        full.close()


def test_native_hnsw_respects_where_isolation_under_narrowing(pg_url):
    """HNSW 收窄下 where 仍【下推到 SQL】、隔离不漏：RESTRICTED 最近邻被 SQL where 排除，绝不出现在结果里。"""
    records = _separated_records(40)
    records[0] = _rec("r000#0", [1.0, 0.0, 0.0], sensitivity="RESTRICTED")  # 最近邻，应被挡
    store = PgVectorVectorStore(dsn=pg_url, pool_ceiling=8)
    try:
        store.upsert(records)
        hits = store.query([1.0, 0.0, 0.0], k=5, where={"sensitivity": "INTERNAL"})
        assert "r000#0" not in {h.id for h in hits}  # RESTRICTED 最近邻被挡
        assert all(h.metadata.get("sensitivity") == "INTERNAL" for h in hits)
    finally:
        store.close()
