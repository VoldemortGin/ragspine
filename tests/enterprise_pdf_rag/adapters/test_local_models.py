"""Typed local model adapters keep secrets and raw provider bodies at the edge."""

import json

import pytest

from enterprise_pdf_rag.figures.ports import EmbeddingPort
from ragspine.common.evidence.providers.local_models import (
    LocalEmbeddingAdapter,
    LocalRerankAdapter,
    RerankResult,
)
from ragspine.common.evidence.providers.providers import load_local_model_config


def test_embedding_adapter_implements_document_and_query_contract() -> None:
    requests: list[dict[str, object]] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        requests.append(
            {
                "url": url,
                "api_key": api_key,
                "payload": json.loads(payload),
                "timeout": timeout,
            }
        )
        return b'{"data":[{"embedding":[0.25,-0.5,0.75],"index":0}]}'

    adapter = LocalEmbeddingAdapter(
        load_local_model_config(
            "embedding",
            {
                "EMBEDDING_BASE_URL": "http://127.0.0.1:29002/v1/",
                "EMBEDDING_MODEL": "embedding-model",
                "EMBEDDING_API_KEY": "embedding-secret",
            },
        ),
        sender=sender,
    )

    assert adapter.embed_description("A sourced chart description.") == (
        0.25,
        -0.5,
        0.75,
    )
    assert adapter.embed_query("What changed?") == (0.25, -0.5, 0.75)
    assert adapter.fingerprint == "local-http/embedding-model"
    assert isinstance(adapter, EmbeddingPort)
    assert requests == [
        {
            "url": "http://127.0.0.1:29002/v1/embeddings",
            "api_key": "embedding-secret",
            "payload": {
                "model": "embedding-model",
                "input": "A sourced chart description.",
                "encoding_format": "float",
            },
            "timeout": 30.0,
        },
        {
            "url": "http://127.0.0.1:29002/v1/embeddings",
            "api_key": "embedding-secret",
            "payload": {
                "model": "embedding-model",
                "input": "What changed?",
                "encoding_format": "float",
            },
            "timeout": 30.0,
        },
    ]
    assert "embedding-secret" not in repr(adapter)


def test_rerank_adapter_returns_typed_ranked_indexes_without_documents() -> None:
    requests: list[dict[str, object]] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        requests.append(
            {
                "url": url,
                "api_key": api_key,
                "payload": json.loads(payload),
                "timeout": timeout,
            }
        )
        return b'{"results":[{"index":1,"document":{"text":"Document B grew.","multi_modal":null},"relevance_score":0.9},{"index":0,"document":{"text":"Document A stayed flat.","multi_modal":null},"relevance_score":0.2}]}'

    adapter = LocalRerankAdapter(
        load_local_model_config(
            "rerank",
            {
                "RERANK_BASE_URL": "http://127.0.0.1:29001",
                "RERANK_MODEL": "rerank-model",
                "RERANK_API_KEY": "rerank-secret",
            },
        ),
        sender=sender,
    )

    result = adapter.rerank(
        "Which document grew?",
        ("Document A stayed flat.", "Document B grew."),
        limit=2,
    )

    assert result == (
        RerankResult(index=1, relevance_score=0.9),
        RerankResult(index=0, relevance_score=0.2),
    )
    assert requests == [
        {
            "url": "http://127.0.0.1:29001/v1/rerank",
            "api_key": "rerank-secret",
            "payload": {
                "model": "rerank-model",
                "query": "Which document grew?",
                "documents": ["Document A stayed flat.", "Document B grew."],
                "top_n": 2,
                "return_documents": False,
            },
            "timeout": 30.0,
        }
    ]
    assert "rerank-secret" not in repr(adapter)


def test_embedding_rejects_blank_text_before_any_request() -> None:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        raise AssertionError("sender must not be called")

    adapter = LocalEmbeddingAdapter(
        load_local_model_config(
            "embedding",
            {
                "EMBEDDING_BASE_URL": "http://127.0.0.1:29002",
                "EMBEDDING_MODEL": "embedding-model",
                "EMBEDDING_API_KEY": "embedding-secret",
            },
        ),
        sender=sender,
    )

    with pytest.raises(ValueError, match="non-empty"):
        adapter.embed_description(" \n ")


def test_rerank_rejects_out_of_range_or_duplicate_provider_indexes() -> None:
    responses = iter(
        (
            b'{"results":[{"index":2,"relevance_score":0.9}]}',
            b'{"results":[{"index":0,"relevance_score":0.9},{"index":0,"relevance_score":0.8}]}',
        )
    )

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        return next(responses)

    adapter = LocalRerankAdapter(
        load_local_model_config(
            "rerank",
            {
                "RERANK_BASE_URL": "http://127.0.0.1:29001",
                "RERANK_MODEL": "rerank-model",
                "RERANK_API_KEY": "rerank-secret",
            },
        ),
        sender=sender,
    )

    with pytest.raises(ValueError, match="outside the submitted documents"):
        adapter.rerank("query", ("A", "B"), limit=1)
    with pytest.raises(ValueError, match="duplicate document indexes"):
        adapter.rerank("query", ("A", "B"), limit=2)
