"""W11 ColBERT 晚交互（late-interaction / 多向量 MaxSim）：单向量 dense 之上的精度层（opt-in）。

现状（docs/prd-quality-depth.md W11）：检索是单向量 dense（W1 ONNX MiniLM）+ BM25 → RRF。无 token 级
多向量晚交互（ColBERT 式 MaxSim：query 每个 token 取其对 doc 任一 token 的最大相似，再求和）——这是
单向量 cosine 表达不了的更细粒度匹配。

本模块给晚交互一个【多向量缝】+ 一个【作为 ListwiseJudge 接入 W2 精排链】的重排器：
- MultiVectorBackend 协议：query/doc -> token 级多向量（list[token vec]）。
- max_sim：确定性 MaxSim 打分（∑_q max_d cosine(q,d)）。零模型（给定向量）、确定。
- ColBERTReranker：实现既有 ListwiseJudge 协议（judge(query, candidates)->名次），用 MaxSim 打分重排。
  作为 ListwiseJudge 走 listwise_rerank 编排 => **RESTRICTED 隔离继承**（编排已把 RESTRICTED 排除在
  judge 之外，与 cross_encoder 同）。可经 make_reranker('colbert') 选用。
- FastEmbedColBERTBackend：fastembed LateInteractionTextEmbedding 适配器（colbert-ir/colbertv2.0，
  Apache-2.0，过 ADR 0009 许可门），延迟 import、归 [colbert]。

**离线性诚实声明（同 W1/W2）**：fastembed 首次会从 HuggingFace 下载 ColBERT 权重再缓存（"首拉后离线"）。
真正"首跑即离线"的权重数据包是 follow-up。多向量索引（PLAID/Vespa 式 as-retriever）也是 follow-up——
本期先给「ColBERT-as-reranker」（W2 链）这一 CI 可测、落库零迁移的接入。

**默认不变**：默认 hybrid（W1 单向量 dense + BM25 → RRF）不变；ColBERT 是 opt-in 重排后端。
"""

import math
import os
from typing import Any, Protocol, runtime_checkable

from corespine import lazy_extra_import

from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge

# 默认 ColBERT 模型（Apache-2.0，过 ADR 0009 ≤Apache-2.0 许可门）。
DEFAULT_COLBERT_MODEL = "colbert-ir/colbertv2.0"
# 模型覆盖环境变量。
COLBERT_MODEL_ENV = "RAGSPINE_COLBERT_MODEL"


@runtime_checkable
class MultiVectorBackend(Protocol):
    """多向量（token 级）嵌入缝：query/doc -> list[token 向量]。

    具体实现（fastembed ColBERT 等）延迟加载，核心只 import 此 Protocol。
    """

    def embed_query(self, query: str) -> list[list[float]]: ...

    def embed_documents(self, docs: list[str]) -> list[list[list[float]]]: ...


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def max_sim(query_vecs: list[list[float]], doc_vecs: list[list[float]]) -> float:
    """ColBERT MaxSim：query 每个 token 取其对 doc 任一 token 的最大 cosine，再求和。

    空 query / 空 doc -> 0.0。确定性（给定向量）。
    """
    if not query_vecs or not doc_vecs:
        return 0.0
    return sum(max(_cosine(q, d) for d in doc_vecs) for q in query_vecs)


class FastEmbedColBERTBackend:
    """fastembed LateInteractionTextEmbedding 适配器（实现 MultiVectorBackend，延迟加载，归 [colbert]）。

    __init__ 只记模型名、不 import fastembed、不加载模型（构造极轻，没装 [colbert] 也能构造 / auto 探测）；
    模型在首次 embed 时延迟下载并缓存。确定性：pin 模型 + fastembed 版本后 CPU 推理可复现。
    """

    def __init__(self, model_name: str = DEFAULT_COLBERT_MODEL, *, cache_dir: str | None = None):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            mod = lazy_extra_import("fastembed", pkg="ragspine", extra="colbert")
            kwargs: dict[str, Any] = {}
            if self.cache_dir is not None:
                kwargs["cache_dir"] = self.cache_dir
            self._model = mod.LateInteractionTextEmbedding(self.model_name, **kwargs)
        return self._model

    @staticmethod
    def _to_lists(arr: Any) -> list[list[float]]:
        # fastembed 返回 numpy 2D（n_tokens, dim）；统一转 list[list[float]]。
        return [[float(x) for x in row] for row in arr]

    def embed_query(self, query: str) -> list[list[float]]:
        model = self._load()
        return self._to_lists(next(iter(model.query_embed([query]))))

    def embed_documents(self, docs: list[str]) -> list[list[list[float]]]:
        if not docs:
            return []
        model = self._load()
        return [self._to_lists(arr) for arr in model.embed(docs)]


class ColBERTReranker:
    """ColBERT 晚交互重排（实现 ListwiseJudge 协议）：MaxSim 打分 -> 候选降序名次。

    backend 默认 None -> 延迟构造 FastEmbedColBERTBackend（首次 judge 时加载）。空候选返回 []，不触发加载。
    平分按原（RRF）序——稳定、确定。作为 ListwiseJudge 走 listwise_rerank => RESTRICTED 不进打分（隔离继承）。
    """

    def __init__(
        self,
        backend: MultiVectorBackend | None = None,
        *,
        model_name: str = DEFAULT_COLBERT_MODEL,
        cache_dir: str | None = None,
    ):
        self._backend = backend
        self.model_name = model_name
        self.cache_dir = cache_dir

    def _backend_or_default(self) -> MultiVectorBackend:
        if self._backend is None:
            self._backend = FastEmbedColBERTBackend(self.model_name, cache_dir=self.cache_dir)
        return self._backend

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        cands = list(candidates)
        if not cands:
            return []
        backend = self._backend_or_default()
        q_vecs = backend.embed_query(query)
        doc_vecs = backend.embed_documents(cands)
        if len(doc_vecs) != len(cands):
            raise RuntimeError(
                f"embed_documents 返回 {len(doc_vecs)} 条与候选数 {len(cands)} 不一致"
                f"（model={self.model_name}）"
            )
        scores = [max_sim(q_vecs, dv) for dv in doc_vecs]
        return sorted(range(len(cands)), key=lambda i: scores[i], reverse=True)


def make_colbert_reranker(**kwargs: Any) -> ListwiseJudge:
    """ColBERT 重排器工厂（供 make_reranker('colbert') 分派）：缺省读 RAGSPINE_COLBERT_MODEL。"""
    if "model_name" not in kwargs:
        env_model = os.environ.get(COLBERT_MODEL_ENV)
        if env_model:
            kwargs["model_name"] = env_model
    return ColBERTReranker(**kwargs)
