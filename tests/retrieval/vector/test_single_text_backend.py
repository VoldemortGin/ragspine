"""SingleTextEmbeddingBackend：把「单条 embed_query」的 HTTP 适配器接成批量 EmbeddingBackend。

替身 embedder 零网络；只验证外部行为：顺序对齐、空输入不调用、协议一致、维度不一致即抛。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.lexical.retrieval import EmbeddingBackend
from ragspine.retrieval.vector.single_text_backend import SingleTextEmbeddingBackend


class FakeEmbedder:
    def __init__(self, dims: dict[str, int] | None = None) -> None:
        self.calls: list[str] = []
        self._dims = dims or {}

    def embed_query(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        dim = self._dims.get(text, 3)
        return tuple(float(len(text) + i) for i in range(dim))


def test_embeds_each_text_in_order() -> None:
    embedder = FakeEmbedder()
    backend = SingleTextEmbeddingBackend(embedder)
    vectors = backend.embed_texts(["a", "bbb"])
    assert embedder.calls == ["a", "bbb"]
    assert vectors == [[1.0, 2.0, 3.0], [3.0, 4.0, 5.0]]
    assert all(isinstance(v, list) for v in vectors)


def test_empty_input_makes_no_call() -> None:
    embedder = FakeEmbedder()
    assert SingleTextEmbeddingBackend(embedder).embed_texts([]) == []
    assert embedder.calls == []


def test_satisfies_embedding_backend_protocol() -> None:
    assert isinstance(SingleTextEmbeddingBackend(FakeEmbedder()), EmbeddingBackend)


def test_inconsistent_dimensions_raise() -> None:
    backend = SingleTextEmbeddingBackend(FakeEmbedder(dims={"b": 2}))
    with pytest.raises(ValueError, match="维度"):
        backend.embed_texts(["a", "b"])
