"""SPLADE 学习稀疏重排（W11）：fastembed SparseTextEmbedding 实现 ListwiseJudge（稀疏点积打分）。

现状（docs/prd-quality-depth.md W11）：检索表示的稀疏信号只有 BM25（词频/idf）。SPLADE 是神经【学习
稀疏】——把 query/文档扩展成带权重的 term-expansion 稀疏向量（比 BM25 强且同样可解释）。本模块给
⭐ 检索表示补上这一档——作【稀疏打分信号】重排候选（query 稀疏向量 vs 候选稀疏向量点积），最小改动、
复用 W2 的 listwise_rerank 编排缝与 make_reranker 工厂，不需稀疏倒排索引（与 BM25 并列经 RRF 融合的
稀疏检索后端 = follow-up）。

SpladeReranker：真本地学习稀疏重排后端，实现既有 ListwiseJudge 协议（judge(query,candidates)->名次）。
经 fastembed 的 SparseTextEmbedding（Apache-2.0，内置 ONNX 权重 + onnxruntime，纯 CPU、确定性）把
query 与每个候选各嵌成【稀疏向量】（term id -> 权重），按共享维度点积给每个候选打分，再按分降序给出
候选下标（平分按原 RRF 序——确定性）。延迟 import、惰性加载，归 [splade] extra（复用 fastembed，与
W1/W2 同范式，过 ADR 0009 ≤Apache-2.0 许可门）。默认模型 prithivida/Splade_PP_en_v1（Apache-2.0）。

sparse_dot：学习稀疏打分纯函数（两稀疏向量共享 term 维度上 value 乘积之和）——可单测的核心。

工厂：SpladeReranker 经 make_reranker（ragspine.retrieval.rerank.cross_encoder，reranker 缝的统一
工厂）以 'splade' / 'splade_pp' / 'learned_sparse' 选用；'none'（默认）不重排、字节不变。模型名可经
kwargs 或 RAGSPINE_SPLADE_MODEL 覆盖。

**离线性诚实声明（同 W1 OnnxEmbeddingBackend / W2 CrossEncoderReranker，不可省略）**：fastembed 首次会
从 HuggingFace 下载 ONNX 权重再缓存（"首拉后离线"，不是首跑即离线）。真正的"首跑即离线"需把权重做成
数据包随包出货——follow-up（见 docs/prd-quality-depth.md），本后端先给"首拉后离线 + 确定性"的真打分默认。

**默认行为不变**：未配置（spec='none'，ServiceConfig 默认）时 make_reranker 返回 None，
build_narrative_retriever 退回原 judge（identity/RRF 或注入的 ProviderListwiseJudge）——默认 loop 字节
不变。SPLADE 是 opt-in。

**隔离不变量继承**：SPLADE 作为 ListwiseJudge 走 listwise_rerank 编排，而后者已把
sensitivity==RESTRICTED 的候选排除在 judge 之外（不送进打分、原位保留）——故 RESTRICTED 文本绝不进入
SPLADE 打分（conformance 见 tests/retrieval/rerank/test_splade_isolation.py，含 reverse-proof）。
"""

from collections.abc import Mapping
from typing import Any

from corespine import lazy_extra_import

# 默认 SPLADE 模型名（唯一出处，改这里即全局生效）。选 prithivida/Splade_PP_en_v1：
# Apache-2.0（过 ADR 0009 ≤Apache-2.0 许可门）、ONNX、纯 CPU、确定性——轻量学习稀疏默认。
DEFAULT_SPLADE_MODEL = "prithivida/Splade_PP_en_v1"

# SPLADE 模型覆盖环境变量名（缺省模型时生效，仅对 splade 系 spec，由 make_reranker 注入）。
SPLADE_MODEL_ENV = "RAGSPINE_SPLADE_MODEL"

# 默认嵌入分批大小（透传给 SparseTextEmbedding.embed）。
DEFAULT_SPLADE_BATCH_SIZE = 32


def sparse_dot(query: Mapping[int, float], doc: Mapping[int, float]) -> float:
    """SPLADE 学习稀疏打分：两稀疏向量共享 term 维度上 value 乘积之和（稀疏点积）。

    score(q, d) = Σ_{t∈ q∩d} q[t] · d[t]

    query / doc 均为 {term_id: weight} 稀疏向量。任一为空或无共享维度 -> 0.0（不抛）；迭代较小者
    以省时，不改结果（点积对称）。纯函数、确定性——学习稀疏打分的核心，可脱离 fastembed 单测。
    """
    if not query or not doc:
        return 0.0
    if len(query) > len(doc):
        query, doc = doc, query  # 迭代较小者（点积对称，结果不变）
    return sum(weight * doc.get(term, 0.0) for term, weight in query.items())


def _to_terms(sparse: Any) -> dict[int, float]:
    """把后端返回的一篇文本的 SparseEmbedding（indices + values 两平行序列）归一成 {term_id: weight}。"""
    return {int(i): float(v) for i, v in zip(sparse.indices, sparse.values, strict=False)}


class SpladeReranker:
    """本地 SPLADE 学习稀疏重排（实现 ListwiseJudge 协议，fastembed 运行时延迟 import）。

    judge(query, candidates) 把 query 与每个候选各嵌成稀疏向量（term id -> 权重），按共享维度点积
    打分，再按分降序返回候选下标（0-based 的一个排列）；平分时保持原（RRF）序——稳定排序，确定性。

    设计约束（延迟 import 范式同 W1 OnnxEmbeddingBackend / W2 CrossEncoderReranker）：
    - __init__ 只校验参数、记下模型名，**不** import fastembed、不加载模型——构造极轻，没装 [splade]
      的机器上也能构造对象（工厂命名 / 单测）；
    - 模型在首次 judge 时延迟下载并缓存（fastembed 调 HF），之后复用缓存；
    - 空候选直接返回 []，不触发任何 import / 下载；
    - 返回稀疏向量条数与候选数一致性校验，异常即抛，绝不静默给坏名次。

    **确定性**：pin 模型名 + fastembed 版本后，CPU onnxruntime 推理逐位可复现 → 同输入同名次
    （conformance 见 tests 的 @pytest.mark.network 用例）。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_SPLADE_MODEL,
        *,
        cache_dir: str | None = None,
        batch_size: int = DEFAULT_SPLADE_BATCH_SIZE,
        threads: int | None = None,
    ):
        if batch_size < 1:
            raise ValueError(f"batch_size 必须 >= 1，得到 {batch_size}")
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.batch_size = batch_size
        self.threads = threads
        self._model: Any = None  # 延迟加载缓存

    def _load_model(self) -> Any:
        """首次调用时延迟 import fastembed 并构造 SparseTextEmbedding（之后复用缓存）。"""
        if self._model is None:
            # 重依赖延迟 import：缺失时由 corespine.lazy_extra_import 翻成
            # `pip install ragspine[splade]` 友好提示（离线/不重排场景无需安装）。
            fastembed = lazy_extra_import("fastembed", pkg="ragspine", extra="splade")
            kwargs: dict[str, Any] = {}
            if self.cache_dir is not None:
                kwargs["cache_dir"] = self.cache_dir
            if self.threads is not None:
                kwargs["threads"] = self.threads
            self._model = fastembed.SparseTextEmbedding(model_name=self.model_name, **kwargs)
        return self._model

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        """query 对每个候选按稀疏点积打分 -> 按分降序的候选下标（平分按原序）。空候选返回 []，不触发加载。"""
        cands = list(candidates)
        if not cands:
            return []

        model = self._load_model()
        # query 走 query_embed，候选走 embed；两者均返回 SparseEmbedding 生成器。
        query_terms = _to_terms(next(iter(model.query_embed(query))))
        doc_terms = [_to_terms(e) for e in model.embed(cands, batch_size=self.batch_size)]
        if len(doc_terms) != len(cands):
            raise RuntimeError(
                f"splade embed 返回稀疏向量条数 {len(doc_terms)} 与候选数 {len(cands)} 不一致"
                f"（model={self.model_name}）"
            )
        scores = [sparse_dot(query_terms, dt) for dt in doc_terms]
        # 稳定降序：sorted(reverse=True) 对平分保持原（RRF）序——确定性。
        return sorted(range(len(cands)), key=lambda i: scores[i], reverse=True)
