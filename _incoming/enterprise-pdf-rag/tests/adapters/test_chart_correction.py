"""Chart binding repairs remain explicit and validate the actual model response."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from tests.adapters.test_chart_mapping import _dto, _point
from tests.adapters.test_chart_semantics import _prepared

from enterprise_pdf_rag.adapters.chart_semantics import (
    ModelChartExtractor,
    ModelOutputBindingError,
)
from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient
from enterprise_pdf_rag.adapters.providers import load_llm_config


@pytest.mark.parametrize("model_corrects_digest", [True, False])
def test_explicit_chart_correction_uses_the_same_view_and_never_repairs_model_output(
    tmp_path: Path, model_corrects_digest: bool
) -> None:
    prepared = _prepared()
    dto = _dto(prepared, _point(prepared))
    payloads: list[bytes] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        payloads.append(payload)
        digest = (
            prepared.svg.digest
            if len(payloads) > 1 and model_corrects_digest
            else "0" * 63
        )
        content = dto.model_copy(update={"svg_digest": digest}).model_dump_json()
        return json.dumps(
            {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        ).encode()

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
        retry_failed=True,
    )
    original = ModelChartExtractor(client, prepared)
    with pytest.raises(ModelOutputBindingError) as old:
        original.infer(prepared.svg)
    old_record = tmp_path / "requests" / f"{old.value.request_fingerprint}.json"
    old_bytes = old_record.read_bytes()
    correction = ModelChartExtractor(
        client, prepared, correction_of=old.value.request_fingerprint
    )
    if model_corrects_digest:
        result = correction.infer(prepared.svg)
        assert result.correction_of == old.value.request_fingerprint
        assert result.chart.points[0].value.value == Decimal("72")
        assert result.view_id == prepared.model_view_id
        assert correction.infer(prepared.svg).completion.cache_hit
    else:
        for _ in range(2):
            with pytest.raises(ModelOutputBindingError) as failed:
                correction.infer(prepared.svg)
            assert failed.value.correction_of == old.value.request_fingerprint
            assert json.loads(failed.value.json_text)["svg_digest"] == "0" * 63
    assert old_record.read_bytes() == old_bytes
    assert len(payloads) == client.live_call_count == 2
    first, second = (json.loads(payload) for payload in payloads)
    assert first["messages"][1]["content"][1] == second["messages"][1]["content"][1]
    ordinary_field = first["response_format"]["json_schema"]["schema"]["properties"][
        "svg_digest"
    ]
    corrected_field = second["response_format"]["json_schema"]["schema"]["properties"][
        "svg_digest"
    ]
    assert "enum" not in ordinary_field
    assert corrected_field["enum"] == [prepared.svg.digest]
