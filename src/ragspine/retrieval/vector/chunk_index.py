"""持久化块向量索引：入库时把块向量写进 sqlite-vec 文件，检索期直接读同一个文件。

解决的问题：``ChunkStore`` 只存块正文，检索期 ``NarrativeIndex`` 以 store-managed 方式只嵌 query、
不重嵌块——若入库时没有把块向量落盘，向量通道就是一个空库（混合检索退化成纯 BM25 而不自知）。

本模块把「块库 → 向量库」的同步做成一个幂等操作（``ChunkVectorIndex.sync``）：
- **存储**：复用 ``SqliteVecVectorStore``（vec0 虚表，[vector] extra）落到一个文件；同一文件里再建两张
  普通清单表：``chunk_vector_meta``（模型标识 + 维度）与 ``chunk_vector_docs``（每 doc 的内容签名）。
- **幂等 / 替换**：按 doc 粒度比对签名（chunk_id + 正文 + 过滤元数据）。签名不变即跳过；变了就先
  ``delete(where={doc_id})`` 撤下旧向量再重嵌；块库里已不存在的 doc 其向量一并撤下。
- **隔离**：只嵌 ``PersistencePolicy`` 放行的块，默认 ``IsolationFirstPolicy`` 绝不落盘 RESTRICTED 块的
  向量；被挡下的块数计入 ``withheld``。
- **模型标识**：首次写入时记下 embedding 模型标识与维度；之后 sync / 检索发现模型或维度不一致即抛
  ``VectorIndexMismatchError``（要求重建），绝不混用两个模型的向量。
- **索引文本版本**（标题进索引开关 ``contextual_index``）：嵌入的是块的【索引文本】（off 时即正文），doc 签名
  也按索引文本算——开关一变，索引文本变了的 doc 在下次 sync 时自动重嵌。库内另记 ``contextual_index``
  （off 不写，旧库缺省即 off）；sync 期间标记为「迁移中」，检索期版本不一致即抛 ``VectorIndexMismatchError``，
  绝不静默混用两种索引文本的向量。
"""

import hashlib
import json
import sqlite3
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ragspine.retrieval.chunking.chunk_store import StoredChunk
from ragspine.retrieval.contextual import (
    CONTEXTUAL_INDEX_OFF,
    IndexTextFn,
    make_contextual_index_mode,
    make_index_text_fn,
)
from ragspine.retrieval.lexical.retrieval import EmbeddingBackend, _index_text, _record_metadata
from ragspine.retrieval.vector.adapters.sqlite_vec import SqliteVecVectorStore
from ragspine.retrieval.vector.persistence_policy import IsolationFirstPolicy, PersistencePolicy
from ragspine.retrieval.vector.store import VectorRecord

VECTOR_DB_SUFFIX = ".vectors.db"
DEFAULT_EMBED_BATCH = 32

_META_TABLE = "chunk_vector_meta"
_DOCS_TABLE = "chunk_vector_docs"
_INDEX_TEXT_KEY = "contextual_index"
_MIGRATING = "migrating:"


class VectorIndexMismatchError(ValueError):
    """向量库里的模型标识 / 维度与当前 embedding 后端不一致（需要重建向量库）。"""


@dataclass(frozen=True)
class VectorSyncReport:
    """一次块向量同步的计数（只含计数与标识，不含任何正文）。

    vector_channel：'hybrid'＝向量已同步；'bm25_only'＝没有 embedding 后端，只走 BM25（vector_reason 给原因）。
    embedded / deleted：本次新写入 / 撤下的向量条数；withheld：被持久化策略挡下（如 RESTRICTED）的块数；
    unchanged_docs：签名未变而跳过的 doc 数；total：同步后库内向量总数；n_chunks：块库活跃块数。
    """

    vector_channel: str
    vector_reason: str = ""
    model_id: str = ""
    dim: int | None = None
    n_chunks: int = 0
    embedded: int = 0
    deleted: int = 0
    withheld: int = 0
    unchanged_docs: int = 0
    total: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "n_chunks": self.n_chunks,
            "embedded": self.embedded,
            "deleted": self.deleted,
            "withheld": self.withheld,
            "unchanged_docs": self.unchanged_docs,
            "total": self.total,
        }


def default_vector_db_path(chunk_db: str | Path) -> Path:
    """块库旁的向量库文件：``knowledge.db`` -> ``knowledge.vectors.db``。"""
    path = Path(chunk_db)
    return path.with_name(path.stem + VECTOR_DB_SUFFIX)


def embedding_model_id(backend: EmbeddingBackend) -> str:
    """embedding 后端的模型标识：优先 ``model_id`` 属性，否则类名 + 模型名 / 维度。"""
    explicit = getattr(backend, "model_id", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    name = type(backend).__name__
    for attr in ("model_name", "model", "dim"):
        value = getattr(backend, attr, None)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and value != "":
            return f"{name}:{attr}={value}"
    return name


def _doc_signature(chunks: Sequence[StoredChunk], index_text_fn: IndexTextFn | None = None) -> str:
    payload = [
        [c.chunk_id, _index_text(c, index_text_fn), sorted(_record_metadata(c).items())]
        for c in sorted(chunks, key=lambda c: c.chunk_id)
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ChunkVectorIndex:
    """一个 sqlite-vec 文件 = 块向量（vec0）+ 模型标识 + doc 签名清单。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self.store = SqliteVecVectorStore(self.db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._finalizer = weakref.finalize(self, self._conn.close)
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_META_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_DOCS_TABLE} ("
            "doc_id TEXT PRIMARY KEY, signature TEXT NOT NULL, n_vectors INTEGER NOT NULL)"
        )
        self._conn.commit()

    @property
    def model_id(self) -> str | None:
        return self._meta("model_id")

    @property
    def dim(self) -> int | None:
        value = self._meta("dim")
        return int(value) if value is not None else None

    @property
    def contextual_index(self) -> str:
        """向量对应的索引文本版本（off / heading / full；同步中断时为 'migrating:<目标>'）。"""
        return self._meta(_INDEX_TEXT_KEY) or CONTEXTUAL_INDEX_OFF

    def count(self) -> int:
        return self.store.count()

    def check_compatible(
        self, model_id: str, dim: int | None = None, *, contextual_index: str | None = None
    ) -> None:
        """模型标识 / 维度（/ 给了时的索引文本版本）与库内记录不一致即抛 VectorIndexMismatchError
        （空库不校验）。"""
        if contextual_index is not None and self.count() > 0:
            wanted = make_contextual_index_mode(contextual_index)
            stored = self.contextual_index
            if stored != wanted:
                raise VectorIndexMismatchError(
                    f"向量库 {self.db_path} 的索引文本版本为 contextual_index={stored!r}，当前为 "
                    f"{wanted!r}；向量与索引文本不对应，请按当前配置重新入库同步以重建（rebuild）"
                )
        stored_model, stored_dim = self.model_id, self.dim
        if stored_model is not None and stored_model != model_id:
            raise VectorIndexMismatchError(
                f"向量库 {self.db_path} 由 embedding 模型 {stored_model!r} 构建，当前为 {model_id!r}；"
                "不能混用，请删除该文件后重新入库以重建（rebuild）"
            )
        if dim is not None and stored_dim is not None and stored_dim != dim:
            raise VectorIndexMismatchError(
                f"向量库 {self.db_path} 维度为 {stored_dim}，当前 embedding 为 {dim}；"
                "请删除该文件后重新入库以重建（rebuild）"
            )

    def sync(
        self,
        chunks: Sequence[StoredChunk],
        backend: EmbeddingBackend,
        *,
        model_id: str,
        persistence_policy: PersistencePolicy | None = None,
        batch_size: int = DEFAULT_EMBED_BATCH,
        contextual_index: str | None = CONTEXTUAL_INDEX_OFF,
    ) -> VectorSyncReport:
        """把活跃块同步进向量库（doc 粒度幂等），返回计数。

        contextual_index：嵌入哪种索引文本（off＝正文，逐字节同旧行为）；与库内版本不同时先标记「迁移中」，
        签名变了的 doc 重嵌，全部完成后才写入新版本。
        """
        self.check_compatible(model_id)
        mode = make_contextual_index_mode(contextual_index)
        index_text_fn = make_index_text_fn(mode)
        if self.contextual_index != mode:
            self._set_meta(_INDEX_TEXT_KEY, _MIGRATING + mode)
        policy = persistence_policy or IsolationFirstPolicy()
        by_doc: dict[str, list[StoredChunk]] = {}
        withheld = 0
        for chunk in chunks:
            by_doc.setdefault(chunk.doc_id, [])
            if policy.persistable(chunk):
                by_doc[chunk.doc_id].append(chunk)
            else:
                withheld += 1
        existing = dict(
            self._conn.execute(f"SELECT doc_id, signature FROM {_DOCS_TABLE}").fetchall()
        )

        deleted = 0
        for doc_id in sorted(set(existing) - set(by_doc)):
            deleted += self.store.delete(where={"doc_id": doc_id})
            self._conn.execute(f"DELETE FROM {_DOCS_TABLE} WHERE doc_id = ?", (doc_id,))
            self._conn.commit()

        embedded = 0
        unchanged = 0
        for doc_id in sorted(by_doc):
            doc_chunks = by_doc[doc_id]
            signature = _doc_signature(doc_chunks, index_text_fn)
            if existing.get(doc_id) == signature:
                unchanged += 1
                continue
            if doc_id in existing:
                deleted += self.store.delete(where={"doc_id": doc_id})
            for start in range(0, len(doc_chunks), batch_size):
                batch = doc_chunks[start : start + batch_size]
                vectors = backend.embed_texts([_index_text(c, index_text_fn) for c in batch])
                if vectors:
                    self._claim(model_id, len(vectors[0]))
                embedded += self.store.upsert(
                    [
                        VectorRecord(id=c.chunk_id, vector=tuple(v), metadata=_record_metadata(c))
                        for c, v in zip(batch, vectors, strict=True)
                    ]
                )
            self._conn.execute(
                f"INSERT OR REPLACE INTO {_DOCS_TABLE} (doc_id, signature, n_vectors) "
                "VALUES (?, ?, ?)",
                (doc_id, signature, len(doc_chunks)),
            )
            self._conn.commit()
        if self.contextual_index != mode:
            self._set_meta(_INDEX_TEXT_KEY, mode)

        return VectorSyncReport(
            vector_channel="hybrid",
            model_id=model_id,
            dim=self.dim,
            n_chunks=len(chunks),
            embedded=embedded,
            deleted=deleted,
            withheld=withheld,
            unchanged_docs=unchanged,
            total=self.count(),
        )

    def close(self) -> None:
        self._finalizer()
        self.store.close()

    def _meta(self, key: str) -> str | None:
        row = self._conn.execute(
            f"SELECT value FROM {_META_TABLE} WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO {_META_TABLE} (key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    def _claim(self, model_id: str, dim: int) -> None:
        """首次写入记下模型标识与维度；之后必须一致。"""
        self.check_compatible(model_id, dim)
        if self.model_id is None:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO {_META_TABLE} (key, value) VALUES (?, ?)",
                [("model_id", model_id), ("dim", str(dim))],
            )
            self._conn.commit()
