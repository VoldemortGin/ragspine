"""Authenticated OpenAI-compatible embedding and rerank HTTP adapters."""

import json
import math
from collections.abc import Sequence
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from ragspine.common.evidence.providers.providers import (
    LocalModelConfig,
    ProviderRequestError,
)


@runtime_checkable
class LocalModelSender(Protocol):
    def __call__(self, url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes: ...


_RESPONSE_LIMIT = 1_048_576


def _send_local_once(
    url: str,
    *,
    api_key: str,
    payload: bytes,
    timeout: float,
    max_response_bytes: int = _RESPONSE_LIMIT,
) -> bytes:
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
                f"Local model returned HTTP {response.status}; no retry performed",
                status=response.status,
                category="http",
            )
        body = response.read(max_response_bytes + 1)
        if len(body) > max_response_bytes:
            raise ProviderRequestError(
                "Local model response exceeded the size limit", category="response_limit"
            )
        return body
    except (OSError, HTTPException) as error:
        raise ProviderRequestError(
            "Local model connection failed or timed out; no retry performed",
            category="timeout" if isinstance(error, TimeoutError) else "connection",
        ) from None
    finally:
        connection.close()


_DEFAULT_SENDER = _send_local_once

# Batched description embeddings (OpenAI-compatible ``input`` array). Conservative defaults:
# OpenAI allows 2048 inputs / 300k tokens per request, but Azure OpenAI deployments of older
# API versions accept only 16 inputs per request, so 16 works everywhere. 48 000 characters
# keeps a full batch far below any per-request token limit even for CJK text (about one token
# per character) and keeps the reply of 16 large vectors within a few MiB.
EMBEDDING_BATCH_MAX_ITEMS = 16
EMBEDDING_BATCH_MAX_CHARS = 48_000
# Failed multi-input requests one adapter tolerates before it sends single inputs only, so a
# degraded endpoint costs at most this many extra requests per run, never one per batch.
EMBEDDING_BATCH_MAX_FAILURES = 8
_SINGLE_TIMEOUT = 30.0
_BATCH_TIMEOUT = 60.0
# A request that fails the same way whatever its size: never split, never retried.
_FATAL_STATUSES = frozenset({401, 403, 404})


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
        batch_max_items: int = EMBEDDING_BATCH_MAX_ITEMS,
        batch_max_chars: int = EMBEDDING_BATCH_MAX_CHARS,
    ) -> None:
        if config.purpose != "embedding":
            raise ValueError("Embedding adapter requires embedding configuration")
        if batch_max_items < 1 or batch_max_chars < 1:
            raise ValueError("Embedding batch limits must be positive")
        self._config = config
        self._sender = sender if sender is not None else _send_local_once
        self._batch_max_items = batch_max_items
        self._batch_max_chars = batch_max_chars
        self._request_count = 0
        self._batch_failures = 0
        self._arrays_worked = False
        self._arrays_refused = False

    @property
    def fingerprint(self) -> str:
        return f"local-http/{self._config.model}"

    @property
    def request_count(self) -> int:
        """Requests this adapter has sent, failed ones included."""
        return self._request_count

    def embed_description(self, text: str) -> tuple[float, ...]:
        return self._embed(text)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._embed(text)

    def embed_descriptions(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Each text's vector, in order, sent as ``input`` arrays within the batch limits.

        Transport only: element ``i`` is what ``embed_description(texts[i])`` returns, a
        one-text batch is that very request, and a reply is aligned by its ``index`` alone
        and refused unless it covers every input once with one finite dimension. A failed
        batch is halved down to single inputs; a single input fails exactly as
        ``embed_description`` does. An endpoint refusing arrays (HTTP 400 on a pair whose
        singles answer, before any array worked) and an adapter past
        ``EMBEDDING_BATCH_MAX_FAILURES`` failed batches send single inputs from then on.
        """
        if any(not text.strip() for text in texts):
            raise ValueError("Embedding text must be non-empty")
        vectors: list[tuple[float, ...]] = []
        start = 0
        while start < len(texts):
            end, chars = start, 0
            while end < len(texts) and end - start < self._batch_max_items:
                chars += len(texts[end])
                if end > start and chars > self._batch_max_chars:
                    break
                end += 1
            vectors.extend(self._embed_split(list(texts[start:end])))
            start = end
        return tuple(vectors)

    def _batching(self) -> bool:
        return not self._arrays_refused and self._batch_failures < EMBEDDING_BATCH_MAX_FAILURES

    def _embed_split(self, texts: list[str]) -> list[tuple[float, ...]]:
        if len(texts) == 1:
            return [self._embed(texts[0])]
        if not self._batching():
            return [self._embed(text) for text in texts]
        try:
            vectors = self._embed_batch(texts)
        except ProviderRequestError as error:
            if error.status in _FATAL_STATUSES:
                raise
            self._batch_failures += 1
            middle = len(texts) // 2
            left = self._embed_split(texts[:middle])
            right = self._embed_split(texts[middle:])
            if len(texts) == 2 and error.status == 400 and not self._arrays_worked:
                self._arrays_refused = True
            return left + right
        self._arrays_worked = True
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[tuple[float, ...]]:
        raw = self._post(texts, timeout=_BATCH_TIMEOUT)
        try:
            response = _EmbeddingResponse.model_validate_json(raw)
        except ValidationError:
            raise ProviderRequestError(
                "Embedding provider returned an invalid response schema"
            ) from None
        by_index = {item.index: tuple(item.embedding) for item in response.data}
        if len(response.data) != len(texts) or set(by_index) != set(range(len(texts))):
            raise ProviderRequestError("Embedding provider returned an unexpected result count")
        vectors = [by_index[index] for index in range(len(texts))]
        if len({len(vector) for vector in vectors}) != 1 or any(
            not vector or not all(math.isfinite(value) for value in vector) for vector in vectors
        ):
            raise ProviderRequestError("Embedding provider returned an invalid numeric vector")
        return vectors

    def _post(self, text: str | list[str], *, timeout: float) -> bytes:
        payload = json.dumps(
            {
                "model": self._config.model,
                "input": text,
                "encoding_format": "float",
            },
            separators=(",", ":"),
        ).encode()
        url = _endpoint(self._config.base_url, "/v1/embeddings")
        api_key = self._config.api_key.get_secret_value()
        self._request_count += 1
        if isinstance(text, list) and self._sender is _DEFAULT_SENDER:
            return _send_local_once(
                url,
                api_key=api_key,
                payload=payload,
                timeout=timeout,
                max_response_bytes=_RESPONSE_LIMIT * len(text),
            )
        return self._sender(url, api_key=api_key, payload=payload, timeout=timeout)

    def _embed(self, text: str) -> tuple[float, ...]:
        if not text.strip():
            raise ValueError("Embedding text must be non-empty")
        raw = self._post(text, timeout=_SINGLE_TIMEOUT)
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
