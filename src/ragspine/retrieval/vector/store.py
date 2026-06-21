"""VectorStore 缝：可插拔向量索引 + 带过滤的 top-k 相似度查询（live 契约见 src/ragspine/retrieval/docs/vector-store.md）。

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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# 默认召回深度，与 retrieval.DEFAULT_TOP_K 对齐（单一出处）。
DEFAULT_QUERY_K = 50

# 工厂读取的环境变量名（缺省 spec 时生效；范式同 EMBEDDING_BACKEND_ENV）。
VECTOR_STORE_ENV = "RAGSPINE_VECTOR_STORE"

# 第三方后端自动发现的 entry-point group：一个包在此 group 下注册一行
# （pyproject `[project.entry-points."ragspine.vector_stores"]`），make_vector_store
# 就能按名字选中它——核心零改动、零 SDK import（落地 docs/prd-breadth-via-adapters.md
# 「Registry + entry-point discovery」与 user stories 1 & 4）。
VECTOR_STORE_ENTRY_POINT_GROUP = "ragspine.vector_stores"


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


def _pool_size(k: int, ef_search: int, count: int, ceiling: int) -> int:
    """native ANN 候选池大小：max(k, ef_search) 但至少 min(count, ceiling)。

    供 sqlite-vec / pgvector / qdrant 三适配器共享的「native ANN 收窄候选池 -> 精确重排定 top-k」
    设计：小库（count <= ceiling）下池 >= count，候选覆盖全部行 -> 精确重排逐位复现暴力扫，故
    exact 能力旗标不松动；大库则收窄到 ANN 候选（规模化）。ceiling / ef_search 是各 adapter 自有
    旋钮（默认值见各 __init__），刻意【不进】核心 VectorStore Protocol——避免抽象泄漏后端参数。
    """
    return max(k, ef_search, min(count, ceiling))


def _rerank(
    candidates: Iterable[tuple[str, Sequence[float], Mapping[str, str]]],
    query: Sequence[float],
    *,
    k: int,
    where: Mapping[str, str] | None,
) -> list[VectorHit]:
    """native ANN 候选池的【精确 cosine 重排】——缝层共享，三适配器复用，打分口径逐字段一致。

    对候选 (id, 向量, metadata)：施 where 过滤（_matches，权威隔离出口）、按精确 _cosine 打分、
    (-score, id) 排序、取 top-k 装回 VectorHit。这是「native ANN 收窄 -> 精确重排」的重排半边：
    复用与 InProcessVectorStore 同一对 helper（_cosine / _matches），故零向量一律 0.0、平分按 id
    升序、where「缺键即排除」AND 语义完全对齐——小库下候选池覆盖全部行即逐位复现暴力扫。
    """
    scored = [
        (_cosine(query, vector), rid, metadata)
        for rid, vector, metadata in candidates
        if _matches(metadata, where)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [VectorHit(id=rid, score=score, metadata=md) for score, rid, md in scored[:k]]


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


# ---------------------------------------------------------------------------
# 注册表：内置后端名字 -> 惰性 loader（返回 VectorStore【类】，尚不实例化）。
# loader 仅在被调用时才 import 对应 adapter 模块（模块体零顶层 SDK import），SDK 留待
# adapter.__init__ 在【实例化】时延迟 import——故 core import store.py 与构建本表均零 SDK。
# 别名共指同一 loader（大小写 / 留白由 make_vector_store 归一化）。第三方后端【不】登记此表，
# 而是经 entry-point 自动发现（见 _discover_entry_points），无需任何核心 PR。
# ---------------------------------------------------------------------------
def _load_in_process() -> type[VectorStore]:
    return InProcessVectorStore


def _load_sqlite_vec() -> type[VectorStore]:
    from ragspine.retrieval.vector.adapters.sqlite_vec import SqliteVecVectorStore

    return SqliteVecVectorStore


def _load_pgvector() -> type[VectorStore]:
    from ragspine.retrieval.vector.adapters.pgvector import PgVectorVectorStore

    return PgVectorVectorStore


def _load_qdrant() -> type[VectorStore]:
    from ragspine.retrieval.vector.adapters.qdrant import QdrantVectorStore

    return QdrantVectorStore


_BUILTIN_LOADERS: dict[str, Callable[[], type[VectorStore]]] = {
    "in_process": _load_in_process,
    "in-process": _load_in_process,
    "inprocess": _load_in_process,
    "memory": _load_in_process,
    "sqlite_vec": _load_sqlite_vec,
    "sqlite-vec": _load_sqlite_vec,
    "sqlitevec": _load_sqlite_vec,
    "pgvector": _load_pgvector,
    "pg_vector": _load_pgvector,
    "qdrant": _load_qdrant,
}

# 错误信息中展示的内置规范名（别名不重复列出，保持可读）。
_BUILTIN_DISPLAY_NAMES = ("none", "in_process", "sqlite_vec", "pgvector", "qdrant")


def _discover_entry_points() -> Sequence[Any]:
    """发现第三方在 VECTOR_STORE_ENTRY_POINT_GROUP 下注册的 VectorStore 后端。

    返回若干 EntryPoint（各有 .name 与 .load()）。在函数内 import entry_points，使
    monkeypatch importlib.metadata.entry_points 在测试中生效，也让发现成本只在真正回落时付出。
    """
    from importlib.metadata import entry_points

    return list(entry_points(group=VECTOR_STORE_ENTRY_POINT_GROUP))


def _resolve_factory(normalized: str) -> Callable[..., VectorStore]:
    """归一化后的名字 -> 一个可 **kwargs 调用得到 VectorStore 的工厂（内置类或 entry-point 目标）。

    先查内置注册表（内置名字优先于同名 entry point，第三方不能劫持内置语义）；未命中再回落到
    entry-point 自动发现，按名字（同样大小写 / 留白不敏感）匹配后 .load()。两者皆不命中 ->
    ValueError，列出内置 + 已发现的 entry-point 名字。注意此函数只【解析】不【实例化】，故对内置
    adapter 不会触发其 SDK import——SDK 由返回类的 __init__ 在实例化时延迟 import。
    """
    loader = _BUILTIN_LOADERS.get(normalized)
    if loader is not None:
        return loader()
    discovered = _discover_entry_points()
    for entry_point in discovered:
        if entry_point.name.strip().lower() == normalized:
            factory: Callable[..., VectorStore] = entry_point.load()
            return factory
    names = sorted({entry_point.name for entry_point in discovered})
    raise ValueError(
        f"未知 vector store spec：{normalized!r}"
        f"（内置可选 {' / '.join(_BUILTIN_DISPLAY_NAMES)}；"
        f"已发现的 entry-point 后端：{names or '无'}；"
        f"第三方包可在 entry-point group {VECTOR_STORE_ENTRY_POINT_GROUP!r} 下注册一个后端）"
    )


def make_vector_store(spec: str | None = None, **kwargs: Any) -> VectorStore | None:
    """向量存储工厂：把「选哪个 store」从改代码降为一个 spec/env（范式同 make_embedding_backend）。

    spec 取值（大小写 / 留白不敏感；缺省读环境变量 RAGSPINE_VECTOR_STORE）：
        - None / 'none'                          -> None（不注入具体 store；检索器用内置内存默认）
        - 'in_process' / 'in-process' / 'memory' -> InProcessVectorStore（零依赖确定性内存默认）
        - 'sqlite_vec' / 'pgvector' / 'qdrant'   -> 对应 adapter（behind [vector] extra，延迟 import）
        - 其余                                    -> entry-point 自动发现（第三方包在
          VECTOR_STORE_ENTRY_POINT_GROUP 下注册即可被选中）；都不命中 -> ValueError 列出可选名字

    名字经注册表解析（内置 loader 或 entry point），再以 **kwargs 实例化；内置 adapter 的 SDK
    在实例化时才延迟 import（缺 [vector] extra 时由 adapter.__init__ 抛可执行的 pip 提示）。
    返回 VectorStore 实例或 None（可直接喂给 HybridRetriever / NarrativeIndex /
    build_narrative_retriever 的 vector_store 参数）。None 与显式 InProcessVectorStore 在
    检索结果上等价——前者让检索器自建内置默认，后者把同一个 store 实例交由调用方持有/复用。
    """
    if spec is None:
        spec = os.environ.get(VECTOR_STORE_ENV)
    normalized = (spec or "none").strip().lower()
    if normalized == "none":
        return None
    factory = _resolve_factory(normalized)
    return factory(**kwargs)
