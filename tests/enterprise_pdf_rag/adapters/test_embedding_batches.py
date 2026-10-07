"""Batched description embeddings: one request per batch, vectors aligned by ``index`` only."""

import json
import math
from collections.abc import Callable

import pytest

from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from ragspine.common.evidence.providers.local_models import (
    EMBEDDING_BATCH_MAX_CHARS,
    EMBEDDING_BATCH_MAX_FAILURES,
    EMBEDDING_BATCH_MAX_ITEMS,
    LocalEmbeddingAdapter,
)
from ragspine.common.evidence.providers.providers import (
    ProviderRequestError,
    load_local_model_config,
)
from ragspine.common.evidence.providers.transient import TRANSIENT_MAX_RETRIES
from ragspine.extraction.evidence.figures.ports import BatchEmbeddingPort, EmbeddingPort

_CONFIG = {
    "APP_EMBEDDING_BASE_URL": "http://127.0.0.1:29002/v1/",
    "APP_EMBEDDING_MODEL": "embedding-model",
    "APP_EMBEDDING_API_KEY": "embedding-secret",
}


def _vector(text: str) -> tuple[float, ...]:
    return OfflineDescriptionEmbedder._vector(text)


def _texts(count: int) -> list[str]:
    return [f"object {index} revenue margin {index * 7}" for index in range(count)]


Fault = Callable[[list[str] | str], ProviderRequestError | bytes | None]


class _Endpoint:
    """A scripted OpenAI-compatible ``/v1/embeddings``: offline vectors, optional faults."""

    def __init__(self, *, fault: Fault | None = None, reverse: bool = False) -> None:
        self.inputs: list[list[str] | str] = []
        self.timeouts: list[float] = []
        self._fault = fault
        self._reverse = reverse

    def __call__(self, url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        body = json.loads(payload)
        assert url == "http://127.0.0.1:29002/v1/embeddings"
        assert api_key == "embedding-secret"
        assert set(body) == {"model", "input", "encoding_format"}
        inputs = body["input"]
        self.inputs.append(inputs)
        self.timeouts.append(timeout)
        if self._fault is not None:
            fault = self._fault(inputs)
            if isinstance(fault, ProviderRequestError):
                raise fault
            if isinstance(fault, bytes):
                return fault
        texts = inputs if isinstance(inputs, list) else [inputs]
        data = [
            {"object": "embedding", "index": index, "embedding": list(_vector(text))}
            for index, text in enumerate(texts)
        ]
        if self._reverse:
            data.reverse()
        return json.dumps({"object": "list", "data": data}).encode()

    @property
    def sizes(self) -> list[int]:
        return [len(item) if isinstance(item, list) else 1 for item in self.inputs]


def _adapter(endpoint: _Endpoint, **limits: int) -> LocalEmbeddingAdapter:
    return LocalEmbeddingAdapter(
        load_local_model_config("embedding", _CONFIG), sender=endpoint, **limits
    )


def _http(status: int) -> ProviderRequestError:
    return ProviderRequestError(
        f"Local model returned HTTP {status}; no retry performed", status=status, category="http"
    )


def test_defaults_are_conservative_named_constants() -> None:
    assert EMBEDDING_BATCH_MAX_ITEMS == 16
    assert EMBEDDING_BATCH_MAX_CHARS == 48_000
    assert EMBEDDING_BATCH_MAX_FAILURES == 8
    adapter = _adapter(_Endpoint())
    assert isinstance(adapter, EmbeddingPort)
    assert isinstance(adapter, BatchEmbeddingPort)
    assert not isinstance(OfflineDescriptionEmbedder(), BatchEmbeddingPort)


def test_n_texts_cost_ceil_n_over_batch_size_requests_with_identical_vectors() -> None:
    endpoint = _Endpoint()
    adapter = _adapter(endpoint)
    texts = _texts(40)

    vectors = adapter.embed_descriptions(texts)

    assert endpoint.sizes == [16, 16, 8]
    assert adapter.request_count == 3
    assert vectors == tuple(_vector(text) for text in texts)
    assert endpoint.inputs[0] == texts[:16]
    single = _adapter(_Endpoint())
    assert vectors == tuple(single.embed_description(text) for text in texts)


def test_a_one_text_batch_sends_exactly_the_single_request() -> None:
    batched, single = _Endpoint(), _Endpoint()
    vector = _adapter(batched).embed_descriptions(["only one"])
    assert vector == (_adapter(single).embed_description("only one"),)
    assert batched.inputs == single.inputs == ["only one"]
    assert batched.timeouts == single.timeouts == [30.0]


def test_no_text_sends_nothing_and_blank_text_is_refused_before_any_request() -> None:
    endpoint = _Endpoint()
    adapter = _adapter(endpoint)
    assert adapter.embed_descriptions([]) == ()
    with pytest.raises(ValueError, match="non-empty"):
        adapter.embed_descriptions(["fine", " \n "])
    assert endpoint.inputs == []


def test_vectors_are_aligned_by_index_even_when_returned_out_of_order() -> None:
    endpoint = _Endpoint(reverse=True)
    texts = _texts(5)
    assert _adapter(endpoint).embed_descriptions(texts) == tuple(_vector(text) for text in texts)
    assert endpoint.sizes == [5]


def test_the_character_budget_splits_batches_and_a_long_text_goes_alone() -> None:
    endpoint = _Endpoint()
    texts = ["a" * 40, "b" * 40, "c" * 40, "d" * 500, "e" * 40]
    vectors = _adapter(endpoint, batch_max_chars=100).embed_descriptions(texts)
    assert endpoint.inputs == [texts[:2], texts[2], texts[3], texts[4]]
    assert vectors == tuple(_vector(text) for text in texts)


def test_a_rejected_batch_is_halved_down_to_the_single_request() -> None:
    # The endpoint refuses more than three inputs; the eight split once and both halves pass.
    endpoint = _Endpoint(
        fault=lambda inputs: _http(413) if isinstance(inputs, list) and len(inputs) > 3 else None
    )
    adapter = _adapter(endpoint, batch_max_items=8)
    texts = _texts(8)
    assert adapter.embed_descriptions(texts) == tuple(_vector(text) for text in texts)
    assert endpoint.sizes == [8, 4, 2, 2, 4, 2, 2]
    # Splitting fixed it, so arrays still work: the next call starts at full size again.
    endpoint.inputs.clear()
    adapter.embed_descriptions(_texts(3))
    assert endpoint.sizes == [3]


def test_a_text_the_endpoint_refuses_alone_fails_as_a_single_request_does() -> None:
    poison = "poison text"
    endpoint = _Endpoint(
        fault=lambda inputs: (
            _http(400) if poison in (inputs if isinstance(inputs, list) else [inputs]) else None
        )
    )
    texts = _texts(8)
    texts[5] = poison
    with pytest.raises(ProviderRequestError, match="HTTP 400"):
        _adapter(endpoint, batch_max_items=8).embed_descriptions(texts)
    assert endpoint.sizes == [8, 4, 4, 2, 1, 1]
    assert endpoint.inputs[-1] == poison


def test_an_endpoint_without_array_input_is_detected_once_and_remembered() -> None:
    endpoint = _Endpoint(fault=lambda inputs: _http(400) if isinstance(inputs, list) else None)
    adapter = _adapter(endpoint)
    texts = _texts(20)

    assert adapter.embed_descriptions(texts) == tuple(_vector(text) for text in texts)
    # 16 → 8 → 4 → 2 refused, the two singles answer: arrays are off for this adapter.
    assert endpoint.sizes == [16, 8, 4, 2] + [1] * 20
    endpoint.inputs.clear()
    adapter.embed_descriptions(_texts(10))
    assert endpoint.sizes == [1] * 10


def test_a_400_after_arrays_worked_never_turns_arrays_off() -> None:
    calls = {"count": 0}

    def fault(inputs: list[str] | str) -> ProviderRequestError | None:
        calls["count"] += 1
        return _http(400) if calls["count"] in (2, 3) else None

    endpoint = _Endpoint(fault=fault)
    adapter = _adapter(endpoint, batch_max_items=4)
    texts = _texts(8)
    assert adapter.embed_descriptions(texts) == tuple(_vector(text) for text in texts)
    # The refused pair's singles answer, but the first batch already proved arrays work.
    assert endpoint.sizes == [4, 4, 2, 1, 1, 2]
    endpoint.inputs.clear()
    adapter.embed_descriptions(_texts(4))
    assert endpoint.sizes == [4]


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(lambda n: [(i, 3) for i in range(n - 1)], id="one-vector-short"),
        pytest.param(lambda n: [(0, 3)] * n, id="duplicate-index"),
        pytest.param(lambda n: [(i + 1, 3) for i in range(n)], id="index-outside-batch"),
        pytest.param(lambda n: [(i, 3 + (i == 1)) for i in range(n)], id="mixed-dimensions"),
        pytest.param(lambda n: [(i, 0) for i in range(n)], id="empty-vectors"),
    ],
)
def test_a_malformed_batch_reply_is_a_failure_never_a_misassignment(
    reply: Callable[[int], list[tuple[int, int]]],
) -> None:
    def fault(inputs: list[str] | str) -> bytes | None:
        if not isinstance(inputs, list):
            return None
        data = [
            {"index": index, "embedding": [0.5] * dimensions}
            for index, dimensions in reply(len(inputs))
        ]
        return json.dumps({"data": data}).encode()

    endpoint = _Endpoint(fault=fault)
    adapter = _adapter(endpoint, batch_max_items=2)
    texts = _texts(2)
    assert adapter.embed_descriptions(texts) == tuple(_vector(text) for text in texts)
    assert endpoint.sizes == [2, 1, 1]
    # A malformed reply is not a refusal of arrays.
    endpoint.inputs.clear()
    adapter.embed_descriptions(_texts(2))
    assert endpoint.sizes == [2, 1, 1]


def test_a_non_finite_value_in_a_batch_is_a_failure() -> None:
    def fault(inputs: list[str] | str) -> bytes | None:
        if not isinstance(inputs, list):
            return None
        return b'{"data":[{"index":0,"embedding":[0.5,NaN]},{"index":1,"embedding":[0.5,0.5]}]}'

    endpoint = _Endpoint(fault=fault)
    texts = _texts(2)
    assert _adapter(endpoint).embed_descriptions(texts) == tuple(_vector(t) for t in texts)
    assert endpoint.sizes == [2, 1, 1]
    assert all(math.isfinite(value) for value in _vector(texts[0]))


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_http(503), id="5xx"),
        pytest.param(
            ProviderRequestError("Local model connection failed or timed out", category="timeout"),
            id="timeout",
        ),
    ],
)
def test_a_transient_batch_failure_is_retried_whole_before_any_split(
    error: ProviderRequestError,
) -> None:
    calls = {"count": 0}

    def fault(inputs: list[str] | str) -> ProviderRequestError | None:
        calls["count"] += 1
        return error if calls["count"] == 1 else None

    endpoint = _Endpoint(fault=fault)
    texts = _texts(16)
    assert _adapter(endpoint).embed_descriptions(texts) == tuple(_vector(t) for t in texts)
    assert endpoint.sizes == [16, 16]  # ADR 0035: the same batch again, not two halves


def test_an_endpoint_that_is_down_costs_one_round_of_retries_per_halving_level() -> None:
    endpoint = _Endpoint(fault=lambda inputs: _http(503))
    with pytest.raises(ProviderRequestError, match="HTTP 503"):
        _adapter(endpoint).embed_descriptions(_texts(16))
    attempts = 1 + TRANSIENT_MAX_RETRIES
    assert endpoint.sizes == [size for size in (16, 8, 4, 2, 1) for _ in range(attempts)]


@pytest.mark.parametrize("status", [401, 403, 404])
def test_an_authorization_or_route_failure_is_not_retried(status: int) -> None:
    endpoint = _Endpoint(fault=lambda inputs: _http(status))
    with pytest.raises(ProviderRequestError, match=f"HTTP {status}"):
        _adapter(endpoint).embed_descriptions(_texts(16))
    assert endpoint.sizes == [16]


def test_failed_batch_requests_are_capped_then_batching_stops() -> None:
    # Arrays always fail with 500 while single inputs answer: without a cap every
    # level of every batch would fail once more.
    endpoint = _Endpoint(fault=lambda inputs: _http(500) if isinstance(inputs, list) else None)
    adapter = _adapter(endpoint)
    texts = _texts(64)
    assert adapter.embed_descriptions(texts) == tuple(_vector(t) for t in texts)
    failed = [size for size in endpoint.sizes if size > 1]
    # Each failed batch is one round of transient retries (ADR 0035).
    assert len(failed) == EMBEDDING_BATCH_MAX_FAILURES * (1 + TRANSIENT_MAX_RETRIES)
    assert endpoint.sizes.count(1) == 64
    endpoint.inputs.clear()
    adapter.embed_descriptions(_texts(4))
    assert endpoint.sizes == [1, 1, 1, 1]


def test_a_batch_waits_longer_than_a_single_request() -> None:
    endpoint = _Endpoint()
    _adapter(endpoint).embed_descriptions(_texts(2))
    assert endpoint.timeouts == [60.0]


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="batch"):
        _adapter(_Endpoint(), batch_max_items=0)
    with pytest.raises(ValueError, match="batch"):
        _adapter(_Endpoint(), batch_max_chars=0)
