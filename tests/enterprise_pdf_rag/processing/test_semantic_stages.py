"""A failed branch cannot discard the independent source-based result."""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.providers import load_llm_config
from enterprise_pdf_rag.adapters.semantic_objects import SemanticObjectAdapter
from enterprise_pdf_rag.documents.models import TextSidecar, TextSpan
from enterprise_pdf_rag.figures.models import Confidence, TextDescription
from enterprise_pdf_rag.processing.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    StageState,
)


@pytest.mark.parametrize("valid_claim,valid_binding", [(True, True), (False, True), (True, False)])
def test_chart_failure_keeps_description_and_source_assets_without_qualification(
    tmp_path: Path,
    valid_claim: bool,
    valid_binding: bool,
) -> None:
    sources = LocalDocumentStore(tmp_path / "source")
    native = sources.put(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50"><rect x="0" y="0" width="20" height="20"/></svg>',
        media_type="image/svg+xml",
    )
    page = PageInput(
        "a" * 64,
        "b" * 64,
        0,
        100.0,
        50.0,
        native,
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            0,
            (TextSpan("title", "Revenue", (1.0, 1.0, 40.0, 10.0)),),
        ),
    )
    item = LayoutObject(
        "chart",
        ObjectKind.CHART,
        (0.0, 0.0, 100.0, 50.0),
        ("title",),
        "chart hypothesis",
        Confidence(None, "test"),
    )
    requests: list[bytes] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        requests.append(payload)
        prompt = json.loads(payload)["messages"][1]["content"][0]["text"]
        view = json.loads(prompt.rsplit("\n", 1)[1])
        correction = "correction_of" in view
        if correction:
            view = json.loads(prompt.rsplit("\n", 2)[1])
        if len(requests) == 1:
            content: dict[str, object] = {"invalid_schema": True}
        else:
            source = next(entry for entry in view["observations"] if entry["text"] == "Revenue")
            content = {
                "schema_version": "figure-description-v1",
                "svg_digest": view["svg_digest"] if valid_binding or correction else "f" * 64,
                "claims": [
                    {
                        "text": "Revenue",
                        "evidence": {
                            "element_ids": [source["id"] if valid_claim else "fabricated"],
                            "confidence": "high",
                        },
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
                "OPENAI_API_KEY": "test",
                "OPENAI_BASE_URL": "https://example.invalid",
                "OPENAI_MODEL": "test",
            }
        ),
        cache_dir=tmp_path / "cache",
        max_live_calls=3,
        sender=sender,
    )
    outputs = ProcessingStore(tmp_path / "processing")
    record = SemanticObjectAdapter(sources, outputs, client).process(page, item)
    stages = {stage.stage: stage for stage in record.stages}
    assert len(requests) == 2

    if not valid_binding:
        failure_ref = stages["description_diagnostics"].artifact
        assert failure_ref is not None
        old_request = json.loads(outputs.assets.get(failure_ref))["request_fingerprint"]
        correction_adapter = SemanticObjectAdapter(
            sources, outputs, client, description_corrections=(old_request,)
        )
        corrected = correction_adapter.process(page, item)
        corrected_stages = {stage.stage: stage for stage in corrected.stages}
        assert corrected_stages["description"].state is StageState.SUCCEEDED
        assert (
            corrected_stages["description_original_raw"].artifact
            == stages["description_raw"].artifact
        )
        assert corrected_stages["description_original_diagnostics"].artifact is not None
        assert len(requests) == 3
        assert correction_adapter.process(page, item) == corrected
        assert len(requests) == 3
    assert stages["ir"].state is StageState.FAILED
    assert stages["description"].state is (
        StageState.FAILED
        if not valid_binding
        else StageState.SUCCEEDED
        if valid_claim
        else StageState.UNAVAILABLE
    )
    assert stages["qualification"].state is StageState.UNAVAILABLE
    ref = stages["description"].artifact
    if valid_claim and valid_binding:
        assert ref is not None
        description = TypeAdapter(TextDescription).validate_json(outputs.assets.get(ref))
        assert description.text == "Revenue" and description.verification == "pending"
    else:
        assert ref is None
        assert stages["description_raw"].artifact is not None
        assert stages["description_diagnostics"].artifact is not None
    assert {
        "svg",
        "model_view",
        "model_render",
        "source_text",
        "description_raw",
    } <= set(stages)
    assert record.qualified_claim_count == 0
    assert SemanticObjectAdapter(sources, outputs, client).process(page, item) == record
    assert len(requests) == (2 if valid_binding else 3)


def test_unreadable_chart_keeps_source_svg_and_explicit_unavailable_description(
    tmp_path: Path,
) -> None:
    sources = LocalDocumentStore(tmp_path / "source")
    native = sources.put(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50"><rect width="40" height="30"/></svg>',
        media_type="image/svg+xml",
    )
    page = PageInput(
        "a" * 64,
        "b" * 64,
        0,
        100.0,
        50.0,
        native,
        TextSidecar("source-text-v1", "b" * 64, 0, ()),
    )
    item = LayoutObject(
        "unreadable",
        ObjectKind.CHART,
        (0.0, 0.0, 100.0, 50.0),
        (),
        "unreadable chart",
        Confidence(None, "test"),
    )
    client = JsonCompletionClient(
        load_llm_config(
            {
                "OPENAI_API_KEY": "test",
                "OPENAI_BASE_URL": "https://example.invalid",
                "OPENAI_MODEL": "test",
            }
        ),
        cache_dir=tmp_path / "cache",
        max_live_calls=0,
    )
    outputs = ProcessingStore(tmp_path / "out")
    record = SemanticObjectAdapter(sources, outputs, client).process(page, item)
    stages = {stage.stage: stage for stage in record.stages}
    assert stages["svg"].artifact is not None
    assert stages["ir"].state is StageState.FAILED
    assert stages["description"].state is StageState.UNAVAILABLE
    assert stages["description"].diagnostic


def test_explicit_chart_binding_correction_keeps_original_raw(tmp_path: Path) -> None:
    sources = LocalDocumentStore(tmp_path / "source")
    native = sources.put(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50"/>',
        media_type="image/svg+xml",
    )
    page = PageInput(
        "a" * 64,
        "b" * 64,
        0,
        100.0,
        50.0,
        native,
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            0,
            (TextSpan("label", "Revenue", (1.0, 1.0, 50.0, 10.0)),),
        ),
    )
    item = LayoutObject(
        "chart",
        ObjectKind.CHART,
        (0.0, 0.0, 100.0, 50.0),
        ("label",),
        "chart",
        Confidence(None, "test"),
    )
    calls: list[bytes] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        calls.append(payload)
        prompt = json.loads(payload)["messages"][1]["content"][0]["text"]
        view = json.loads(prompt.rsplit("\n", 1)[1])
        correction = "correction_of" in view
        if correction:
            view = json.loads(prompt.rsplit("\n", 2)[1])
        observation = next(row for row in view["observations"] if row["text"] == "Revenue")
        evidence = {"element_ids": [observation["id"]], "confidence": "high"}
        content: dict[str, object]
        if "chart-observations-v1" in prompt:
            content = {
                "schema_version": "chart-observations-v1",
                "svg_digest": view["svg_digest"] if correction else "f" * 64,
                "grammar": "bar",
                "title": {"text": "Revenue", "evidence": evidence},
                "period": None,
                "axes": [],
                "points": [],
                "marks": [],
                "diagnostics": [],
            }
        else:
            content = {
                "schema_version": "figure-description-v1",
                "svg_digest": view["svg_digest"],
                "claims": [
                    {
                        "text": "Revenue",
                        "evidence": evidence,
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
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(content)},
                    }
                ]
            }
        ).encode()

    config = load_llm_config(
        {
            "OPENAI_API_KEY": "test",
            "OPENAI_BASE_URL": "https://example.invalid",
            "OPENAI_MODEL": "test",
        }
    )
    client = JsonCompletionClient(
        config, cache_dir=tmp_path / "cache", max_live_calls=3, sender=sender
    )
    outputs = ProcessingStore(tmp_path / "out")
    original = SemanticObjectAdapter(sources, outputs, client).process(page, item)
    stages = {stage.stage: stage for stage in original.stages}
    diagnostics = stages["ir_diagnostics"].artifact
    assert diagnostics is not None
    request = json.loads(outputs.assets.get(diagnostics))["request_fingerprint"]
    corrected_adapter = SemanticObjectAdapter(
        sources, outputs, client, chart_corrections=(request,)
    )
    corrected = corrected_adapter.process(page, item)
    fixed = {stage.stage: stage for stage in corrected.stages}
    assert fixed["ir"].state is StageState.SUCCEEDED
    assert fixed["ir_original_raw"].artifact == stages["ir_raw"].artifact
    assert len(calls) == 3
    assert corrected_adapter.process(page, item) == corrected
    assert len(calls) == 3
