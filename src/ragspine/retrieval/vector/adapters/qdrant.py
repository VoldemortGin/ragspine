"""Qdrant 适配器：把 VectorStore 缝落到 Qdrant（HNSW 向量库）的 LOCAL 模式（进程内 / 零服务）。

第三个真实 VectorStore adapter（落地 docs/prd-vector-store-seam.md 的 adapter roadmap #3），
也是【第一个 approximate 后端】——它带来 conformance 的「exact vs approximate」能力旗标。
驱动用 **qdrant-client**（Apache-2.0，permissive，过 ADR 0009 的 ≤Apache-2.0 许可门，同 sqlite-vec）。
延迟 import，behind `[vector]` extra；零依赖默认仍是 InProcessVectorStore。

LOCAL 模式（无服务、纯进程内，conformance 整套在进程里跑、不连任何服务器）：
  - QdrantClient(location=":memory:")  —— 临时实例（conformance 各实例天然隔离、零残留）。
  - QdrantClient(path=...)             —— 本地落盘的命名 collection（跨进程持久，重开见数据 + 血缘）。

**为何归类 approximate（而非 exact）**：Qdrant 的生产特征是 HNSW 近似最近邻；local 模式只是
【偶然】精确。本适配器对外的【保证】是 approximate——这是诚实的合约，也正是引入能力旗标的理由：
conformance 对 approximate 后端只断言较弱的确定性保证（同实例重复调用顺序稳定 + recall@k 下限），
不把「逐位 byte-identical / id 升序破平分」钉死，从而未来切到原生 HNSW KNN 不会被合约误伤。
而 provenance / isolation / where 过滤下推三项不变量【对 approximate 后端照样全量绑定】，绝不松动。

打分走【native HNSW search 收窄候选池 + Python 精确重排】（落地 PRD 的 Native ANN/KNN）：
  - 候选池 pool = max(k, ef_search) 但至少 min(count, pool_ceiling)（见 store._pool_size）；
    用 query_points(limit=pool, query_filter=…, search_params=hnsw_ef) 经 HNSW 索引取候选池 +
    把 where 下推为 payload 过滤（`__meta__.<key>` 嵌套精确匹配，缺键即排除，隔离下推 + 减传输）；
    再用 store._rerank 在 Python 以精确 cosine 重排（复用 _cosine / _matches）定 top-k。
  - collection 用 Distance.DOT（不归一化、原样存取），故 native search 按点积收窄、Python 按 cosine
    重排——这正是 Qdrant 归类 approximate 的诚实之处（点积近邻未必等于 cosine 近邻）；小库（含全部
    conformance 库）下 pool >= count，候选覆盖全部行 -> 精确重排【偶然】精确，但合约只担 approximate
    的较弱保证（重复调用顺序稳定 + recall@k 下限），故未来切真·HNSW 收窄不会被合约误伤。
delete 仍走全量 scroll（删除非热路径、需取全部匹配）。ef_search / pool_ceiling 是本 adapter 自有
旋钮（默认值见 __init__），不进核心 VectorStore Protocol。

point id 映射：Qdrant 的 point id 只接受 uint / UUID，而 chunk_id 是字符串——用 UUID5(chunk_id)
做确定性映射（同 chunk_id 恒得同 point id，故 upsert 按 id 替换），原始字符串 id 存进 payload，
VectorHit.id 原样回传【原始字符串】。metadata 存进 payload 的 __meta__，血缘原样存取。

诚实边界（持久化）：path= 落盘即把向量（含 RESTRICTED 块的衍生向量）写入本地 Qdrant 存储——
与 sqlite-vec 落盘同属 at-rest 衍生面（向量在自有库、不进 prompt）；默认 :memory: 无此问题，
落盘护栏见 docs/prd-vector-store-seam.md 的持久化增量。
"""

import uuid
import weakref
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from ragspine.retrieval.vector.store import (
    DEFAULT_QUERY_K,
    VectorHit,
    VectorRecord,
    _matches,
    _pool_size,
    _rerank,
)

# point id 命名空间：UUID5(chunk_id) 的确定性映射根（同 chunk_id 恒得同 point id）。
_POINT_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "ragspine.retrieval.vector.qdrant")
# payload 保留键：原始字符串 id 与 metadata（双下划线，避开 metadata 字段名碰撞）。
_ID_KEY = "__id__"
_META_KEY = "__meta__"


def _point_id(chunk_id: str) -> str:
    """字符串 chunk_id -> 确定性 UUID5 point id（Qdrant point id 只接受 uint / UUID）。"""
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, chunk_id))


class QdrantVectorStore:
    """Qdrant（local 模式）后端的 VectorStore 实现（conformance-bound，approximate 能力旗标）。

    维度不变式同 InProcessVectorStore：库内向量共享单一维度，首条 upsert 定维（按观测维度建
    collection——Qdrant 要求 collection 向量尺寸固定）；此后任何长度不符的向量（同批 / 跨批）或
    维度不符的查询向量抛 ValueError。空库查询返回 []。
    """

    def __init__(
        self,
        path: str | None = None,
        collection: str = "rv_items",
        *,
        ef_search: int = 0,
        pool_ceiling: int = 4096,
    ) -> None:
        try:
            from qdrant_client import QdrantClient, models
        except ImportError as exc:  # pragma: no cover - 仅未装 [vector] 时触发
            raise ImportError(
                "未安装 qdrant-client：pip install 'ragspine[vector]' 或 pip install qdrant-client；"
                "离线/纯内存场景用默认 InProcessVectorStore 即可，无需安装。"
            ) from exc

        # native HNSW search 候选池旋钮（本 adapter 自有，不进核心 Protocol）：ef_search 放大候选广度
        # （0 即用 Qdrant 默认 hnsw_ef），pool_ceiling 封顶大库的池大小（小于库则触发 HNSW 收窄、再精确重排）。
        self._ef_search = ef_search
        self._pool_ceiling = pool_ceiling
        self._models = models
        # path=None -> 临时 :memory:（conformance 隔离）；否则本地落盘命名 collection（跨进程持久）。
        self._client = (
            QdrantClient(path=path) if path is not None else QdrantClient(location=":memory:")
        )
        self._collection = collection
        self._dim: int | None = None
        # 确定性资源回收（同 sqlite-vec / pgvector）：GC 时兜底关 client，免 ResourceWarning 被零警告门升级。
        self._finalizer = weakref.finalize(self, self._client.close)
        self._restore_dim()

    def _restore_dim(self) -> None:
        """持久化重开时从已存在 collection 的 VectorParams.size 恢复维度（:memory: 必为新建，跳过）。"""
        if self._client.collection_exists(self._collection):
            params = self._client.get_collection(self._collection).config.params.vectors
            # 单匿名向量配置下 params 为 VectorParams（带 .size）；用 getattr 取以容纳类型联合。
            size = getattr(params, "size", None)
            if size:
                self._dim = int(size)

    def _ensure_collection(self, dim: int) -> None:
        if self._dim is None:
            if not self._client.collection_exists(self._collection):
                self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=self._models.VectorParams(
                        size=dim, distance=self._models.Distance.DOT
                    ),
                )
            self._dim = dim

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        """写入记录；同 id 为替换（UUID5(chunk_id) 同 id -> 同 point id -> Qdrant 原生替换）。

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
        self._ensure_collection(expected)
        points = [
            self._models.PointStruct(
                id=_point_id(record.id),
                vector=[float(x) for x in record.vector],
                payload={_ID_KEY: record.id, _META_KEY: dict(record.metadata)},
            )
            for record in records
        ]
        self._client.upsert(collection_name=self._collection, points=points)
        return len(records)

    def _iter_points(self) -> Iterator[Any]:
        """全量 scroll 回所有点（带 payload + 向量），分页直至取尽（offset 为 None 即结束）。"""
        offset = None
        while True:
            batch, offset = self._client.scroll(
                collection_name=self._collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            yield from batch
            if offset is None:
                break

    def _where_filter(self, where: Mapping[str, str] | None) -> Any:
        """where dict -> Qdrant payload 过滤（`__meta__.<key>` 嵌套精确匹配的 AND；缺键即排除，同 _matches）。"""
        if not where:
            return None
        return self._models.Filter(
            must=[
                self._models.FieldCondition(
                    key=f"{_META_KEY}.{key}",
                    match=self._models.MatchValue(value=value),
                )
                for key, value in where.items()
            ]
        )

    def query(
        self,
        vector: Sequence[float],
        *,
        k: int = DEFAULT_QUERY_K,
        where: Mapping[str, str] | None = None,
    ) -> list[VectorHit]:
        """native HNSW search 收窄候选池 + Python 精确重排：返回 cosine 降序 top-k、平分按 id 升序。

        口径与 InProcessVectorStore 完全一致（重排复用 _cosine / _matches）：空库 -> []；非空库查询
        向量维度须与库内一致否则 ValueError；零向量 cosine 处处 0，不崩（Distance.DOT 对零向量正常）。
        where 下推为 payload 过滤（缺键即排除），再在精确重排里复核；小库 pool >= count 故候选覆盖全部行。
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
        search_params = (
            self._models.SearchParams(hnsw_ef=self._ef_search) if self._ef_search > 0 else None
        )
        response = self._client.query_points(
            collection_name=self._collection,
            query=[float(x) for x in vector],
            limit=pool,
            query_filter=self._where_filter(where),
            with_payload=True,
            with_vectors=True,
            search_params=search_params,
        )
        # point / payload 取 Any（同 _iter_points 的 Iterator[Any] 风格）：ScoredPoint.vector 的类型
        # 联合含 None，这里运行时必为带向量、带 payload 的点（with_vectors / with_payload=True），故按 Any 取用。
        points: list[Any] = list(response.points)
        candidates: list[tuple[str, Sequence[float], Mapping[str, str]]] = []
        for point in points:
            payload: Any = point.payload or {}
            stored = tuple(float(x) for x in point.vector)
            candidates.append((payload.get(_ID_KEY), stored, payload.get(_META_KEY, {})))
        return _rerank(candidates, vector, k=k, where=where)

    def delete(self, *, where: Mapping[str, str]) -> int:
        """删除所有匹配 where 的记录（语义同 query 的过滤）；返回删除条数。"""
        if self._dim is None:
            return 0
        victims = [
            point.id
            for point in self._iter_points()
            if _matches((point.payload or {}).get(_META_KEY, {}), where)
        ]
        if victims:
            self._client.delete(
                collection_name=self._collection,
                points_selector=self._models.PointIdsList(points=victims),
            )
        return len(victims)

    def count(self) -> int:
        """库内记录数（upsert / delete 的净效果）。"""
        if self._dim is None:
            return 0
        return int(self._client.count(collection_name=self._collection).count)

    def close(self) -> None:
        self._finalizer()  # 幂等：关 client（释放 path= 锁）并注销 finalizer，重复调用安全
