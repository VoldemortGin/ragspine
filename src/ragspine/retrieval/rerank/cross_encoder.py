"""本地 cross-encoder 重排（W2）：fastembed TextCrossEncoder 实现 ListwiseJudge + reranker 工厂。

现状（docs/prd-quality-depth.md W2）：rerank 只有 LLM listwise（narrative_link.ProviderListwiseJudge，
需联网/非确定），离线默认是 identity（RRF 序，listwise_rerank 的 fallback）。本模块给离线 ⭐ 重排
阶段一个真正的大脑——一个轻量、确定性、纯 CPU 的本地 cross-encoder。

CrossEncoderReranker：真本地重排后端，实现既有 ListwiseJudge 协议（judge(query, candidates)->名次）。
经 fastembed 的 TextCrossEncoder（Apache-2.0，内置 ONNX 权重 + onnxruntime，纯 CPU、确定性）对每个
候选打 query-候选相关性分，再按分降序给出候选下标（平分按原 RRF 序——确定性）。延迟 import、惰性加载，
归 [rerank] extra（复用 fastembed，与 W1 的 [embed-onnx] 同范式，过 ADR 0009 ≤Apache-2.0 许可门）。
默认模型 Xenova/ms-marco-MiniLM-L-6-v2（Apache-2.0、约 80MB）。

make_reranker：reranker 选型工厂（reranker 缝的统一工厂，把「换重排大脑」从改代码降为一个 spec 字符串 / env）。
'none'＝不重排（默认，保持 identity/RRF 或注入的 LLM judge，行为不变）；'cross_encoder' 系＝本地
cross-encoder；'colbert' 系＝ColBERT 晚交互 MaxSim 重排（W11，colbert.py）；'splade' 系＝SPLADE 学习
稀疏点积重排（W11，splade.py）；'auto'＝装了 fastembed 就用本地 cross-encoder、否则回落 None（不重排）。
与 W1 make_embedding_backend 的 none/auto 范式一致。三种本地重排大脑（cross-encoder / ColBERT / SPLADE）
都实现 ListwiseJudge、都走同一 listwise_rerank 编排缝（RESTRICTED 不出域一致继承），故共用本工厂选型。

**离线性诚实声明（同 W1 OnnxEmbeddingBackend，不可省略）**：fastembed 首次会从 HuggingFace 下载 ONNX
权重再缓存（"首拉后离线"，不是首跑即离线）。真正的"首跑即离线"需把权重做成数据包随包出货——follow-up
（见 docs/prd-quality-depth.md），本后端先给"首拉后离线 + 确定性"的真重排默认。

**默认行为不变**：未配置 cross-encoder（spec='none'，ServiceConfig 默认）时，make_reranker 返回 None，
build_narrative_retriever 退回原 judge（identity/RRF 或注入的 ProviderListwiseJudge）——默认 loop 字节
不变。cross-encoder 是 opt-in。

**隔离不变量继承**：cross-encoder 作为 ListwiseJudge 走 listwise_rerank 编排，而后者已把
sensitivity==RESTRICTED 的候选排除在 judge 之外（不送进打分、原位保留）——故 RESTRICTED 文本绝不进入
cross-encoder 打分（conformance 见 tests/retrieval/rerank/test_cross_encoder_isolation.py）。
"""

import importlib
import os
from typing import Any

from corespine import Registry, lazy_extra_import

from ragspine.retrieval.rerank.colbert import COLBERT_MODEL_ENV, ColbertReranker
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge
from ragspine.retrieval.rerank.splade import SPLADE_MODEL_ENV, SpladeReranker

# 默认 cross-encoder 模型名（唯一出处，改这里即全局生效）。选 Xenova/ms-marco-MiniLM-L-6-v2：
# Apache-2.0（过 ADR 0009 ≤Apache-2.0 许可门）、ONNX、纯 CPU、确定性、约 80MB——轻量真重排默认。
DEFAULT_CROSS_ENCODER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"

# cross-encoder 模型覆盖环境变量名（缺省模型时生效，仅对 cross_encoder 系 spec）。
CROSS_ENCODER_MODEL_ENV = "RAGSPINE_CROSS_ENCODER_MODEL"

# reranker 选型读取的环境变量名（缺省 spec 时生效）。
RERANKER_ENV = "RAGSPINE_RERANKER"

# 默认重排分批大小（透传给 TextCrossEncoder.rerank）。
DEFAULT_RERANK_BATCH_SIZE = 64


class CrossEncoderReranker:
    """本地 cross-encoder 重排（实现 ListwiseJudge 协议，fastembed 运行时延迟 import）。

    judge(query, candidates) 用 TextCrossEncoder 对每个候选打 query-候选相关性分，再按分降序返回
    候选下标（0-based 的一个排列）；平分时保持原（RRF）序——稳定排序，确定性。

    设计约束（延迟 import 范式同 W1 OnnxEmbeddingBackend）：
    - __init__ 只校验参数、记下模型名，**不** import fastembed、不加载模型——构造极轻，没装 [rerank]
      的机器上也能构造对象（auto 探测 / 单测）；
    - 模型在首次 judge 时延迟下载并缓存（fastembed 调 HF），之后复用缓存；
    - 空候选直接返回 []，不触发任何 import / 下载；
    - 返回分数条数与候选数一致性校验，异常即抛，绝不静默给坏名次。

    **确定性**：pin 模型名 + fastembed 版本后，CPU onnxruntime 推理逐位可复现 → 同输入同名次
    （conformance 见 tests 的 @pytest.mark.network 用例）。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_CROSS_ENCODER_MODEL,
        *,
        cache_dir: str | None = None,
        batch_size: int = DEFAULT_RERANK_BATCH_SIZE,
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
        """首次调用时延迟 import fastembed 并构造 TextCrossEncoder（之后复用缓存）。"""
        if self._model is None:
            # 重依赖延迟 import：缺失时由 corespine.lazy_extra_import 翻成
            # `pip install ragspine[rerank]` 友好提示（离线/不重排场景无需安装）。
            ce_mod = lazy_extra_import(
                "fastembed.rerank.cross_encoder", pkg="ragspine", extra="rerank"
            )
            kwargs: dict[str, Any] = {}
            if self.cache_dir is not None:
                kwargs["cache_dir"] = self.cache_dir
            if self.threads is not None:
                kwargs["threads"] = self.threads
            self._model = ce_mod.TextCrossEncoder(model_name=self.model_name, **kwargs)
        return self._model

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        """query 对每个候选打分 -> 按分降序的候选下标（平分按原序）。空候选返回 []，不触发加载。"""
        cands = list(candidates)
        if not cands:
            return []

        model = self._load_model()
        scores = [float(s) for s in model.rerank(query, cands, batch_size=self.batch_size)]
        if len(scores) != len(cands):
            raise RuntimeError(
                f"rerank 返回分数条数 {len(scores)} 与候选数 {len(cands)} 不一致"
                f"（model={self.model_name}）"
            )
        # 稳定降序：sorted(reverse=True) 对平分保持原（RRF）序——确定性。
        return sorted(range(len(cands)), key=lambda i: scores[i], reverse=True)


# reranker 缝注册表（corespine.Registry 泛化 make_*：名字->工厂 + spec 归一）。这是【三种本地重排
# 大脑】的统一注册表：cross_encoder / ce / ms_marco -> CrossEncoderReranker（W2 pointwise 交叉编码）；
# colbert / colbertv2 / late_interaction -> ColbertReranker（W11 token 级晚交互 MaxSim，colbert.py）；
# splade / splade_pp / learned_sparse -> SpladeReranker（W11 学习稀疏点积，splade.py）。三者都实现
# ListwiseJudge、都走 listwise_rerank（RESTRICTED 不出域一致继承）。Registry 内部对名字做大小写/留白/
# 连字符归一，故 "cross-encoder"/"CROSS_ENCODER"/" ce "/"late-interaction" 等都解析到同一工厂。
# 三个后端类的构造均惰性（不 import fastembed），故本表在此登记零 SDK import。
RERANKERS: Registry[ListwiseJudge] = Registry("reranker")
RERANKERS.register("cross_encoder", CrossEncoderReranker)
RERANKERS.register("ce", CrossEncoderReranker)
RERANKERS.register("ms_marco", CrossEncoderReranker)
RERANKERS.register("colbert", ColbertReranker)
RERANKERS.register("colbertv2", ColbertReranker)
RERANKERS.register("late_interaction", ColbertReranker)
RERANKERS.register("splade", SpladeReranker)
RERANKERS.register("splade_pp", SpladeReranker)
RERANKERS.register("learned_sparse", SpladeReranker)

# 各本地重排后端的别名集合（归一后）与其模型覆盖环境变量——用于 make_reranker 缺省模型时的 env 注入。
# 一张 (spec 集合 -> 模型 env 变量名) 表，随新增重排后端在此追加一行即可（数据驱动，避免多段 if 复制）。
_CROSS_ENCODER_SPECS = frozenset({"cross_encoder", "ce", "ms_marco"})
_COLBERT_SPECS = frozenset({"colbert", "colbertv2", "late_interaction"})
_SPLADE_SPECS = frozenset({"splade", "splade_pp", "learned_sparse"})
_MODEL_ENV_BY_SPECS: tuple[tuple[frozenset[str], str], ...] = (
    (_CROSS_ENCODER_SPECS, CROSS_ENCODER_MODEL_ENV),
    (_COLBERT_SPECS, COLBERT_MODEL_ENV),
    (_SPLADE_SPECS, SPLADE_MODEL_ENV),
)


def make_reranker(spec: str | None = None, **kwargs: Any) -> ListwiseJudge | None:
    """reranker 选型工厂：把「换重排大脑」从改代码降为一个 spec/env，默认 None＝不重排（行为不变）。

    薄封装 corespine.Registry（RERANKERS）：归一 spec 后交由 Registry 分派；保留 None＝不重排默认、
    env RAGSPINE_RERANKER 缺省、cross_encoder 系的 model_name env 注入与 kwargs 透传。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_RERANKER）：
        - None / 'none'                       -> None（不重排；build_narrative_retriever 退回原 judge：
                                                 identity/RRF 或注入的 LLM ProviderListwiseJudge——默认
                                                 loop 字节不变，cross-encoder 是 opt-in）
        - 'auto'                              -> 装了 fastembed（可导入）则 CrossEncoderReranker（本地真
                                                 重排）；否则 None（回落不重排，行为不变）
        - 'cross_encoder' / 'ce' / 'ms_marco' -> CrossEncoderReranker（本地 cross-encoder，延迟 import，
                                                 缺 fastembed 在首次 judge 时抛友好错；model_name 可经
                                                 kwargs 或 RAGSPINE_CROSS_ENCODER_MODEL 覆盖）
        - 'colbert' / 'colbertv2' / 'late_interaction'
                                              -> ColbertReranker（W11 ColBERT 晚交互 MaxSim 重排，延迟
                                                 import，[colbert]；model_name 可经 kwargs 或
                                                 RAGSPINE_COLBERT_MODEL 覆盖）
        - 'splade' / 'splade_pp' / 'learned_sparse'
                                              -> SpladeReranker（W11 SPLADE 学习稀疏点积重排，延迟
                                                 import，[splade]；model_name 可经 kwargs 或
                                                 RAGSPINE_SPLADE_MODEL 覆盖）
        - 其他                                -> ValueError（Registry 列清当前可用名）

    返回 ListwiseJudge 实例或 None（可直接喂给 build_narrative_retriever 的 reranker 参数）。
    """
    if spec is None:
        spec = os.environ.get(RERANKER_ENV)
    # 与 Registry._normalize 同口径地归一，用于 none 短路与各后端别名判定。
    normalized = (spec or "none").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "none":
        return None
    if normalized == "auto":
        # auto＝"装了就用、否则回落不重排"。探测 fastembed 是否可导入（不加载模型）：
        # 装了 [rerank] -> 走 cross_encoder（默认本地重排大脑，colbert/splade 是显式命名 opt-in）；
        # 没装 -> None（不重排，行为不变）。
        try:
            importlib.import_module("fastembed")
        except ImportError:
            return None
        normalized = "cross_encoder"
    if "model_name" not in kwargs:
        # 缺省模型时按 spec 所属后端读对应模型覆盖 env（数据驱动，随新增后端追加一行即可）。
        for specs, env_name in _MODEL_ENV_BY_SPECS:
            if normalized in specs:
                env_model = os.environ.get(env_name)
                if env_model:
                    kwargs["model_name"] = env_model
                break
    return RERANKERS.make(normalized, **kwargs)
