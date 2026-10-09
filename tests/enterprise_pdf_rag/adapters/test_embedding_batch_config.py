"""APP_EMBEDDING_BATCH_MAX_ITEMS / _CHARS drive the per-request batch limits (ADR 0026)."""

import pytest
from pydantic import ValidationError

from ragspine.common.evidence.configs import Settings
from ragspine.common.evidence.providers.local_models import (
    EMBEDDING_BATCH_MAX_CHARS,
    EMBEDDING_BATCH_MAX_ITEMS,
    LocalEmbeddingAdapter,
)
from ragspine.common.evidence.providers.providers import load_local_model_config
from tests.enterprise_pdf_rag.adapters.test_embedding_batches import (
    _CONFIG,
    _adapter,
    _Endpoint,
    _http,
    _texts,
    _vector,
)


def _env_adapter(endpoint: _Endpoint) -> LocalEmbeddingAdapter:
    return LocalEmbeddingAdapter(load_local_model_config("embedding", _CONFIG), sender=endpoint)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_EMBEDDING_BATCH_MAX_ITEMS", raising=False)
    monkeypatch.delenv("APP_EMBEDDING_BATCH_MAX_CHARS", raising=False)


def test_unset_config_keeps_the_16_and_48000_defaults() -> None:
    settings = Settings()
    assert settings.embedding_batch_max_items == EMBEDDING_BATCH_MAX_ITEMS == 16
    assert settings.embedding_batch_max_chars == EMBEDDING_BATCH_MAX_CHARS == 48_000
    endpoint = _Endpoint()
    _env_adapter(endpoint).embed_descriptions(_texts(40))
    assert endpoint.sizes == [16, 16, 8]


def test_items_setting_of_64_cuts_100_texts_into_64_and_36(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_EMBEDDING_BATCH_MAX_ITEMS", "64")
    endpoint = _Endpoint()
    texts = _texts(100)
    adapter = _env_adapter(endpoint)
    assert adapter.batch_max_items == 64
    assert adapter.embed_descriptions(texts) == tuple(_vector(text) for text in texts)
    assert endpoint.sizes == [64, 36]


def test_chars_setting_wins_when_it_fills_before_the_items_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_EMBEDDING_BATCH_MAX_ITEMS", "64")
    monkeypatch.setenv("APP_EMBEDDING_BATCH_MAX_CHARS", "1000")
    endpoint = _Endpoint()
    _env_adapter(endpoint).embed_descriptions(["x" * 400] * 5)
    assert endpoint.sizes == [2, 2, 1]


def test_a_failing_batch_of_128_halves_down_to_single_inputs() -> None:
    endpoint = _Endpoint(fault=lambda inputs: _http(500) if isinstance(inputs, list) else None)
    texts = _texts(128)
    vectors = _adapter(endpoint, batch_max_items=128).embed_descriptions(texts)
    assert vectors == tuple(_vector(text) for text in texts)
    # A 500 is retried in place (ADR 0035), so each size repeats before the next halving.
    halving = [
        size for i, size in enumerate(endpoint.sizes) if i == 0 or endpoint.sizes[i - 1] != size
    ]
    assert halving[:7] == [128, 64, 32, 16, 8, 4, 2]
    assert endpoint.sizes.count(1) >= 2


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_EMBEDDING_BATCH_MAX_ITEMS", "0"),
        ("APP_EMBEDDING_BATCH_MAX_ITEMS", "1025"),
        ("APP_EMBEDDING_BATCH_MAX_CHARS", "999"),
        ("APP_EMBEDDING_BATCH_MAX_ITEMS", "many"),
    ],
)
def test_out_of_range_values_fail_with_the_setting_named(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=name.removeprefix("APP_").lower()):
        _env_adapter(_Endpoint())


def test_range_edges_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_EMBEDDING_BATCH_MAX_ITEMS", "1024")
    monkeypatch.setenv("APP_EMBEDDING_BATCH_MAX_CHARS", "1000")
    assert _env_adapter(_Endpoint()).batch_max_items == 1024
    monkeypatch.setenv("APP_EMBEDDING_BATCH_MAX_ITEMS", "1")
    assert _env_adapter(_Endpoint()).batch_max_items == 1
