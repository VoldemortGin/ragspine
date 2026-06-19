"""sqlite-vec 适配器：把 VectorStore 缝落到 sqlite-vec 的 vec0 虚表（持久化 + 索引格式）。

第一个真实 VectorStore adapter（落地 docs/prd-vector-store-seam.md 的 adapter roadmap #1）。
范式同 ragspine 既有 SDK 适配（OcrBackend / OpenAIEmbeddingBackend）：sqlite_vec 延迟 import
（不装也能 import 本模块），behind `[vector]` extra；零依赖默认仍是 InProcessVectorStore。

**口径与 InProcessVectorStore 逐字段一致**——直接复用 store._cosine / store._matches，故
同一套 tests/conformance 全过：cosine 零向量一律 0.0、平分按 id 升序、where「缺键即排除」的
AND 语义、provenance metadata 原样回传、确定性（float32 存储，每实现可复现）。

存储：vec0 虚表 `id TEXT PK, embedding float[N] distance_metric=cosine, +meta TEXT(JSON)`，
默认 :memory:（conformance / 默认零副作用），可传 db_path 落盘持久化。

打分走【全表扫 + Python cosine】而非 vec0 原生 KNN MATCH，是刻意取舍：
  - vec0 的 `k` 上限 4096，且对零向量返回 NULL 距离——全表扫两者皆规避；
  - 全表扫让打分/排序/where 过滤口径与 InProcessVectorStore 完全对齐（复用同一对 helper），
    从而无需 conformance「exact vs approximate」能力旗标即整套通过。
vec0 原生 KNN 索引加速（带精确 float64 重排）属规模化后续优化，不在首个适配器范围。

诚实边界（持久化 + 隔离）：把 store 指向文件即把向量（含 RESTRICTED 块的衍生向量）落盘——
与「RESTRICTED 不出域」是不同关注点（向量在自有 db、不进 prompt），但仍是 at-rest 衍生面；
默认 :memory: 无此问题，落盘用法的护栏见 docs/prd-vector-store-seam.md 的持久化增量。
"""

import json
import re
import sqlite3
import struct
import weakref
from collections.abc import Mapping, Sequence

from ragspine.retrieval.vector.store import (
    DEFAULT_QUERY_K,
    VectorHit,
    VectorRecord,
    _cosine,
    _matches,
)

_TABLE = "vec_items"
_DIM_RE = re.compile(r"float\[(\d+)\]")


class SqliteVecVectorStore:
    """sqlite-vec（vec0）后端的 VectorStore 实现（conformance-bound，精确、确定性）。

    维度不变式同 InProcessVectorStore：库内向量共享单一维度，首条 upsert 定维；此后任何
    长度不符的向量（同批 / 跨批）或维度不符的查询向量抛 ValueError。空库查询返回 []。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        try:
            import sqlite_vec
        except ImportError as exc:  # pragma: no cover - 仅未装 [vector] 时触发
            raise ImportError(
                "未安装 sqlite-vec：pip install 'ragspine[vector]' 或 pip install sqlite-vec；"
                "离线/纯内存场景用默认 InProcessVectorStore 即可，无需安装。"
            ) from exc

        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._dim: int | None = None
        # 确定性资源回收（同 ChunkStore）：GC 时兜底关连接，免 ResourceWarning 被零警告门升级。
        self._finalizer = weakref.finalize(self, self._conn.close)
        self._restore_dim()

    def _restore_dim(self) -> None:
        """持久化重开时从已存在的 vec0 表恢复维度（空表则解析 schema 的 float[N]）。"""
        exists = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
        ).fetchone()
        if exists is None:
            return  # 表未建，dim 仍 None（首次 upsert 按数据建表）
        row = self._conn.execute(f"SELECT embedding FROM {_TABLE} LIMIT 1").fetchone()
        if row is not None and row[0] is not None:
            self._dim = len(row[0]) // 4  # float32 = 4 字节
            return
        match = _DIM_RE.search(exists[0] or "")
        if match:
            self._dim = int(match.group(1))

    def _ensure_table(self, dim: int) -> None:
        if self._dim is None:
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {_TABLE} USING vec0("
                f"id TEXT PRIMARY KEY, embedding float[{dim}] distance_metric=cosine, +meta TEXT)"
            )
            self._dim = dim

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        """写入记录；同 id 为替换（vec0 不支持 INSERT OR REPLACE，故 DELETE 再 INSERT）。

        先全量校验维度再落库（混维一批整体拒绝、库状态不被部分污染），语义同 InProcessVectorStore。
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
        # 写循环包 try/rollback：任一记录的 sqlite 错误（落盘满 / 库锁 / IO）整批回滚，
        # 不留半截事务被后续 commit 冲刷——兑现「库状态不被部分污染」的原子语义。
        try:
            for record in records:
                blob = struct.pack(f"{expected}f", *record.vector)
                meta = json.dumps(dict(record.metadata), ensure_ascii=False, sort_keys=True)
                self._conn.execute(f"DELETE FROM {_TABLE} WHERE id = ?", (record.id,))
                self._conn.execute(
                    f"INSERT INTO {_TABLE}(id, embedding, meta) VALUES (?, ?, ?)",
                    (record.id, blob, meta),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return len(records)

    def query(
        self,
        vector: Sequence[float],
        *,
        k: int = DEFAULT_QUERY_K,
        where: Mapping[str, str] | None = None,
    ) -> list[VectorHit]:
        """返回 cosine 降序的 top-k；平分按 id 升序；where 在打分前过滤（缺键即排除）。

        口径与 InProcessVectorStore 完全一致（复用 _cosine / _matches）：空库 -> []；非空库查询
        向量维度须与库内一致否则 ValueError；零向量 cosine 处处 0，不崩。
        """
        if self._dim is None:
            return []
        vector = tuple(vector)
        if len(vector) != self._dim:
            raise ValueError(
                f"查询向量维度 {len(vector)} 与库内维度 {self._dim} 不一致"
            )
        scored = []
        for rid, blob, meta_json in self._conn.execute(
            f"SELECT id, embedding, meta FROM {_TABLE}"
        ).fetchall():
            metadata = json.loads(meta_json)
            if not _matches(metadata, where):
                continue
            stored = struct.unpack(f"{self._dim}f", blob)
            scored.append((_cosine(vector, stored), rid, metadata))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [VectorHit(id=rid, score=score, metadata=md) for score, rid, md in scored[:k]]

    def delete(self, *, where: Mapping[str, str]) -> int:
        """删除所有匹配 where 的记录（语义同 query 的过滤）；返回删除条数。"""
        if self._dim is None:
            return 0
        victims = [
            rid
            for rid, meta_json in self._conn.execute(f"SELECT id, meta FROM {_TABLE}").fetchall()
            if _matches(json.loads(meta_json), where)
        ]
        try:
            for rid in victims:
                self._conn.execute(f"DELETE FROM {_TABLE} WHERE id = ?", (rid,))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return len(victims)

    def count(self) -> int:
        """库内记录数（upsert / delete 的净效果）。"""
        if self._dim is None:
            return 0
        return int(self._conn.execute(f"SELECT count(*) FROM {_TABLE}").fetchone()[0])

    def close(self) -> None:
        self._finalizer()  # 幂等：关连接并注销 finalizer，重复调用安全
