"""单条 embedder → 批量 EmbeddingBackend 的薄适配（零 SDK、零 import 依赖）。

OpenAI 兼容的 HTTP embedding 适配器（如 ``ragspine.common.evidence.providers.local_models`` 的
``LocalEmbeddingAdapter``，对接 vLLM 上的 Qwen3-Embedding ``/v1/embeddings``）只暴露单条
``embed_query(text)``；叙事检索的 ``EmbeddingBackend`` 协议要的是批量 ``embed_texts``。本模块把
前者鸭子类型地接成后者：逐条调用、顺序对齐，不 import 任何具体适配器，由调用方注入。

非对称 embedding 模型（如 Qwen3-Embedding）查询端要加指令前缀、文档端不加：``embed_texts``（文档）
原样发送，``embed_query``（查询，``HybridRetriever`` 优先调用）拼上 ``query_prefix``；缺省 ``""``＝不加。
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class QueryEmbedder(Protocol):
    """单条文本 -> 向量（如 LocalEmbeddingAdapter.embed_query）。"""

    def embed_query(self, text: str) -> Sequence[float]: ...


class SingleTextEmbeddingBackend:
    """实现 EmbeddingBackend 协议：对每条文本调一次 ``embedder.embed_query``。"""

    def __init__(
        self, embedder: QueryEmbedder, *, model_id: str | None = None, query_prefix: str = ""
    ) -> None:
        self._embedder = embedder
        # 只作用于查询端（embed_query）；文档向量不受影响，故不进 model_id（持久化向量库不失效）。
        self.query_prefix = query_prefix
        # 模型标识（持久化向量库据此核对，见 chunk_index.embedding_model_id）；None＝未声明。
        self.model_id = model_id

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量文本 -> 向量（顺序与输入对齐）。空输入直接返回空表，不发请求。"""
        vectors = [list(self._embedder.embed_query(text)) for text in texts]
        dims = {len(v) for v in vectors}
        if len(dims) > 1:
            raise ValueError(f"embedding 维度不一致：{sorted(dims)}")
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """查询 -> 向量：拼 ``query_prefix`` 后单条嵌入（文档端请用 ``embed_texts``）。"""
        return list(self._embedder.embed_query(self.query_prefix + text))
