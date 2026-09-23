"""Source-binding corrections are explicit, cache-linked, single new attempts."""

import json
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.chart_semantics import (
    ModelDescriptionGenerator,
    ModelOutputBindingError,
)
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
)
from ragspine.common.evidence.providers.providers import ProviderRequestError, load_llm_config
from tests.enterprise_pdf_rag.adapters.test_chart_semantics import _prepared


def _response(prepared: PreparedFigure, digest: str) -> bytes:
    element = next(element for element in prepared.svg.elements if element.text == "VONB")
    content = {
        "schema_version": "figure-description-v1",
        "svg_digest": digest,
        "claims": [
            {
                "text": "VONB",
                "evidence": {"element_ids": [element.element_id], "confidence": "high"},
                "series": None,
                "category": None,
                "unit": None,
                "value": None,
                "period": None,
            }
        ],
        "diagnostics": [],
    }
    return json.dumps(
        {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}
    ).encode()


def test_explicit_binding_correction_preserves_old_result_and_reuses_new_attempt(
    tmp_path: Path,
) -> None:
    prepared = _prepared()
    payloads: list[bytes] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        payloads.append(payload)
        return _response(prepared, "0" * 64 if len(payloads) == 1 else prepared.svg.digest)

    client = JsonCompletionClient(
        load_llm_config(
            {
                "OPENAI_API_KEY": "test",
                "OPENAI_BASE_URL": "https://test.invalid",
                "OPENAI_MODEL": "test",
            }
        ),
        cache_dir=tmp_path,
        max_live_calls=3,
        sender=sender,
    )
    ordinary = ModelDescriptionGenerator(client, prepared)
    with pytest.raises(ModelOutputBindingError) as old:
        ordinary.infer(prepared.svg)
    original = tmp_path / "requests" / f"{old.value.request_fingerprint}.json"
    old_bytes = original.read_bytes()
    corrected = ModelDescriptionGenerator(
        client, prepared, correction_of=old.value.request_fingerprint
    )
    result = corrected.infer(prepared.svg)
    assert result.description is not None and result.description.text == "VONB"
    assert result.correction_of == old.value.request_fingerprint
    assert result.completion.request_fingerprint != result.correction_of
    assert result.view_id == prepared.model_view_id
    assert corrected.infer(prepared.svg).completion.cache_hit
    with pytest.raises(ModelOutputBindingError) as replay:
        ordinary.infer(prepared.svg)
    assert replay.value.json_text == old.value.json_text
    assert original.read_bytes() == old_bytes
    assert len(payloads) == client.live_call_count == 2
    first, second = (json.loads(value) for value in payloads)
    assert first["messages"][1]["content"][1] == second["messages"][1]["content"][1]
    assert old.value.request_fingerprint in second["messages"][1]["content"][0]["text"]
    assert prepared.svg.digest in second["messages"][1]["content"][0]["text"]


@pytest.mark.parametrize("prior", ["absent", "different-reference", "already-correct"])
def test_correction_requires_the_matching_cached_binding_failure(
    tmp_path: Path, prior: str
) -> None:
    prepared = _prepared()
    calls = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        calls.append(payload)
        return _response(prepared, prepared.svg.digest if prior == "already-correct" else "0" * 64)

    client = JsonCompletionClient(
        load_llm_config(
            {
                "OPENAI_API_KEY": "test",
                "OPENAI_BASE_URL": "https://test.invalid",
                "OPENAI_MODEL": "test",
            }
        ),
        cache_dir=tmp_path,
        max_live_calls=2,
        sender=sender,
    )
    reference = "f" * 64
    if prior == "different-reference":
        with pytest.raises(ModelOutputBindingError):
            ModelDescriptionGenerator(client, prepared).infer(prepared.svg)
    elif prior == "already-correct":
        original = ModelDescriptionGenerator(client, prepared).infer(prepared.svg)
        reference = original.completion.request_fingerprint
    before = len(calls)
    with pytest.raises(
        JsonCompletionError,
        match=r"cache_miss|correction_reference_mismatch|correction_not_needed",
    ):
        ModelDescriptionGenerator(client, prepared, correction_of=reference).infer(prepared.svg)
    assert len(calls) == before


def test_failed_correction_is_not_retried_by_a_client_with_retry_enabled(
    tmp_path: Path,
) -> None:
    prepared = _prepared()
    calls = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        calls.append(payload)
        if len(calls) == 1:
            return _response(prepared, "0" * 64)
        raise ProviderRequestError("bounded correction failed", status=429, category="http")

    client = JsonCompletionClient(
        load_llm_config(
            {
                "OPENAI_API_KEY": "test",
                "OPENAI_BASE_URL": "https://test.invalid",
                "OPENAI_MODEL": "test",
            }
        ),
        cache_dir=tmp_path,
        max_live_calls=4,
        sender=sender,
        retry_failed=True,
    )
    with pytest.raises(ModelOutputBindingError) as old:
        ModelDescriptionGenerator(client, prepared).infer(prepared.svg)
    correction = ModelDescriptionGenerator(
        client, prepared, correction_of=old.value.request_fingerprint
    )
    for _ in range(2):
        with pytest.raises(JsonCompletionError, match="provider_http_429"):
            correction.infer(prepared.svg)
    assert len(calls) == client.live_call_count == 2
    assert not tuple((tmp_path / "requests").glob("*.retry-1.json"))
