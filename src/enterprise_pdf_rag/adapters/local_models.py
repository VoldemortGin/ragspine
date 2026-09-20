"""Authenticated OpenAI-compatible embedding and rerank HTTP adapters."""

import json
import math
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from enterprise_pdf_rag.adapters.providers import (
    LocalModelConfig,
    ProviderRequestError,
)


@runtime_checkable
class LocalModelSender(Protocol):
    def __call__(self, url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes: ...


def _send_local_once(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
    parsed = urlsplit(url)
    connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    connection = connection_type(parsed.netloc, timeout=timeout)
    try:
        connection.request(
            "POST",
            parsed.path,
            body=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ProviderRequestError(
                f"Local model returned HTTP {response.status}; no retry performed"
            )
        body = response.read(1_048_577)
        if len(body) > 1_048_576:
            raise ProviderRequestError("Local model response exceeded the size limit")
        return body
    except (OSError, HTTPException):
        raise ProviderRequestError(
            "Local model connection failed or timed out; no retry performed"
        ) from None
    finally:
        connection.close()


class _EmbeddingItem(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    embedding: list[float]
    index: int


class _EmbeddingResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    data: list[_EmbeddingItem]


class RerankResult(BaseModel):
    """A provider score tied only to the caller's immutable document position."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    index: int
    relevance_score: float


class _RerankItem(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    index: int
    relevance_score: float


class _RerankResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    results: list[_RerankItem]


def _endpoint(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    return base + (path.removeprefix("/v1") if base.endswith("/v1") else path)


class LocalEmbeddingAdapter:
    """The production embedding boundary; only natural-language text is accepted."""

    def __init__(
        self,
        config: LocalModelConfig,
        *,
        sender: LocalModelSender | None = None,
    ) -> None:
        if config.purpose != "embedding":
            raise ValueError("Embedding adapter requires embedding configuration")
        self._config = config
        self._sender = sender if sender is not None else _send_local_once

    @property
    def fingerprint(self) -> str:
        return f"local-http/{self._config.model}"

    def embed_description(self, text: str) -> tuple[float, ...]:
        return self._embed(text)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._embed(text)

    def _embed(self, text: str) -> tuple[float, ...]:
        if not text.strip():
            raise ValueError("Embedding text must be non-empty")
        payload = json.dumps(
            {
                "model": self._config.model,
                "input": text,
                "encoding_format": "float",
            },
            separators=(",", ":"),
        ).encode()
        raw = self._sender(
            _endpoint(self._config.base_url, "/v1/embeddings"),
            api_key=self._config.api_key.get_secret_value(),
            payload=payload,
            timeout=30.0,
        )
        try:
            response = _EmbeddingResponse.model_validate_json(raw)
        except ValidationError:
            raise ProviderRequestError(
                "Embedding provider returned an invalid response schema"
            ) from None
        if len(response.data) != 1 or response.data[0].index != 0:
            raise ProviderRequestError("Embedding provider returned an unexpected result count")
        vector = tuple(response.data[0].embedding)
        if not vector or not all(math.isfinite(value) for value in vector):
            raise ProviderRequestError("Embedding provider returned an invalid numeric vector")
        return vector


class LocalRerankAdapter:
    """Typed reranking boundary; document text is not returned or retained."""

    def __init__(
        self,
        config: LocalModelConfig,
        *,
        sender: LocalModelSender | None = None,
    ) -> None:
        if config.purpose != "rerank":
            raise ValueError("Rerank adapter requires rerank configuration")
        self._config = config
        self._sender = sender if sender is not None else _send_local_once

    def rerank(
        self, query: str, documents: tuple[str, ...], *, limit: int
    ) -> tuple[RerankResult, ...]:
        if not query.strip():
            raise ValueError("Rerank query must be non-empty")
        if not documents or any(not document.strip() for document in documents):
            raise ValueError("Rerank documents must be non-empty")
        if limit < 1 or limit > len(documents):
            raise ValueError("Rerank limit must select at least one submitted document")
        payload = json.dumps(
            {
                "model": self._config.model,
                "query": query,
                "documents": list(documents),
                "top_n": limit,
                "return_documents": False,
            },
            separators=(",", ":"),
        ).encode()
        raw = self._sender(
            _endpoint(self._config.base_url, "/v1/rerank"),
            api_key=self._config.api_key.get_secret_value(),
            payload=payload,
            timeout=30.0,
        )
        try:
            response = _RerankResponse.model_validate_json(raw)
        except ValidationError:
            raise ProviderRequestError(
                "Rerank provider returned an invalid response schema"
            ) from None
        results = tuple(
            RerankResult(index=item.index, relevance_score=item.relevance_score)
            for item in response.results
        )
        indexes = tuple(result.index for result in results)
        if len(results) != limit:
            raise ProviderRequestError("Rerank provider returned an unexpected result count")
        if any(index < 0 or index >= len(documents) for index in indexes):
            raise ProviderRequestError(
                "Rerank provider returned an index outside the submitted documents"
            )
        if len(set(indexes)) != len(indexes):
            raise ProviderRequestError("Rerank provider returned duplicate document indexes")
        if any(not math.isfinite(result.relevance_score) for result in results):
            raise ProviderRequestError("Rerank provider returned an invalid score")
        return results
