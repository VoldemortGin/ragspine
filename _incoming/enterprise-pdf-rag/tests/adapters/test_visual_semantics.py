"""Image, diagram and formula inference stays source-bound and independent."""

import json
import re
from hashlib import sha256
from pathlib import Path

from pydantic import SecretStr

from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient
from enterprise_pdf_rag.adapters.providers import (
    LLMConfig,
    ProviderRequestError,
    SmokeSender,
)
from enterprise_pdf_rag.adapters.visual_semantics import VisualSemanticAdapter
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar, TextSpan
from enterprise_pdf_rag.figures.models import Confidence, Verification
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput
from enterprise_pdf_rag.processing.typed_ir import DiagramIR, FormulaIR, ImageIR


def _source(
    kind: ObjectKind, *, text: tuple[str, ...] = ("Revenue",)
) -> tuple[PageInput, LayoutObject, bytes]:
    native = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"><rect x="10" y="10" width="80" height="80" fill="blue"/></svg>'
    digest = sha256(native).hexdigest()
    spans = tuple(
        TextSpan(f"s{index}", value, (20.0, 20.0 + index * 15, 70.0, 30.0 + index * 15))
        for index, value in enumerate(text)
    )
    page = PageInput(
        "a" * 64,
        "b" * 64,
        4,
        100.0,
        100.0,
        AssetRef(digest, "image/svg+xml", len(native)),
        TextSidecar("source-text-v1", "b" * 64, 4, spans),
    )
    item = LayoutObject(
        "visual-object",
        kind,
        (5.0, 5.0, 95.0, 95.0),
        tuple(span.span_id for span in spans),
        "Model-proposed visual region",
        Confidence(None, "layout inference pending"),
    )
    return page, item, native


def _response(content: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "model": "test-model",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(content)},
                }
            ],
        }
    ).encode()


def _client(tmp_path: Path, sender: SmokeSender) -> JsonCompletionClient:
    return JsonCompletionClient(
        LLMConfig(
            api_key=SecretStr("test-secret"),
            base_url="https://example.invalid",
            model="test-model",
        ),
        cache_dir=tmp_path,
        max_live_calls=2,
        sender=sender,
    )


def _ids(payload: bytes) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"obs-[0-9a-f]{16}", payload.decode())))


def test_image_ir_and_description_are_independent_pending_branches(
    tmp_path: Path,
) -> None:
    requests: list[bytes] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        requests.append(payload)
        ids = _ids(payload)
        body = json.loads(payload)
        digest = (
            body["messages"][1]["content"][0]["text"]
            .split('"svg_digest":"', 1)[1]
            .split('"', 1)[0]
        )
        if len(requests) == 1:
            return _response(
                {
                    "schema_version": "image-observations-v1",
                    "svg_digest": digest,
                    "visible_objects": [
                        {
                            "text": "ir-only-visible-object",
                            "evidence": {
                                "element_ids": [],
                                "confidence": "high",
                            },
                        }
                    ],
                    "observed_label_element_ids": list(ids[:1]),
                    "confidence": "high",
                    "diagnostics": [],
                }
            )
        assert b"ir-only-visible-object" not in payload
        return _response(
            {
                "schema_version": "visual-description-v1",
                "svg_digest": digest,
                "text": "The region visibly contains a blue graphic labeled Revenue.",
                "evidence": {"element_ids": list(ids[:1]), "confidence": "medium"},
                "diagnostics": [],
            }
        )

    page, item, native = _source(ObjectKind.IMAGE)
    result = VisualSemanticAdapter(_client(tmp_path, sender)).infer(
        page=page, item=item, native_svg=native
    )

    assert isinstance(result.ir, ImageIR)
    assert result.ir.visible_objects == ("ir-only-visible-object",)
    assert result.ir.observed_labels[0].text == "Revenue"
    assert result.ir.verification is Verification.PENDING
    assert result.description is not None
    assert result.description.verification is Verification.PENDING
    assert result.description.source_span_ids == ("s0",)
    assert result.ir_confidence is not None
    assert result.ir_confidence.score is None
    assert "high" in result.ir_confidence.method
    assert result.description.confidence.score is None
    assert "medium" in result.description.confidence.method
    assert result.description_index_eligible is False
    assert len(requests) == 2
    assert result.ir_raw_json is not None and result.description_raw_json is not None
    assert result.crop_svg.startswith(b"<svg") and result.model_png.startswith(
        b"\x89PNG"
    )


def test_textless_image_keeps_visual_result_without_inventing_source_text(
    tmp_path: Path,
) -> None:
    calls = 0

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        body = json.loads(payload)
        digest = (
            body["messages"][1]["content"][0]["text"]
            .split('"svg_digest":"', 1)[1]
            .split('"', 1)[0]
        )
        if calls == 1:
            return _response(
                {
                    "schema_version": "image-observations-v1",
                    "svg_digest": digest,
                    "visible_objects": [
                        {
                            "text": "A blue rectangle",
                            "evidence": {"element_ids": [], "confidence": None},
                        }
                    ],
                    "observed_label_element_ids": [],
                    "confidence": None,
                    "diagnostics": ["No readable source text in region"],
                }
            )
        return _response(
            {
                "schema_version": "visual-description-v1",
                "svg_digest": digest,
                "text": "A blue rectangle is visible.",
                "evidence": {"element_ids": [], "confidence": None},
                "diagnostics": ["Visual hypothesis only"],
            }
        )

    page, item, native = _source(ObjectKind.IMAGE, text=())
    result = VisualSemanticAdapter(_client(tmp_path, sender)).infer(
        page=page, item=item, native_svg=native
    )
    assert isinstance(result.ir, ImageIR)
    assert result.ir.observed_labels == ()
    assert result.description is not None
    assert result.description.source_span_ids == ()
    view = json.loads(result.model_view_json)
    assert view["full_span_ids"] == []


def test_first_branch_failure_does_not_discard_independent_description(
    tmp_path: Path,
) -> None:
    calls = 0

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderRequestError(
                "Provider returned HTTP 400; no retry performed",
                status=400,
                category="http",
            )
        ids = _ids(payload)
        body = json.loads(payload)
        digest = (
            body["messages"][1]["content"][0]["text"]
            .split('"svg_digest":"', 1)[1]
            .split('"', 1)[0]
        )
        return _response(
            {
                "schema_version": "visual-description-v1",
                "svg_digest": digest,
                "text": "Revenue is an observed label.",
                "evidence": {"element_ids": list(ids[:1]), "confidence": "low"},
                "diagnostics": [],
            }
        )

    page, item, native = _source(ObjectKind.DIAGRAM)
    result = VisualSemanticAdapter(_client(tmp_path, sender)).infer(
        page=page, item=item, native_svg=native
    )
    assert result.ir is None and result.ir_raw_json is None
    assert result.ir_diagnostic == "provider_http_400"
    assert result.description is not None
    assert result.description_diagnostic is None
    assert calls == 2


def test_model_cannot_bind_ir_to_an_unknown_source_occurrence(tmp_path: Path) -> None:
    calls = 0

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        body = json.loads(payload)
        digest = (
            body["messages"][1]["content"][0]["text"]
            .split('"svg_digest":"', 1)[1]
            .split('"', 1)[0]
        )
        if calls == 1:
            return _response(
                {
                    "schema_version": "image-observations-v1",
                    "svg_digest": digest,
                    "visible_objects": [],
                    "observed_label_element_ids": ["obs-fabricated0000"],
                    "confidence": "0.8",
                    "diagnostics": [],
                }
            )
        ids = _ids(payload)
        return _response(
            {
                "schema_version": "visual-description-v1",
                "svg_digest": digest,
                "text": "Revenue is an observed label.",
                "evidence": {"element_ids": list(ids), "confidence": "0.5"},
                "diagnostics": [],
            }
        )

    page, item, native = _source(ObjectKind.IMAGE)
    result = VisualSemanticAdapter(_client(tmp_path, sender)).infer(
        page=page, item=item, native_svg=native
    )

    assert result.ir is None
    assert result.ir_diagnostic == "unbound_visual_evidence"
    assert result.ir_raw_json is not None
    assert result.description is not None
    assert calls == 2


def test_diagram_edge_label_must_match_its_exact_source_occurrence(
    tmp_path: Path,
) -> None:
    calls = 0

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        ids = _ids(payload)
        body = json.loads(payload)
        digest = (
            body["messages"][1]["content"][0]["text"]
            .split('"svg_digest":"', 1)[1]
            .split('"', 1)[0]
        )
        if calls == 1:
            return _response(
                {
                    "schema_version": "diagram-observations-v1",
                    "svg_digest": digest,
                    "nodes": [
                        {
                            "node_id": "n1",
                            "label": "A",
                            "bbox": [10.0, 10.0, 40.0, 40.0],
                            "evidence": {
                                "element_ids": list(ids[:1]),
                                "confidence": "0.5",
                            },
                        },
                        {
                            "node_id": "n2",
                            "label": "B",
                            "bbox": [60.0, 60.0, 90.0, 90.0],
                            "evidence": {
                                "element_ids": list(ids[-1:]),
                                "confidence": "0.5",
                            },
                        },
                    ],
                    "edges": [
                        {
                            "source_node_id": "n1",
                            "target_node_id": "n2",
                            "label": "invented",
                            "relationship": "connection",
                            "evidence": {
                                "element_ids": list(ids[1:2]),
                                "confidence": "0.5",
                            },
                        }
                    ],
                    "confidence": "0.5",
                    "diagnostics": [],
                }
            )
        return _response(
            {
                "schema_version": "visual-description-v1",
                "svg_digest": digest,
                "text": "The region contains source labels.",
                "evidence": {"element_ids": list(ids), "confidence": "0.5"},
                "diagnostics": [],
            }
        )

    page, item, native = _source(ObjectKind.DIAGRAM, text=("A", "+", "B"))
    result = VisualSemanticAdapter(_client(tmp_path, sender)).infer(
        page=page, item=item, native_svg=native
    )

    assert result.ir is None
    assert result.ir_diagnostic == "invalid_diagram_edge"
    assert result.ir_raw_json is not None
    assert result.description is not None


def test_diagram_and_formula_preserve_structure_as_pending_source_hypotheses(
    tmp_path: Path,
) -> None:
    for kind in (ObjectKind.DIAGRAM, ObjectKind.FORMULA):
        page, item, native = _source(kind, text=("A", "+", "B"))
        calls = 0

        def sender(
            url: str,
            *,
            api_key: str,
            payload: bytes,
            timeout: float,
            object_kind: ObjectKind = kind,
        ) -> bytes:
            nonlocal calls
            calls += 1
            ids = _ids(payload)
            body = json.loads(payload)
            digest = (
                body["messages"][1]["content"][0]["text"]
                .split('"svg_digest":"', 1)[1]
                .split('"', 1)[0]
            )
            if calls == 2:
                return _response(
                    {
                        "schema_version": "visual-description-v1",
                        "svg_digest": digest,
                        "text": "The source region contains observed labels.",
                        "evidence": {"element_ids": list(ids), "confidence": "0.4"},
                        "diagnostics": [],
                    }
                )
            if object_kind is ObjectKind.DIAGRAM:
                return _response(
                    {
                        "schema_version": "diagram-observations-v1",
                        "svg_digest": digest,
                        "nodes": [
                            {
                                "node_id": "n1",
                                "label": "A",
                                "bbox": [10.0, 10.0, 40.0, 40.0],
                                "evidence": {
                                    "element_ids": list(ids[:1]),
                                    "confidence": "0.6",
                                },
                            },
                            {
                                "node_id": "n2",
                                "label": "B",
                                "bbox": [60.0, 60.0, 90.0, 90.0],
                                "evidence": {
                                    "element_ids": list(ids[-1:]),
                                    "confidence": "0.6",
                                },
                            },
                        ],
                        "edges": [
                            {
                                "source_node_id": "n1",
                                "target_node_id": "n2",
                                "label": None,
                                "relationship": "directed connection",
                                "evidence": {"element_ids": [], "confidence": "low"},
                            }
                        ],
                        "confidence": "low",
                        "diagnostics": [],
                    }
                )
            return _response(
                {
                    "schema_version": "formula-observations-v1",
                    "svg_digest": digest,
                    "source_literal_element_ids": list(ids),
                    "normalization_state": "inferred",
                    "latex": "A+B",
                    "confidence": "medium",
                    "diagnostics": [],
                }
            )

        result = VisualSemanticAdapter(_client(tmp_path / kind.value, sender)).infer(
            page=page, item=item, native_svg=native
        )
        if kind is ObjectKind.DIAGRAM:
            assert isinstance(result.ir, DiagramIR)
            assert result.ir.edges[0].verification is Verification.PENDING
        else:
            assert isinstance(result.ir, FormulaIR)
            assert result.ir.source_literal == "A+B"
            assert result.ir.latex == "A+B"
            assert result.ir.source_span_ids == ("s0", "s1", "s2")
