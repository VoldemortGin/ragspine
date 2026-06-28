"""W11 SPLADE 学习稀疏（learned-sparse）：神经稀疏 term-expansion 向量（比 BM25 强、仍可解释，opt-in）。

现状（docs/prd-quality-depth.md W11）：稀疏侧只有 BM25。SPLADE 把每条文本扩成一个稀疏的「term_id ->
权重」向量（像 BM25 一样可解释——非零项即被激活的词，但权重学习而来、含同义/相关词扩展），打分用稀疏
点积。

本模块给学习稀疏一个【稀疏向量缝】+ 一个【作为 ListwiseJudge 接入 W2 精排链】的重排器：
- SparseEmbeddingBackend 协议：query/doc -> 稀疏向量（dict[term_id, weight]）。
- sparse_dot：确定性稀疏点积打分。零模型（给定向量）、确定。
- SpladeReranker：实现既有 ListwiseJudge 协议，用稀疏点积重排。作为 ListwiseJudge 走 listwise_rerank
  => **RESTRICTED 隔离继承**（编排已把 RESTRICTED 排除在 judge 之外）。经 make_reranker('splade') 选用。
- FastEmbedSpladeBackend：fastembed SparseTextEmbedding 适配器（prithivida/Splade_PP_en_v1），延迟
  import、归 [splade]。

**离线性诚实声明（同 W1/W2/ColBERT）**：fastembed 首次从 HF 下载 SPLADE 权重再缓存（"首拉后离线"）。
稀疏向量 store（as-retriever 全库检索）是 follow-up——本期先给「SPLADE-as-reranker」（W2 链）这一
CI 可测、落库零迁移的接入。**默认不变**：默认 hybrid（BM25 + W1 dense → RRF）不变；SPLADE 是 opt-in。
"""

import os
from typing import Any, Protocol, runtime_checkable

from corespine import lazy_extra_import

from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge

# 默认 SPLADE 模型。
DEFAULT_SPLADE_MODEL = "prithivida/Splade_PP_en_v1"
# 模型覆盖环境变量。
SPLADE_MODEL_ENV = "RAGSPINE_SPLADE_MODEL"


@runtime_checkable
class SparseEmbeddingBackend(Protocol):
    """稀疏向量嵌入缝：query/doc -> dict[term_id, weight]（非零项即激活词）。

    具体实现（fastembed SPLADE 等）延迟加载，核心只 import 此 Protocol。
    """

    def embed_query(self, query: str) -> dict[int, float]: ...

    def embed_documents(self, docs: list[str]) -> list[dict[int, float]]: ...


def sparse_dot(q: dict[int, float], d: dict[int, float]) -> float:
    """稀疏点积：仅在共同非零维上累加 q[t]*d[t]。空向量 -> 0.0。确定性。"""
    if not q or not d:
        return 0.0
    # 遍历较小者，查较大者——更省。
    small, large = (q, d) if len(q) <= len(d) else (d, q)
    return sum(w * large.get(t, 0.0) for t, w in small.items())


class FastEmbedSpladeBackend:
    """fastembed SparseTextEmbedding 适配器（实现 SparseEmbeddingBackend，延迟加载，归 [splade]）。

    __init__ 只记模型名、不 import fastembed（构造极轻）；模型首次 embed 时延迟下载并缓存。
    确定性：pin 模型 + fastembed 版本后 CPU 推理可复现。
    """

    def __init__(self, model_name: str = DEFAULT_SPLADE_MODEL, *, cache_dir: str | None = None):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            mod = lazy_extra_import("fastembed", pkg="ragspine", extra="splade")
            kwargs: dict[str, Any] = {}
            if self.cache_dir is not None:
                kwargs["cache_dir"] = self.cache_dir
            self._model = mod.SparseTextEmbedding(self.model_name, **kwargs)
        return self._model

    @staticmethod
    def _to_dict(sparse: Any) -> dict[int, float]:
        # fastembed SparseEmbedding 带 .indices（np int）与 .values（np float）。
        return {int(i): float(v) for i, v in zip(sparse.indices, sparse.values, strict=False)}

    def embed_query(self, query: str) -> dict[int, float]:
        model = self._load()
        return self._to_dict(next(iter(model.query_embed([query]))))

    def embed_documents(self, docs: list[str]) -> list[dict[int, float]]:
        if not docs:
            return []
        model = self._load()
        return [self._to_dict(s) for s in model.embed(docs)]


class SpladeReranker:
    """SPLADE 学习稀疏重排（实现 ListwiseJudge 协议）：稀疏点积打分 -> 候选降序名次。

    backend 默认 None -> 延迟构造 FastEmbedSpladeBackend。空候选返回 []，不触发加载。平分按原序——稳定。
    作为 ListwiseJudge 走 listwise_rerank => RESTRICTED 不进打分（隔离继承）。
    """

    def __init__(
        self,
        backend: SparseEmbeddingBackend | None = None,
        *,
        model_name: str = DEFAULT_SPLADE_MODEL,
        cache_dir: str | None = None,
    ):
        self._backend = backend
        self.model_name = model_name
        self.cache_dir = cache_dir

    def _backend_or_default(self) -> SparseEmbeddingBackend:
        if self._backend is None:
            self._backend = FastEmbedSpladeBackend(self.model_name, cache_dir=self.cache_dir)
        return self._backend

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        cands = list(candidates)
        if not cands:
            return []
        backend = self._backend_or_default()
        q_vec = backend.embed_query(query)
        doc_vecs = backend.embed_documents(cands)
        if len(doc_vecs) != len(cands):
            raise RuntimeError(
                f"embed_documents 返回 {len(doc_vecs)} 条与候选数 {len(cands)} 不一致"
                f"（model={self.model_name}）"
            )
        scores = [sparse_dot(q_vec, dv) for dv in doc_vecs]
        return sorted(range(len(cands)), key=lambda i: scores[i], reverse=True)


def make_splade_reranker(**kwargs: Any) -> ListwiseJudge:
    """SPLADE 重排器工厂（供 make_reranker('splade') 分派）：缺省读 RAGSPINE_SPLADE_MODEL。"""
    if "model_name" not in kwargs:
        env_model = os.environ.get(SPLADE_MODEL_ENV)
        if env_model:
            kwargs["model_name"] = env_model
    return SpladeReranker(**kwargs)
