"""VectorStore 缝：可插拔向量索引 + 带过滤的 top-k 相似度查询（落地 docs/prd-vector-store-seam.md）。

这条缝只担一件事：**存向量 + 回答一次带 where 过滤的 top-k 相似度查询**。它【不】管
BM25 / RRF 融合 / rerank——那些留在 HybridRetriever。任何实现（内存默认实现现在，
将来的 Qdrant / pgvector / FAISS adapter）都跑同一套 tests/conformance，把
provenance / isolation / determinism 三项不变量绑死在缝上。

InProcessVectorStore：零三方依赖、确定性的默认实现，行为等价于 HybridRetriever 今天的
cosine 暴力扫（brute-force cosine + id 升序破平分），让 `pip install ragspine` 的精简默认
仍可端到端跑（ADR 0009）。cosine 口径与 retrieval.cosine_similarity 一致：零向量一律 0.0。

三项不变量如何被【实现真正保证】（非注释）：
- Provenance —— VectorRecord 的 id 与 metadata（含 doc_id / source_locator）原样存、原样
  随 VectorHit 回传；store 既不臆造也不丢 id。
- Isolation —— where 过滤是「把敏感度隔离下推到存储层」的机制：过滤在【打分前】施加，被
  排除的记录即便是最近邻也绝不出现；同时诚实反证——不带过滤时本层【不】自动剔除
  RESTRICTED（权威剔除仍在 retrieval/link 与 retrieval/rerank 两出口）。
- Determinism —— 候选按 id 升序遍历（固定浮点求和次序），结果按 (-score, id) 排序，
  跨调用 / 跨独立实例逐位一致，平分按 id 升序稳定。
"""

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# 默认召回深度，与 retrieval.DEFAULT_TOP_K 对齐（单一出处）。
DEFAULT_QUERY_K = 50

# 工厂读取的环境变量名（缺省 spec 时生效；范式同 EMBEDDING_BACKEND_ENV）。
VECTOR_STORE_ENV = "RAGSPINE_VECTOR_STORE"


@dataclass(frozen=True)
class VectorRecord:
    """一条向量 + 其身份 + 可过滤元数据。承载血缘。

    id 是 chunk_id——provenance 锚点；metadata 含 doc_id / source_locator / topic /
    … / sensitivity，口径同 StoredChunk。frozen 保证入库后不被就地改写。
    """

    id: str
    vector: tuple[float, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VectorHit:
    """一条命中：id + cosine 分值 + metadata（血缘原样保留）。"""

    id: str
    score: float
    metadata: Mapping[str, str]


@runtime_checkable
class VectorStore(Protocol):
    """向量存储缝的最小结构接口（core 只 import 这个 Protocol，不 import 任何 SDK）。

    刻意保持在不变量所需的最低公约数（upsert / query+where / delete / count）；后端特定
    旋钮（HNSW 参数、pgvector lists/probes 等）属各 adapter 自己的配置，不进核心 Protocol。
    """

    def upsert(self, records: Sequence[VectorRecord]) -> int: ...

    def query(
        self,
        vector: Sequence[float],
        *,
        k: int = DEFAULT_QUERY_K,
        where: Mapping[str, str] | None = None,
    ) -> list[VectorHit]: ...

    def delete(self, *, where: Mapping[str, str]) -> int: ...

    def count(self) -> int: ...


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度；零向量一律 0.0（口径同 retrieval.cosine_similarity）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _matches(metadata: Mapping[str, str], where: Mapping[str, str] | None) -> bool:
    """where 过滤：所有键精确匹配的 AND；记录缺少任一被过滤键即排除（不视为通过）。"""
    if not where:
        return True
    for key, value in where.items():
        if metadata.get(key) != value:
            return False
    return True


class InProcessVectorStore:
    """零依赖、确定性的内存默认实现：brute-force cosine + id 升序破平分。

    维度不变式：库内所有向量共享同一维度。首条 upsert 确定维度；此后任何长度不同的
    向量（同批内或跨批）或维度不符的查询向量都抛 ValueError——绝不静默给坏向量。
    """

    def __init__(self) -> None:
        self._records: dict[str, VectorRecord] = {}
        self._dim: int | None = None

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        """写入记录；已存在的 id 为【替换】（upsert 非 append）。返回写入条数；空输入 -> 0。

        先做全量维度校验再落库，保证「混维一批整体拒绝、库状态不被部分污染」的原子语义。
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
        for record in records:
            self._records[record.id] = record
        self._dim = expected
        return len(records)

    def query(
        self,
        vector: Sequence[float],
        *,
        k: int = DEFAULT_QUERY_K,
        where: Mapping[str, str] | None = None,
    ) -> list[VectorHit]:
        """返回 cosine 降序的 top-k；平分按 id 升序。

        过滤在打分前施加（被 where 排除的记录即便最近邻也不出现）。空库 -> []（无维度可校
        验）；非空库时查询向量维度须与库内一致，否则 ValueError。零向量 cosine 处处 0，不崩。
        """
        if not self._records:
            return []
        vector = tuple(vector)
        if self._dim is not None and len(vector) != self._dim:
            raise ValueError(
                f"查询向量维度 {len(vector)} 与库内维度 {self._dim} 不一致"
            )
        # 候选按 id 升序遍历：固定浮点求和次序，跨实例 / 跨调用逐位一致。
        scored = [
            (_cosine(vector, record.vector), record)
            for rid in sorted(self._records)
            for record in (self._records[rid],)
            if _matches(record.metadata, where)
        ]
        # 主键 cosine 降序、平分按 id 升序——确定性排序。
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return [
            VectorHit(id=record.id, score=score, metadata=record.metadata)
            for score, record in scored[:k]
        ]

    def delete(self, *, where: Mapping[str, str]) -> int:
        """删除所有匹配 where 的记录（语义同 query 的过滤）；返回删除条数。"""
        victims = [rid for rid, record in self._records.items() if _matches(record.metadata, where)]
        for rid in victims:
            del self._records[rid]
        return len(victims)

    def count(self) -> int:
        """库内记录数（upsert / delete 的净效果）。"""
        return len(self._records)


def make_vector_store(spec: str | None = None, **kwargs: Any) -> VectorStore | None:
    """向量存储工厂：把「选哪个 store」从改代码降为一个 spec/env（范式同 make_embedding_backend）。

    spec 取值（大小写 / 留白不敏感；缺省读环境变量 RAGSPINE_VECTOR_STORE）：
        - None / 'none'                          -> None（不注入具体 store；检索器用内置内存默认）
        - 'in_process' / 'in-process' / 'memory' -> InProcessVectorStore（零依赖确定性内存默认）
        - 'sqlite_vec' / 'pgvector' / 'qdrant'   -> 对应 adapter（behind [vector] extra，延迟 import）
        - 其他                                    -> ValueError（真实后端待后续 adapter 落地）

    返回 VectorStore 实例或 None（可直接喂给 HybridRetriever / NarrativeIndex /
    build_narrative_retriever 的 vector_store 参数）。None 与显式 InProcessVectorStore 在
    检索结果上等价——前者让检索器自建内置默认，后者把同一个 store 实例交由调用方持有/复用。
    """
    if spec is None:
        spec = os.environ.get(VECTOR_STORE_ENV)
    normalized = (spec or "none").strip().lower()
    if normalized == "none":
        return None
    if normalized in ("in_process", "in-process", "inprocess", "memory"):
        return InProcessVectorStore(**kwargs)
    if normalized in ("sqlite_vec", "sqlite-vec", "sqlitevec"):
        # 延迟 import：仅选用时才拉适配器（其 __init__ 再延迟 import sqlite_vec SDK，behind [vector]）。
        from ragspine.retrieval.vector.adapters.sqlite_vec import SqliteVecVectorStore

        return SqliteVecVectorStore(**kwargs)
    if normalized in ("pgvector", "pg_vector"):
        from ragspine.retrieval.vector.adapters.pgvector import PgVectorVectorStore

        return PgVectorVectorStore(**kwargs)
    if normalized == "qdrant":
        from ragspine.retrieval.vector.adapters.qdrant import QdrantVectorStore

        return QdrantVectorStore(**kwargs)
    raise ValueError(
        f"未知 vector store spec：{spec!r}"
        "（本期可选 none / in_process / sqlite_vec / pgvector / qdrant；其余等待后续 adapter 落地）"
    )
