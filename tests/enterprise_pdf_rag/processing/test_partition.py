"""Model layout is source-bound enrichment, not invented Canonical observations."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import SecretStr

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient
from enterprise_pdf_rag.adapters.page_partition import ModelPagePartitioner
from enterprise_pdf_rag.adapters.providers import LLMConfig
from enterprise_pdf_rag.documents.models import TextSidecar, TextSpan
from enterprise_pdf_rag.processing.models import ObjectKind, PageInput


def test_model_partition_maps_local_aliases_to_exact_observed_occurrences(
    tmp_path: Path,
) -> None:
    store = LocalDocumentStore(tmp_path / "source")
    svg = store.put(b'<svg xmlns="http://www.w3.org/2000/svg"/>', media_type="image/svg+xml")
    page = PageInput(
        "a" * 64,
        "b" * 64,
        17,
        960.0,
        540.0,
        svg,
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            17,
            (
                TextSpan("same-label-one", "72%", (10.0, 10.0, 30.0, 20.0)),
                TextSpan("same-label-two", "72%", (50.0, 10.0, 70.0, 20.0)),
            ),
        ),
    )
    calls: list[dict[str, object]] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        assert url.startswith("https://provider.invalid/")
        assert api_key == "unit-secret" and timeout > 0
        calls.append(json.loads(payload))
        content = {
            "regions": [
                {
                    "region_id": "chart1",
                    "kind": "Chart",
                    "bbox": [1.0, 1.0, 100.0, 100.0],
                    "source_span_ids": ["s0000"],
                    "context_span_ids": ["s0001"],
                    "list_items": [],
                    "list_ordered": None,
                    "parent_id": None,
                    "interpretation": "A candidate chart",
                }
            ],
            "unassigned_span_ids": ["s0001"],
            "diagnostics": ["Second occurrence is outside this chart"],
        }
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": json.dumps(content)},
                        "finish_reason": "stop",
                    }
                ]
            }
        ).encode()

    client = JsonCompletionClient(
        LLMConfig(
            api_key=SecretStr("unit-secret"),
            base_url="https://provider.invalid",
            model="test-model",
        ),
        cache_dir=tmp_path / "model-cache",
        max_live_calls=1,
        sender=sender,
    )
    partitioner = ModelPagePartitioner(
        client, store, renderer=lambda _svg: b"\x89PNG\r\n\x1a\nunit"
    )
    result = partitioner.partition(page)
    assert result.objects[0].kind is ObjectKind.CHART
    assert result.objects[0].source_span_ids == ("same-label-one",)
    assert result.objects[0].context_span_ids == ("same-label-two",)
    assert result.objects[0].verification == "pending"
    assert result.unassigned_span_ids == ("same-label-two",)
    assert result.page_index == 17
    assert partitioner.partition(page) == result
    assert len(calls) == 1


def test_cross_page_and_dropped_source_occurrences_fail_before_processing() -> None:
    from enterprise_pdf_rag.documents.models import AssetRef
    from enterprise_pdf_rag.figures.models import Confidence
    from enterprise_pdf_rag.processing.models import LayoutObject, PagePartition
    from enterprise_pdf_rag.processing.service import validate_partition

    page = PageInput(
        "a" * 64,
        "b" * 64,
        0,
        100.0,
        100.0,
        AssetRef(sha256(b"svg").hexdigest(), "image/svg+xml", 3),
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            0,
            (TextSpan("s0", "actual", (1.0, 1.0, 5.0, 5.0)),),
        ),
    )
    dropped = PagePartition(
        "layout-v1", page.source_manifest_id, page.source_sha256, 0, "test", (), ()
    )
    with pytest.raises(ValueError, match="all source text"):
        validate_partition(page, dropped)
    wrong = PagePartition(
        "layout-v1", page.source_manifest_id, page.source_sha256, 1, "test", (), ("s0",)
    )
    with pytest.raises(ValueError, match="different source page"):
        validate_partition(page, wrong)
    invented = PagePartition(
        "layout-v1",
        page.source_manifest_id,
        page.source_sha256,
        0,
        "test",
        (
            LayoutObject(
                "wrong",
                ObjectKind.TEXT,
                (0.0, 0.0, 10.0, 10.0),
                ("invented",),
                "bad",
                Confidence(None, "test"),
            ),
        ),
        ("s0",),
    )
    with pytest.raises(ValueError, match="all source text"):
        validate_partition(page, invented)


def test_page_geometry_tolerates_model_rendered_float_noise_but_not_real_overreach() -> None:
    from enterprise_pdf_rag.documents.models import AssetRef
    from enterprise_pdf_rag.figures.models import Confidence
    from enterprise_pdf_rag.processing.models import LayoutObject, PagePartition
    from enterprise_pdf_rag.processing.service import validate_partition

    page = PageInput(
        "a" * 64,
        "b" * 64,
        0,
        100.0,
        100.0,
        AssetRef(sha256(b"svg").hexdigest(), "image/svg+xml", 3),
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            0,
            (TextSpan("s0", "actual", (1.0, 1.0, 5.0, 5.0)),),
        ),
    )

    def partition(bbox: tuple[float, float, float, float]) -> PagePartition:
        return PagePartition(
            "layout-v1",
            page.source_manifest_id,
            page.source_sha256,
            0,
            "test",
            (LayoutObject("text", ObjectKind.TEXT, bbox, ("s0",), "ok", Confidence(None, "t")),),
            (),
        )

    validate_partition(page, partition((0.0, 0.0, 100.00000000000001, 100.0)))
    with pytest.raises(ValueError, match="outside source page geometry"):
        validate_partition(page, partition((0.0, 0.0, 100.5, 100.0)))
