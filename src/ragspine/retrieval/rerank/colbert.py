"""ColBERT 晚交互重排（W11）：fastembed LateInteractionTextEmbedding 实现 ListwiseJudge（MaxSim 打分）。

现状（docs/prd-quality-depth.md W11）：检索表示只有单向量 dense（W1 ONNX MiniLM）+ BM25 → RRF，
没有 token 级晚交互（ColBERT 多向量 MaxSim，比单向量 dense 精度高一档）。本模块给 ⭐ 检索表示补上
这一档——作【重排器】（对 base 检索的候选做 MaxSim 重打分，对标 LlamaIndex ColbertRerank），最小改动、
复用 W2 的 listwise_rerank 编排缝与 make_reranker 工厂，不需多向量索引（多向量检索后端 = follow-up）。

ColbertReranker：真本地晚交互重排后端，实现既有 ListwiseJudge 协议（judge(query,candidates)->名次）。
经 fastembed 的 LateInteractionTextEmbedding（Apache-2.0，内置 ONNX 权重 + onnxruntime，纯 CPU、
确定性）把 query 与每个候选各嵌成 token 级【多向量矩阵】，按 MaxSim（query 每个 token 取其对任一
doc token 的最大 cosine，求和）给每个候选打分，再按分降序给出候选下标（平分按原 RRF 序——确定性）。
延迟 import、惰性加载，归 [colbert] extra（复用 fastembed，与 W1/W2 同范式，过 ADR 0009 ≤Apache-2.0
许可门）。默认模型 colbert-ir/colbertv2.0（Apache-2.0，2025 前沿 ColBERTv2）。

maxsim：晚交互打分纯函数（sum over query tokens of max cosine to any doc token）——可单测的核心。

工厂：ColbertReranker 经 make_reranker（ragspine.retrieval.rerank.cross_encoder，reranker 缝的统一
工厂）以 'colbert' / 'colbertv2' / 'late_interaction' 选用；'none'（默认）不重排、字节不变。模型名可经
kwargs 或 RAGSPINE_COLBERT_MODEL 覆盖。

**离线性诚实声明（同 W1 OnnxEmbeddingBackend / W2 CrossEncoderReranker，不可省略）**：fastembed 首次会
从 HuggingFace 下载 ONNX 权重再缓存（"首拉后离线"，不是首跑即离线）。真正的"首跑即离线"需把权重做成
数据包随包出货——follow-up（见 docs/prd-quality-depth.md），本后端先给"首拉后离线 + 确定性"的真重排默认。

**默认行为不变**：未配置（spec='none'，ServiceConfig 默认）时 make_reranker 返回 None，
build_narrative_retriever 退回原 judge（identity/RRF 或注入的 ProviderListwiseJudge）——默认 loop 字节
不变。ColBERT 是 opt-in。

**隔离不变量继承**：ColBERT 作为 ListwiseJudge 走 listwise_rerank 编排，而后者已把
sensitivity==RESTRICTED 的候选排除在 judge 之外（不送进打分、原位保留）——故 RESTRICTED 文本绝不进入
ColBERT 打分（conformance 见 tests/retrieval/rerank/test_colbert_isolation.py，含 reverse-proof）。
"""

import math
from collections.abc import Sequence
from typing import Any

from corespine import lazy_extra_import

# 默认 ColBERT 模型名（唯一出处，改这里即全局生效）。选 colbert-ir/colbertv2.0：
# Apache-2.0（过 ADR 0009 ≤Apache-2.0 许可门）、ONNX、纯 CPU、确定性、2025 前沿 ColBERTv2 晚交互。
DEFAULT_COLBERT_MODEL = "colbert-ir/colbertv2.0"

# ColBERT 模型覆盖环境变量名（缺省模型时生效，仅对 colbert 系 spec，由 make_reranker 注入）。
COLBERT_MODEL_ENV = "RAGSPINE_COLBERT_MODEL"

# 默认嵌入分批大小（透传给 LateInteractionTextEmbedding.embed）。
DEFAULT_COLBERT_BATCH_SIZE = 32


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度；零向量一律 0.0（口径同 retrieval.cosine_similarity / vector.store._cosine）。

    本模块内复刻以保持零互依赖（同 embedding_backends._tokenize / store._cosine 的做法）。
    ColBERT token 向量已 L2 归一化，dot 即 cosine；此处仍归一化以对零向量鲁棒、口径统一。
    """
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def maxsim(
    query_vectors: Sequence[Sequence[float]],
    doc_vectors: Sequence[Sequence[float]],
) -> float:
    """ColBERT 晚交互 MaxSim：query 每个 token 取其对任一 doc token 的最大 cosine，求和。

    MaxSim(Q, D) = Σ_{i∈Q} max_{j∈D} cos(q_i, d_j)

    query_vectors / doc_vectors 均为 token 级向量矩阵（每行一个 token 的向量）。空 query 或空 doc
    -> 0.0（不抛）；零向量按 cosine 口径贡献 0。纯函数、确定性——晚交互打分的核心，可脱离 fastembed 单测。
    """
    if not query_vectors or not doc_vectors:
        return 0.0
    total = 0.0
    for q in query_vectors:
        total += max(_cosine(q, d) for d in doc_vectors)
    return total


def _to_matrix(rows: Any) -> list[list[float]]:
    """把后端返回的一篇文本的 token 矩阵（numpy 2D array 或 list）归一成 list[list[float]]。"""
    return [[float(x) for x in row] for row in rows]


class ColbertReranker:
    """本地 ColBERT 晚交互重排（实现 ListwiseJudge 协议，fastembed 运行时延迟 import）。

    judge(query, candidates) 把 query 与每个候选各嵌成 token 级多向量矩阵，按 MaxSim 打分，再按分
    降序返回候选下标（0-based 的一个排列）；平分时保持原（RRF）序——稳定排序，确定性。

    设计约束（延迟 import 范式同 W1 OnnxEmbeddingBackend / W2 CrossEncoderReranker）：
    - __init__ 只校验参数、记下模型名，**不** import fastembed、不加载模型——构造极轻，没装 [colbert]
      的机器上也能构造对象（工厂命名 / 单测）；
    - 模型在首次 judge 时延迟下载并缓存（fastembed 调 HF），之后复用缓存；
    - 空候选直接返回 []，不触发任何 import / 下载；
    - 返回矩阵条数与候选数一致性校验，异常即抛，绝不静默给坏名次。

    **确定性**：pin 模型名 + fastembed 版本后，CPU onnxruntime 推理逐位可复现 → 同输入同名次
    （conformance 见 tests 的 @pytest.mark.network 用例）。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_COLBERT_MODEL,
        *,
        cache_dir: str | None = None,
        batch_size: int = DEFAULT_COLBERT_BATCH_SIZE,
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
        """首次调用时延迟 import fastembed 并构造 LateInteractionTextEmbedding（之后复用缓存）。"""
        if self._model is None:
            # 重依赖延迟 import：缺失时由 corespine.lazy_extra_import 翻成
            # `pip install ragspine[colbert]` 友好提示（离线/不重排场景无需安装）。
            fastembed = lazy_extra_import("fastembed", pkg="ragspine", extra="colbert")
            kwargs: dict[str, Any] = {}
            if self.cache_dir is not None:
                kwargs["cache_dir"] = self.cache_dir
            if self.threads is not None:
                kwargs["threads"] = self.threads
            self._model = fastembed.LateInteractionTextEmbedding(
                model_name=self.model_name, **kwargs
            )
        return self._model

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        """query 对每个候选按 MaxSim 打分 -> 按分降序的候选下标（平分按原序）。空候选返回 []，不触发加载。"""
        cands = list(candidates)
        if not cands:
            return []

        model = self._load_model()
        # query 走 query_embed（ColBERT query/doc 前缀有别），候选走 embed；两者均返回生成器。
        query_matrix = _to_matrix(next(iter(model.query_embed(query))))
        doc_matrices = [_to_matrix(m) for m in model.embed(cands, batch_size=self.batch_size)]
        if len(doc_matrices) != len(cands):
            raise RuntimeError(
                f"colbert embed 返回矩阵条数 {len(doc_matrices)} 与候选数 {len(cands)} 不一致"
                f"（model={self.model_name}）"
            )
        scores = [maxsim(query_matrix, dm) for dm in doc_matrices]
        # 稳定降序：sorted(reverse=True) 对平分保持原（RRF）序——确定性。
        return sorted(range(len(cands)), key=lambda i: scores[i], reverse=True)
