"""Model layout is source-bound enrichment, not invented Canonical observations."""

import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import SecretStr

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.page_partition import ModelPagePartitioner
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
)
from ragspine.common.evidence.providers.providers import LLMConfig
from ragspine.extraction.evidence.document.models import TextSidecar, TextSpan
from ragspine.extraction.evidence.page.models import ObjectKind, PageInput


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


def _budget_page(store: LocalDocumentStore) -> PageInput:
    svg = store.put(b'<svg xmlns="http://www.w3.org/2000/svg"/>', media_type="image/svg+xml")
    spans = (TextSpan("only-span", "Revenue", (10.0, 10.0, 30.0, 20.0)),)
    return PageInput(
        "a" * 64, "b" * 64, 3, 960.0, 540.0, svg, TextSidecar("source-text-v1", "b" * 64, 3, spans)
    )


def _budget_client(
    cache_dir: Path, max_live_calls: int, calls: list[bytes]
) -> JsonCompletionClient:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        calls.append(payload)
        content = {
            "regions": [
                {
                    "region_id": "text1",
                    "kind": "Text",
                    "bbox": [1.0, 1.0, 100.0, 100.0],
                    "source_span_ids": ["s0000"],
                    "context_span_ids": [],
                    "list_items": [],
                    "list_ordered": None,
                    "parent_id": None,
                    "interpretation": "A text block",
                }
            ],
            "unassigned_span_ids": [],
            "diagnostics": [],
        }
        message = {"message": {"content": json.dumps(content)}, "finish_reason": "stop"}
        return json.dumps({"choices": [message]}).encode()

    return JsonCompletionClient(
        LLMConfig(
            api_key=SecretStr("unit-secret"),
            base_url="https://provider.invalid",
            model="test-model",
        ),
        cache_dir=cache_dir,
        max_live_calls=max_live_calls,
        sender=sender,
    )


def _counting_renderer(renders: list[bytes]) -> Callable[[bytes], bytes]:
    def render(svg: bytes) -> bytes:
        renders.append(svg)
        return b"\x89PNG\r\n\x1a\nunit"

    return render


def test_spent_budget_and_empty_model_cache_skip_the_page_render(tmp_path: Path) -> None:
    """No live call left and no record to replay: the call can only be refused, so the PNG
    the request would carry is never rendered; the refusal is the one the client raises."""
    store = LocalDocumentStore(tmp_path / "source")
    page = _budget_page(store)
    calls: list[bytes] = []
    renders: list[bytes] = []
    client = _budget_client(tmp_path / "model-cache", 0, calls)
    partitioner = ModelPagePartitioner(client, store, renderer=_counting_renderer(renders))
    with pytest.raises(JsonCompletionError) as refused:
        partitioner.partition(page)
    assert refused.value.code == "call_budget_exhausted"
    assert str(refused.value) == "call_budget_exhausted"
    assert renders == [] and calls == []


def test_spent_budget_still_renders_and_replays_a_cached_layout(tmp_path: Path) -> None:
    """ADR 0024: the cached reply is found by a fingerprint over the image, so a cache that
    holds records keeps rendering; a cached page replays with no budget at all."""
    store = LocalDocumentStore(tmp_path / "source")
    page = _budget_page(store)
    calls: list[bytes] = []
    first_renders: list[bytes] = []
    first = ModelPagePartitioner(
        _budget_client(tmp_path / "model-cache", 1, calls),
        store,
        renderer=_counting_renderer(first_renders),
    ).partition(page)
    renders: list[bytes] = []
    client = _budget_client(tmp_path / "model-cache", 0, calls)
    replayed = ModelPagePartitioner(client, store, renderer=_counting_renderer(renders)).partition(
        page
    )
    assert replayed == first
    assert len(calls) == 1 and len(first_renders) == 1 and len(renders) == 1
    assert client.cache_hit_count == 1 and client.live_call_count == 0


def test_spent_budget_with_other_cache_records_renders_and_is_refused_alike(
    tmp_path: Path,
) -> None:
    """A record for another request still forces the render (it might have been this one);
    the refusal is byte-for-byte the one the skipped render raises."""
    store = LocalDocumentStore(tmp_path / "source")
    other = PageInput(
        "a" * 64,
        "b" * 64,
        4,
        960.0,
        540.0,
        store.put(b'<svg xmlns="http://www.w3.org/2000/svg"> </svg>', media_type="image/svg+xml"),
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            4,
            (TextSpan("other-span", "Revenue", (10.0, 10.0, 30.0, 20.0)),),
        ),
    )
    calls: list[bytes] = []
    ModelPagePartitioner(
        _budget_client(tmp_path / "model-cache", 1, calls),
        store,
        renderer=_counting_renderer([]),
    ).partition(other)
    renders: list[bytes] = []
    partitioner = ModelPagePartitioner(
        _budget_client(tmp_path / "model-cache", 0, calls),
        store,
        renderer=_counting_renderer(renders),
    )
    with pytest.raises(JsonCompletionError) as refused:
        partitioner.partition(_budget_page(store))
    assert refused.value.code == "call_budget_exhausted"
    assert str(refused.value) == "call_budget_exhausted"
    assert len(renders) == 1 and len(calls) == 1


def test_spent_budget_and_empty_cache_report_an_oversized_page_as_budget_deferred(
    tmp_path: Path,
) -> None:
    """The one deviation ADR 0041 accepts: a page whose PNG would exceed the request's image
    budget reads ``call_budget_exhausted`` while no call can be made, and its real code
    (``input_budget_exceeded``) the first time a record or a live call makes the render count."""
    store = LocalDocumentStore(tmp_path / "source")
    page = _budget_page(store)
    oversized = b"\x89PNG\r\n\x1a\n" + b"0" * 512_001
    with pytest.raises(JsonCompletionError, match=r"^call_budget_exhausted$"):
        ModelPagePartitioner(
            _budget_client(tmp_path / "model-cache", 0, []), store, renderer=lambda _: oversized
        ).partition(page)
    with pytest.raises(JsonCompletionError, match=r"^input_budget_exceeded$"):
        ModelPagePartitioner(
            _budget_client(tmp_path / "model-cache", 1, []), store, renderer=lambda _: oversized
        ).partition(page)


def test_cross_page_and_dropped_source_occurrences_fail_before_processing() -> None:
    from ragspine.extraction.evidence.document.models import AssetRef
    from ragspine.extraction.evidence.figures.models import Confidence
    from ragspine.extraction.evidence.page.models import LayoutObject, PagePartition
    from ragspine.extraction.evidence.page.service import validate_partition

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
    from ragspine.extraction.evidence.document.models import AssetRef
    from ragspine.extraction.evidence.figures.models import Confidence
    from ragspine.extraction.evidence.page.models import LayoutObject, PagePartition
    from ragspine.extraction.evidence.page.service import validate_partition

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
