"""pgvector 适配器：把 VectorStore 缝落到 PostgreSQL 的 pgvector 扩展（网络化 / 共享持久化）。

第二个真实 VectorStore adapter（见 src/ragspine/retrieval/docs/vector-store.md 的 adapter #2）。
驱动用 **pg8000**（纯 Python、**BSD**，permissive）——【不用】psycopg（LGPL，被 ADR 0009
的 ≤Apache-2.0 许可门排除）。延迟 import，behind `[vector]` extra；零依赖默认仍是 InProcessVectorStore。

**口径与 InProcessVectorStore 逐字段一致**——复用 store._cosine / store._matches，故同一套
tests/conformance 全过：cosine 零向量一律 0.0、平分按 id 升序、where「缺键即排除」AND 语义、
provenance metadata 原样回传、确定性（pgvector float4 存储，每实现可复现）。

存储：一张 `(id TEXT PK, embedding vector(N), meta JSONB)` 表 + 一个 embedding 列上的 **HNSW
（vector_cosine_ops）索引**。`table=None` 时建【会话级 TEMP 表】（断连即自动 drop —— conformance
各实例天然隔离、零残留）；给了 table 名则建持久命名表（CREATE TABLE IF NOT EXISTS，跨连接 / 跨进程持久）。

打分走【native HNSW KNN 收窄候选池 + Python 精确重排】（落地 PRD 的 Native ANN/KNN），where 始终
【下推到 SQL】（`meta->>'k' = v` 的 AND，缺键即排除，隔离下推 + 减传输）：
  - 候选池 pool = max(k, ef_search) 但至少 min(count, pool_ceiling)（见 store._pool_size）。
  - pool >= count（含全部 conformance 库）：走全表扫 `SELECT ... WHERE <where>`（不带 ORDER BY <=>），
    取回【全部】匹配行（含零向量行）——规避 pgvector `<=>` 对零向量返回 NaN、HNSW 索引把 NaN 距离行
    丢弃的坑；再用 store._rerank 在 Python 以精确 cosine 重排，逐位复现暴力扫，exact 能力旗标不松动。
  - pool < count（大库）：`SET hnsw.ef_search` 后 `... WHERE <where> ORDER BY embedding <=> q LIMIT pool`
    经 HNSW 索引收窄到 pool 候选（规模化），再精确重排定 top-k（Python 重算规避 `<=>` 的 NaN/破平分口径）。
索引参数（m / ef_construction / ef_search / pool_ceiling）是本 adapter 自有旋钮，不进核心 VectorStore Protocol。

连接：RAGSPINE_PG_URL（postgresql://user[:pass]@host:port/db）或显式 dsn；未装 pg8000 /
未配 URL 时抛友好错。conformance 需 RAGSPINE_PG_URL 指向带 pgvector 扩展的 PG，否则该参数 skip。

float4 值域：pgvector 的 vector 是 float4，分量须有限且在 float4 范围内（真实 embedding 恒满足）；
非有限 / 越界分量会被 pgvector 在 upsert 时拒绝（InProcessVectorStore 是 float64、会接受 NaN）——
这是 float4 后端的固有边界，正常归一化向量永不触发。
"""

import json
import os
import re
import uuid
import weakref
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from ragspine.retrieval.vector.store import (
    DEFAULT_QUERY_K,
    VectorHit,
    VectorRecord,
    _pool_size,
    _rerank,
)

PG_URL_ENV = "RAGSPINE_PG_URL"

# pgvector 的 hnsw.ef_search 取值上界（会话级 GUC）；SET 前一律 clamp 到此值。
_HNSW_EF_SEARCH_MAX = 1000

# 表名只能是合法 SQL 标识符：SQL 标识符不能走绑定参数，故表名以 f-string 拼入 SQL；
# 强约束为 [A-Za-z_][A-Za-z0-9_]* 杜绝注入（默认 uuid 表名天然满足；用户传名亦须满足）。
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_dsn(dsn: str) -> dict[str, Any]:
    """postgresql://user[:pass]@host[:port]/db -> pg8000.native.Connection kwargs。"""
    u = urlparse(dsn)
    kwargs: dict[str, Any] = {
        "user": u.username or "postgres",
        "host": u.hostname or "localhost",
        "port": u.port or 5432,
        "database": (u.path or "/postgres").lstrip("/") or "postgres",
    }
    if u.password:
        kwargs["password"] = u.password
    return kwargs


class PgVectorVectorStore:
    """pgvector（PostgreSQL）后端的 VectorStore 实现（conformance-bound，精确、确定性）。

    维度不变式同 InProcessVectorStore：库内向量共享单一维度，首条 upsert 定维（建 vector(N) 列）；
    此后任何长度不符的向量（同批 / 跨批）或维度不符的查询向量抛 ValueError。空库查询返回 []。
    """

    def __init__(
        self,
        dsn: str | None = None,
        table: str | None = None,
        *,
        m: int = 16,
        ef_construction: int = 64,
        ef_search: int = 100,
        pool_ceiling: int = 4096,
    ) -> None:
        try:
            import pg8000.native as pg
        except ImportError as exc:  # pragma: no cover - 仅未装 [vector] 时触发
            raise ImportError(
                "未安装 pg8000：pip install 'ragspine[vector]' 或 pip install pg8000；"
                "离线/纯内存场景用默认 InProcessVectorStore 即可，无需安装。"
            ) from exc

        # HNSW 索引 + native KNN 候选池旋钮（本 adapter 自有，不进核心 Protocol）：
        # m / ef_construction 建索引用，ef_search / pool_ceiling 查询收窄用（int，f-string 拼入故强校验）。
        self._m = int(m)
        self._ef_construction = int(ef_construction)
        self._ef_search = int(ef_search)
        self._pool_ceiling = int(pool_ceiling)
        dsn = dsn or os.environ.get(PG_URL_ENV)
        if not dsn:
            raise ValueError(
                f"未提供 Postgres 连接：传 dsn= 或设环境变量 {PG_URL_ENV}"
                "（postgresql://user[:pass]@host:port/db），且该库须装有 pgvector 扩展。"
            )
        self._conn = pg.Connection(**_parse_dsn(dsn))
        self._conn.run("CREATE EXTENSION IF NOT EXISTS vector")
        # table=None -> 会话级 TEMP 表（断连自动 drop，conformance 隔离）；否则持久命名表。
        if table is not None and not _TABLE_RE.match(table):
            raise ValueError(
                f"非法表名 {table!r}：只接受 SQL 标识符 [A-Za-z_][A-Za-z0-9_]*（表名不可参数化，须强约束防注入）"
            )
        self._temp = table is None
        self._table = table or ("rv_" + uuid.uuid4().hex)
        self._dim: int | None = None
        self._finalizer = weakref.finalize(self, self._conn.close)
        self._restore_dim()

    def _qtable(self) -> str:
        """带 schema 限定的表名（TEMP 表在 pg_temp schema）——供 regclass / SQL 引用。"""
        return f"pg_temp.{self._table}" if self._temp else self._table

    def _restore_dim(self) -> None:
        """持久命名表重开时从 vector(N) 列的 atttypmod 恢复维度（TEMP 表必为新建，跳过）。"""
        if self._temp:
            return
        rows = self._conn.run(
            "SELECT a.atttypmod FROM pg_attribute a "
            "WHERE a.attrelid = to_regclass(:t) AND a.attname = 'embedding'",
            t=self._table,
        )
        if rows and rows[0][0] is not None and int(rows[0][0]) > 0:
            self._dim = int(rows[0][0])

    def _ensure_table(self, dim: int) -> None:
        if self._dim is None:
            kind = "TEMP TABLE" if self._temp else "TABLE"
            self._conn.run(
                f"CREATE {kind} IF NOT EXISTS {self._table} "
                f"(id TEXT PRIMARY KEY, embedding vector({dim}), meta JSONB)"
            )
            # embedding 列上的 HNSW（cosine）索引：加速大库的 `ORDER BY <=> LIMIT pool` 候选收窄。
            # HNSW 支持空表增量建（不像 IVFFlat 需先有数据训练），故建表即建索引。m / ef_construction
            # 为 int（强校验后 f-string 拼入，非值绑定位，安全）。TEMP 表的索引随表 drop。
            self._conn.run(
                f"CREATE INDEX IF NOT EXISTS {self._table}_emb_hnsw ON {self._table} "
                f"USING hnsw (embedding vector_cosine_ops) "
                f"WITH (m = {self._m}, ef_construction = {self._ef_construction})"
            )
            self._dim = dim

    @staticmethod
    def _vec_literal(vector: Sequence[float]) -> str:
        return "[" + ",".join(repr(float(x)) for x in vector) + "]"

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        """写入记录；同 id 为替换（INSERT ... ON CONFLICT DO UPDATE）。

        先全量校验维度再落库（混维一批整体拒绝、库状态不被部分污染），语义同 InProcessVectorStore；
        整批包事务，任一行错误回滚，不留半截。
        """
        records = list(records)
        if not records:
            return 0
        expected = self._dim
        for record in records:
            dim = len(record.vector)
            if expected is None:
                expected = dim
            elif dim != expected:
                raise ValueError(
                    f"向量维度不一致：期望 {expected}，记录 {record.id!r} 为 {dim}"
                )
        assert expected is not None  # records 非空 -> 循环至少一轮，expected 已定维
        self._ensure_table(expected)
        self._conn.run("BEGIN")
        try:
            for record in records:
                self._conn.run(
                    f"INSERT INTO {self._table}(id, embedding, meta) VALUES (:i, :v, :m) "
                    "ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding, meta = EXCLUDED.meta",
                    i=record.id,
                    v=self._vec_literal(record.vector),
                    m=json.dumps(dict(record.metadata), ensure_ascii=False, sort_keys=True),
                )
            self._conn.run("COMMIT")
        except Exception:
            self._conn.run("ROLLBACK")
            raise
        return len(records)

    @staticmethod
    def _where_sql(where: Mapping[str, str] | None) -> tuple[str, dict[str, Any]]:
        """where dict -> SQL 片段 + 参数（meta->>'k' = v 的 AND；缺键即排除，同 _matches）。"""
        if not where:
            return "", {}
        clauses, params = [], {}
        for idx, (key, value) in enumerate(where.items()):
            clauses.append(f"meta->>:k{idx} = :v{idx}")
            params[f"k{idx}"] = key
            params[f"v{idx}"] = value
        return " WHERE " + " AND ".join(clauses), params

    def query(
        self,
        vector: Sequence[float],
        *,
        k: int = DEFAULT_QUERY_K,
        where: Mapping[str, str] | None = None,
    ) -> list[VectorHit]:
        """native HNSW KNN 收窄候选池 + Python 精确重排：返回 cosine 降序 top-k、平分按 id 升序。

        where 始终【下推到 SQL】过滤；打分/排序在 Python 精确重算（复用 _cosine / _matches，规避
        pgvector `<=>` 的 NaN 与破平分口径差异）。空库 -> []；非空库查询向量维度须与库内一致否则
        ValueError；零向量 cosine 处处 0，不崩。pool >= count 走全表扫（含零向量行、逐位复现暴力扫），
        pool < count 经 HNSW 索引收窄到 pool 候选。
        """
        if self._dim is None:
            return []
        vector = tuple(vector)
        if len(vector) != self._dim:
            raise ValueError(
                f"查询向量维度 {len(vector)} 与库内维度 {self._dim} 不一致"
            )
        n = self.count()
        if n == 0:
            return []
        pool = _pool_size(k, self._ef_search, n, self._pool_ceiling)
        where_sql, params = self._where_sql(where)
        if pool >= n:
            # 小库：全表扫取回全部匹配行（含 `<=>` 会判 NaN 的零向量行），精确重排逐位复现暴力扫。
            sql = f"SELECT id, embedding, meta FROM {self._table}{where_sql}"
        else:
            # 大库：经 HNSW 索引按 cosine 距离收窄到 pool 候选（ef_search 放大候选广度），再精确重排。
            ef = max(1, min(max(self._ef_search, pool), _HNSW_EF_SEARCH_MAX))
            self._conn.run(f"SET hnsw.ef_search = {ef}")
            params["q"] = self._vec_literal(vector)
            params["pool"] = pool
            sql = (
                f"SELECT id, embedding, meta FROM {self._table}{where_sql} "
                "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :pool"
            )
        candidates = [
            (rid, tuple(float(x) for x in emb.strip("[]").split(",")), meta)
            for rid, emb, meta in self._conn.run(sql, **params)
        ]
        return _rerank(candidates, vector, k=k, where=where)

    def delete(self, *, where: Mapping[str, str]) -> int:
        """删除所有匹配 where 的记录（语义同 query 的过滤）；返回删除条数。"""
        if self._dim is None:
            return 0
        where_sql, params = self._where_sql(where)
        self._conn.run(f"DELETE FROM {self._table}{where_sql}", **params)
        return int(self._conn.row_count or 0)

    def count(self) -> int:
        """库内记录数（upsert / delete 的净效果）。"""
        if self._dim is None:
            return 0
        return int(self._conn.run(f"SELECT count(*) FROM {self._table}")[0][0])

    def close(self) -> None:
        self._finalizer()  # 幂等：关连接（TEMP 表随之 drop）并注销 finalizer，重复调用安全
