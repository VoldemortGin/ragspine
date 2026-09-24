"""local-http 工厂注册：RAGSPINE_EMBEDDING=local-http / RAGSPINE_RERANKER=local-http 直接可用。

只验证装配（构造不发请求）：读 EMBEDDING_* / RERANK_* 环境变量，包成 T4 的薄适配器，带模型标识。
"""

import pytest

from ragspine.retrieval.rerank.cross_encoder import make_reranker
from ragspine.retrieval.rerank.scored_judge import ScoredRerankJudge
from ragspine.retrieval.vector.chunk_index import embedding_model_id
from ragspine.retrieval.vector.embedding_backends import make_embedding_backend
from ragspine.retrieval.vector.single_text_backend import SingleTextEmbeddingBackend

_ENV = {
    "EMBEDDING_BASE_URL": "http://127.0.0.1:39002",
    "EMBEDDING_MODEL": "Qwen/Qwen3-Embedding-4B",
    "EMBEDDING_API_KEY": "test-key",
    "RERANK_BASE_URL": "http://127.0.0.1:39001",
    "RERANK_MODEL": "Qwen/Qwen3-Reranker-4B",
    "RERANK_API_KEY": "test-key",
}


@pytest.fixture
def local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("spec", ["local-http", "local_http", " LOCAL-HTTP "])
def test_embedding_factory_builds_local_http_backend(local_env, spec):
    backend = make_embedding_backend(spec)
    assert isinstance(backend, SingleTextEmbeddingBackend)
    assert embedding_model_id(backend) == "local-http:Qwen/Qwen3-Embedding-4B"


@pytest.mark.parametrize("spec", ["local-http", "local_http"])
def test_reranker_factory_builds_scored_judge(local_env, spec):
    assert isinstance(make_reranker(spec), ScoredRerankJudge)


def test_local_http_requires_env(monkeypatch):
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(Exception, match="EMBEDDING_BASE_URL"):
        make_embedding_backend("local-http")
    with pytest.raises(Exception, match="RERANK_BASE_URL"):
        make_reranker("local-http")


def test_defaults_unchanged(monkeypatch):
    monkeypatch.delenv("RAGSPINE_EMBEDDING_BACKEND", raising=False)
    monkeypatch.delenv("RAGSPINE_RERANKER", raising=False)
    assert make_embedding_backend() is None
    assert make_reranker() is None
