"""embedding 后端实现（B 线 src/retrieval.EmbeddingBackend 协议）+ 后端工厂。

OpenAIEmbeddingBackend：OpenAI embeddings API（openai>=1.0 SDK）。
设计约束（范式同 src/llm_provider.AnthropicProvider）：
- SDK 延迟 import：不装 openai 也能 import 本模块；
- base_url 可覆盖：适配企业网关（GenAI Hub）转发 OpenAI API；
- 默认模型名集中在 DEFAULT_OPENAI_EMBEDDING_MODEL 一处；
- 批量请求按 batch_size 分批，返回按 index 恢复与输入对齐的顺序；
- 返回向量做条数与维度一致性校验，异常即抛，绝不静默给坏向量。

DeterministicEmbeddingBackend：离线 / 测试 / 打通管线用的【词法散列】后端
（hashlib 散列 token 到桶 + L2 归一化），零网络、零三方依赖（仅 hashlib+math）、
跨进程 / 跨平台可复现。**诚实声明：这是与 BM25 信号高度相关的【非语义】后端，
不代表、也绝不带来真语义召回增益。真语义增益需 Qwen3 等真实模型（待 GPU infra）。**

make_embedding_backend：后端工厂，把「接通向量通道」的成本从改代码降为一个
flag/env（spec / RAGSPINE_EMBEDDING_BACKEND），默认仍为 None＝纯 BM25 现状。

Qwen3 自托管后端本期不做（部署形态未定）：协议留口，任何实现
embed_texts(list[str]) -> list[list[float]] 的对象皆可注入。
"""

import hashlib
import math
import os
import re

from ragspine.agent.llm_provider import ProviderError

# 默认 embedding 模型名（唯一出处，改这里即全局生效；docs/06 混合栈推荐）
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-large"

# 默认 SentenceTransformer 模型名（缺省 Qwen3-Embedding-0.6B，唯一出处）。
DEFAULT_ST_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"

# 设备/模型覆盖环境变量名（与 EMBEDDING_BACKEND_ENV 同族）。
EMBEDDING_DEVICE_ENV = "RAGSPINE_EMBEDDING_DEVICE"
EMBEDDING_MODEL_ENV = "RAGSPINE_EMBEDDING_MODEL"

# 合法 torch 设备取值（override / env 取值校验白名单）。
_VALID_DEVICES = ("cuda", "mps", "cpu")

# 默认分批大小（OpenAI 单请求 input 上限远高于此，取保守值控制单请求体积）
DEFAULT_EMBEDDING_BATCH_SIZE = 64

# 确定性后端默认维度（可配；与 BM25 同走中英混排分词，桶累加 tf）。
DEFAULT_DETERMINISTIC_DIM = 256

# 确定性后端读取的环境变量名（缺省 spec 时生效）。
EMBEDDING_BACKEND_ENV = "RAGSPINE_EMBEDDING_BACKEND"

# 确定性后端分词：与 src/retrieval.tokenize 同口径（ASCII 串按词、CJK 串 unigram+bigram）。
_CJK_RANGE = "㐀-䶿一-鿿豈-﫿"
_TOKEN_RE = re.compile(rf"[a-z0-9]+|[{_CJK_RANGE}]+")


def _select_torch_device(
    override: str | None = None,
    *,
    cuda_available: bool | None = None,
    mps_available: bool | None = None,
) -> str:
    """解析 torch 设备名，代码层自适应运行机器（笔记本 MPS / GPU 盒子 CUDA）。

    优先级：显式 override 参数 > 环境变量 RAGSPINE_EMBEDDING_DEVICE > 自动探测；
    自动探测顺序 cuda → mps → cpu。override / env 取值只接受 cuda/mps/cpu，
    非法值抛 ValueError。

    可测性：cuda_available / mps_available 显式传入时直接采用（不碰本机硬件），
    仅当二者为 None 时才延迟 import torch 探测——故设备逻辑单测可完全脱离本机
    真实硬件与 torch 是否安装。torch.backends.mps 用 getattr 防御老版本无 mps。
    """
    explicit = override if override is not None else os.environ.get(EMBEDDING_DEVICE_ENV)
    if explicit is not None:
        device = explicit.strip().lower()
        if device not in _VALID_DEVICES:
            raise ValueError(
                f"非法设备 {explicit!r}（只接受 {'/'.join(_VALID_DEVICES)}）"
            )
        return device

    if cuda_available is None or mps_available is None:
        import torch

        if cuda_available is None:
            cuda_available = torch.cuda.is_available()
        if mps_available is None:
            mps_backend = getattr(torch.backends, "mps", None)
            mps_available = bool(mps_backend and mps_backend.is_available())

    if cuda_available:
        return "cuda"
    if mps_available:
        return "mps"
    return "cpu"


class OpenAIEmbeddingBackend:
    """OpenAI embeddings 后端（实现 EmbeddingBackend 协议，SDK 延迟 import）。"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_OPENAI_EMBEDDING_MODEL,
        base_url: str | None = None,
        batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
        timeout: float = 30.0,
        max_retries: int = 2,
    ):
        try:
            import openai
        except ImportError as exc:
            raise ImportError(
                "未安装 openai SDK：pip install 'ragspine[llm]' 或 "
                "pip install openai；离线/纯 BM25 场景无需安装。"
            ) from exc

        if batch_size < 1:
            raise ValueError(f"batch_size 必须 >= 1，得到 {batch_size}")

        # 超时/重试透传给 SDK：由 SDK 原生退避处理，不自造退避。
        client_kwargs: dict = {"timeout": timeout, "max_retries": max_retries}
        if api_key is not None:
            client_kwargs["api_key"] = api_key
        if base_url is not None:
            client_kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**client_kwargs)
        self.model = model
        self.batch_size = batch_size

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量文本 -> 向量（顺序与输入对齐）。空输入直接返回空表，不发请求。"""
        if not texts:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            try:
                resp = self._client.embeddings.create(model=self.model, input=batch)
            except Exception as exc:  # noqa: BLE001 — SDK 网络/API 异常归一到 ProviderError
                raise ProviderError(f"OpenAI embedding 调用失败：{exc}") from exc
            data = sorted(resp.data, key=lambda item: item.index)
            if len(data) != len(batch):
                raise RuntimeError(
                    f"embedding 返回条数 {len(data)} 与请求条数 {len(batch)} 不一致"
                    f"（model={self.model}）"
                )
            vectors.extend(list(item.embedding) for item in data)

        dims = {len(v) for v in vectors}
        if len(dims) > 1:
            raise ValueError(f"embedding 维度不一致：{sorted(dims)}（model={self.model}）")
        return vectors


def _tokenize(text: str) -> list[str]:
    """中英混排分词（与 src/retrieval.tokenize 同口径，本模块内复刻以保持零互依赖）。"""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        run = match.group(0)
        if run[0].isascii():
            tokens.append(run)
        else:
            tokens.extend(run)  # unigram
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))  # bigram
    return tokens


class DeterministicEmbeddingBackend:
    """离线确定性【词法散列】后端（实现 EmbeddingBackend 协议）。

    算法：对每条文本分词（中英混排，同 BM25 口径），用 **hashlib**（非内置
    hash()，避免 PYTHONHASHSEED 随机化导致跨进程不可复现）把每个 token 稳定散列
    到 [0, dim) 桶，按 tf 权重累加，最后 L2 归一化为单位向量。零网络、零三方依赖
    （仅 hashlib + math），跨进程 / 跨平台逐元素可复现；输出顺序与输入对齐，空输入
    返回 []，无可分词内容的文本返回零向量（cosine 一律 0，不抛错）。

    **诚实边界（不可省略）**：本后端是用于离线 / 测试 / 打通管线（切块→入库→混合
    检索→listwise）的【词法散列】后端，向量信号本质来自 token 桶命中，与 BM25
    高度相关，属【非语义】后端——它绝不代表、也绝不带来真语义召回提升。真语义
    增益需 Qwen3 等真实 embedding 模型（待 GPU infra）。
    """

    def __init__(self, dim: int = DEFAULT_DETERMINISTIC_DIM):
        if dim < 1:
            raise ValueError(f"dim 必须 >= 1，得到 {dim}")
        self.dim = dim

    def _bucket(self, token: str) -> int:
        """hashlib 把 token 稳定散列到 [0, dim) 桶（blake2b，截 8 字节大端整数取模）。"""
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dim

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量文本 -> L2 归一化词法散列向量（顺序对齐）。空输入返回 []。"""
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in _tokenize(text):
                vec[self._bucket(token)] += 1.0
            norm = math.sqrt(sum(x * x for x in vec))
            if norm > 0.0:
                vec = [x / norm for x in vec]
            vectors.append(vec)
        return vectors


class SentenceTransformerEmbeddingBackend:
    """通用 SentenceTransformer 后端（实现 EmbeddingBackend 协议，重依赖延迟 import）。

    默认 Qwen3-Embedding-0.6B；设备经 _select_torch_device 代码层自适应（cuda/mps/cpu）。
    设计约束（同 OpenAIEmbeddingBackend 的延迟 import 范式）：
    - __init__ **只**解析 device（可 import torch，本机有 torch），**不** import
      sentence_transformers、不加载模型——构造轻，可在没装 sentence-transformers 的
      机器上构造对象用于单测设备逻辑；
    - 模型在首次 embed_texts 时延迟加载并缓存；
    - 空输入直接返回 []，不触发任何 import / 加载；
    - encode(normalize_embeddings=normalize)，返回向量做维度一致性校验。

    诚实边界：真实 Qwen3 的加载与编码属【集成行为】，需在 GPU 盒子上联网下载模型后
    跑（pytest -m gpu），不在离线单测内。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_ST_EMBEDDING_MODEL,
        device: str | None = None,
        normalize: bool = True,
        batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    ):
        if batch_size < 1:
            raise ValueError(f"batch_size 必须 >= 1，得到 {batch_size}")
        self.model_name = model_name
        self.device = _select_torch_device(device)
        self.normalize = normalize
        self.batch_size = batch_size
        self._model = None  # 延迟加载缓存

    def _load_model(self):
        """首次调用时延迟 import sentence_transformers 并加载模型（之后复用缓存）。"""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise ImportError(
                    "未安装 sentence-transformers：pip install "
                    "'ragspine[embed]' 或 pip install sentence-transformers；"
                    "离线/纯 BM25 场景无需安装。"
                ) from exc
            self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量文本 -> 向量（顺序对齐）。空输入直接返回 []，不触发模型加载。"""
        if not texts:
            return []

        model = self._load_model()
        embeddings = model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=self.normalize,
        )
        vectors = [list(map(float, row)) for row in embeddings]
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"embedding 返回条数 {len(vectors)} 与请求条数 {len(texts)} 不一致"
                f"（model={self.model_name}）"
            )
        dims = {len(v) for v in vectors}
        if len(dims) > 1:
            raise ValueError(f"embedding 维度不一致：{sorted(dims)}（model={self.model_name}）")
        return vectors


def make_embedding_backend(spec: str | None = None, **kwargs):
    """后端工厂：把「接通向量通道」从改代码降为一个 flag/env，默认仍为 None＝纯 BM25。

    spec 取值（大小写不敏感；缺省读环境变量 RAGSPINE_EMBEDDING_BACKEND）：
        - None / 'none'                       -> None（纯 BM25+RRF，保持现状默认）
        - 'deterministic'                     -> DeterministicEmbeddingBackend（dim 等经 kwargs 透传）
        - 'openai'                            -> OpenAIEmbeddingBackend（延迟 import，缺 SDK 抛友好错）
        - 'qwen3' / 'sentence-transformers' / 'st'
                                              -> SentenceTransformerEmbeddingBackend
                                                 （缺省 Qwen3-Embedding-0.6B；model_name 可经
                                                 kwargs 或 RAGSPINE_EMBEDDING_MODEL 覆盖；device 经
                                                 kwargs / RAGSPINE_EMBEDDING_DEVICE / 自动探测）
        - 其他                                -> ValueError

    返回 EmbeddingBackend 实例或 None（可直接喂给 NarrativeIndex /
    build_narrative_retriever 的 embedding_backend 参数）。
    """
    if spec is None:
        spec = os.environ.get(EMBEDDING_BACKEND_ENV)
    normalized = (spec or "none").strip().lower()
    if normalized == "none":
        return None
    if normalized == "deterministic":
        return DeterministicEmbeddingBackend(**kwargs)
    if normalized == "openai":
        return OpenAIEmbeddingBackend(**kwargs)
    if normalized in ("qwen3", "sentence-transformers", "st"):
        if "model_name" not in kwargs:
            env_model = os.environ.get(EMBEDDING_MODEL_ENV)
            if env_model:
                kwargs["model_name"] = env_model
        return SentenceTransformerEmbeddingBackend(**kwargs)
    raise ValueError(
        f"未知 embedding 后端 spec：{spec!r}"
        "（可选 none / deterministic / openai / qwen3 / sentence-transformers / st）"
    )
