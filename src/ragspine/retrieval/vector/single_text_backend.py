"""单条 embedder → 批量 EmbeddingBackend 的薄适配（零 SDK、零 import 依赖）。

OpenAI 兼容的 HTTP embedding 适配器（如 ``ragspine.common.evidence.providers.local_models`` 的
``LocalEmbeddingAdapter``，对接 vLLM 上的 Qwen3-Embedding ``/v1/embeddings``）只暴露单条
``embed_query(text)``；叙事检索的 ``EmbeddingBackend`` 协议要的是批量 ``embed_texts``。本模块把
前者鸭子类型地接成后者：逐条调用、顺序对齐，不 import 任何具体适配器，由调用方注入。
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class QueryEmbedder(Protocol):
    """单条文本 -> 向量（如 LocalEmbeddingAdapter.embed_query）。"""

    def embed_query(self, text: str) -> Sequence[float]: ...


class SingleTextEmbeddingBackend:
    """实现 EmbeddingBackend 协议：对每条文本调一次 ``embedder.embed_query``。"""

    def __init__(self, embedder: QueryEmbedder, *, model_id: str | None = None) -> None:
        self._embedder = embedder
        # 模型标识（持久化向量库据此核对，见 chunk_index.embedding_model_id）；None＝未声明。
        self.model_id = model_id

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量文本 -> 向量（顺序与输入对齐）。空输入直接返回空表，不发请求。"""
        vectors = [list(self._embedder.embed_query(text)) for text in texts]
        dims = {len(v) for v in vectors}
        if len(dims) > 1:
            raise ValueError(f"embedding 维度不一致：{sorted(dims)}")
        return vectors
