"""Independent producers consume one source view, without consuming each other."""

import base64
import json
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.chart_semantics import (
    ModelChartExtractor,
    ModelDescriptionGenerator,
)
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure, prepare_figure
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar, TextSpan
from enterprise_pdf_rag.figures.models import ExecutionMode, ValueKind, Verification
from enterprise_pdf_rag.processing.models import PageInput
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
)
from ragspine.common.evidence.providers.providers import load_llm_config


def _prepared(
    *, additional_literal: str | None = None, page_context: bool = False
) -> PreparedFigure:
    native = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50"><path fill="red" d="M0 0H100V50H0Z"/></svg>'
    source = sha256(b"test-pdf").hexdigest()
    labels: tuple[str, ...] = ("VONB", "Agency", "72%", "1H26")
    if additional_literal is not None:
        labels = (*labels, additional_literal)
    spans = tuple(
        TextSpan(f"s-{index}", text, (float(index), 1.0, float(index + 1), 2.0))
        for index, text in enumerate(labels)
    )
    if page_context:
        spans = (
            *spans,
            TextSpan(
                "footer",
                "Year-on-year changes use constant FX.",
                (5.0, 44.0, 95.0, 49.0),
            ),
        )
    page = PageInput(
        "a" * 64,
        source,
        17,
        100.0,
        50.0,
        AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
        TextSidecar("source-text-v1", source, 17, spans),
    )
    return prepare_figure(
        page=page,
        native_svg=native,
        bbox=(0.0, 0.0, 100.0, 40.0),
        region_id="pilot",
        context_span_ids=("footer",) if page_context else (),
    )


@pytest.mark.parametrize("confidence", ["0.99", "high", "source_text_observation"])
@pytest.mark.parametrize("page_context", [False, True])
def test_independent_branches_bind_the_same_svg_and_do_not_promote_model_confidence(
    tmp_path: Path,
    confidence: str,
    page_context: bool,
) -> None:
    prepared = _prepared(page_context=page_context)
    ids = {element.text: element.element_id for element in prepared.svg.elements}

    def evidence(text: str) -> dict[str, object]:
        return {"element_ids": [ids[text]], "confidence": confidence}

    def field(text: str) -> dict[str, object]:
        return {"text": text, "evidence": evidence(text)}

    chart = {
        "schema_version": "chart-observations-v1",
        "svg_digest": prepared.svg.digest,
        "grammar": "donut",
        "title": None,
        "period": field("1H26"),
        "axes": [],
        "points": [
            {
                "point_id": "model-only-point-id",
                "series": field("VONB"),
                "category": field("Agency"),
                "unit": field("%"),
                "value": {
                    "value": "72",
                    "kind": "explicit",
                    "evidence": evidence("72"),
                },
            }
        ],
        "marks": [],
        "diagnostics": [],
    }
    description = {
        "schema_version": "figure-description-v1",
        "svg_digest": prepared.svg.digest,
        "claims": [
            {
                "text": "During 1H26, VONB for Agency: 72 %.",
                "evidence": {
                    "element_ids": [ids[text] for text in ("VONB", "Agency", "72", "%", "1H26")],
                    "confidence": confidence,
                },
                "series": "VONB",
                "category": "Agency",
                "unit": "%",
                "value": "72",
                "period": "1H26",
            }
        ],
        "diagnostics": [],
    }
    payloads: list[dict[str, object]] = []
    replies = iter((chart, description))

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        payloads.append(json.loads(payload))
        return json.dumps(
            {
                "model": "test-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(next(replies))},
                    }
                ],
            }
        ).encode()

    config = load_llm_config(
        {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://test.invalid",
            "OPENAI_MODEL": "test-model",
        }
    )
    client = JsonCompletionClient(config, cache_dir=tmp_path, max_live_calls=2, sender=sender)
    chart_result = ModelChartExtractor(client, prepared).infer(prepared.svg)
    description_result = ModelDescriptionGenerator(client, prepared).infer(prepared.svg)
    assert description_result.description is not None
    assert (
        chart_result.chart.binding == description_result.description.binding == prepared.svg.binding
    )
    assert chart_result.chart.verification is Verification.PENDING
    assert description_result.description.verification is Verification.PENDING
    assert chart_result.chart.execution_mode is ExecutionMode.PRODUCTION
    assert chart_result.chart.points[0].value.evidence.verification is Verification.PENDING
    assert chart_result.chart.points[0].value.value == Decimal("72")
    if confidence == "high":
        assert chart_result.chart.points[0].value.evidence.confidence.score is None
        assert "ordinal=high" in chart_result.chart.points[0].value.evidence.confidence.method
    elif confidence == "source_text_observation":
        assert chart_result.chart.points[0].value.evidence.confidence.score is None
        assert description_result.description.claims[0].evidence.confidence.score is None
    assert chart_result.completion.output_digest != description_result.completion.output_digest
    assert "model-only-point-id" not in json.dumps(payloads[1])
    assert all(
        base64.b64encode(prepared.rendered.png).decode() in json.dumps(payload)
        for payload in payloads
    )
    assert chart_result.view_id == description_result.view_id == prepared.model_view_id
    assert all(
        ("Year-on-year changes use constant FX." in json.dumps(payload)) is page_context
        for payload in payloads
    )
    assert all(element.source_span_id != "footer" for element in prepared.svg.elements)


def test_changed_source_is_rejected_before_any_model_call(tmp_path: Path) -> None:
    prepared = _prepared()
    client = JsonCompletionClient(
        load_llm_config(
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": "https://test.invalid",
                "OPENAI_MODEL": "test-model",
            }
        ),
        cache_dir=tmp_path,
        max_live_calls=0,
    )
    changed = replace(prepared.svg, figure_id="different-source")
    with pytest.raises(JsonCompletionError, match="model_view_binding_mismatch"):
        ModelChartExtractor(client, prepared).extract(changed)
    with pytest.raises(JsonCompletionError, match="model_view_binding_mismatch"):
        ModelDescriptionGenerator(client, prepared).generate(changed)
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("branch", ["chart", "description"])
def test_model_cannot_rebind_observations_to_another_svg(tmp_path: Path, branch: str) -> None:
    prepared = _prepared()

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        content: dict[str, object] = {
            "schema_version": "chart-observations-v1",
            "svg_digest": "0" * 64,
            "grammar": "unknown",
            "title": None,
            "period": None,
            "axes": [],
            "points": [],
            "marks": [],
            "diagnostics": ["unrecognized"],
        }
        if branch == "description":
            content = {
                "schema_version": "figure-description-v1",
                "svg_digest": "0" * 64,
                "claims": [],
                "diagnostics": ["unrecognized"],
            }
        return json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(content)},
                    }
                ]
            }
        ).encode()

    client = JsonCompletionClient(
        load_llm_config(
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": "https://test.invalid",
                "OPENAI_MODEL": "test-model",
            }
        ),
        cache_dir=tmp_path,
        max_live_calls=1,
        sender=sender,
    )
    with pytest.raises(JsonCompletionError, match="model_svg_binding_mismatch") as raised:
        if branch == "chart":
            ModelChartExtractor(client, prepared).extract(prepared.svg)
        else:
            ModelDescriptionGenerator(client, prepared).generate(prepared.svg)
    raw = getattr(raised.value, "json_text", None)
    assert isinstance(raw, str)
    assert json.loads(raw)["svg_digest"] == "0" * 64
    assert getattr(raised.value, "output_digest", None) == sha256(raw.encode()).hexdigest()
    assert raw not in str(raised.value)
    assert len(tuple((tmp_path / "responses").iterdir())) == 1


@pytest.mark.parametrize("literal", ["<0.1", ">3.4", "(0.2)", "(3.4)%"])
@pytest.mark.parametrize("model_removes_operator", [False, True])
def test_unsupported_financial_literal_preserves_raw_input_and_other_chart_points(
    tmp_path: Path, literal: str, model_removes_operator: bool
) -> None:
    prepared = _prepared(additional_literal=literal)
    ids = {element.text: element.element_id for element in prepared.svg.elements}

    def field(text: str) -> dict[str, object]:
        return {
            "text": text,
            "evidence": {"element_ids": [ids[text]], "confidence": None},
        }

    def point(point_id: str, value: str, observed_literal: str) -> dict[str, object]:
        return {
            "point_id": point_id,
            "series": field("VONB"),
            "category": field("Agency"),
            "unit": field("%"),
            "value": {
                "value": value,
                "kind": "explicit",
                "evidence": {
                    "element_ids": [ids[observed_literal]],
                    "confidence": None,
                },
            },
        }

    model_literal = "0.1" if model_removes_operator else literal
    content = {
        "schema_version": "chart-observations-v1",
        "svg_digest": prepared.svg.digest,
        "grammar": "bar",
        "title": None,
        "period": None,
        "axes": [],
        "points": [
            point("plain", "72", "72"),
            point("financial-literal", model_literal, literal),
        ],
        "marks": [],
        "diagnostics": [],
    }

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        return json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(content)},
                    }
                ]
            }
        ).encode()

    client = JsonCompletionClient(
        load_llm_config(
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": "https://test.invalid",
                "OPENAI_MODEL": "test-model",
            }
        ),
        cache_dir=tmp_path,
        max_live_calls=1,
        sender=sender,
    )
    result = ModelChartExtractor(client, prepared).infer(prepared.svg)
    assert result.chart.points[0].value.value == Decimal("72")
    unsupported = result.chart.points[1].value
    assert unsupported.value is None and unsupported.kind is ValueKind.UNAVAILABLE
    assert unsupported.evidence.verification is Verification.PENDING
    assert result.completion.parsed.points[1].value.value == model_literal
    assert unsupported.evidence.element_ids == (ids[literal],)
    assert result.diagnostics == (
        "points.financial-literal.value:unsupported_comparison_or_accounting_literal; raw model literal retained",
    )
