"""Pinned AIA regions are candidates with occurrence-bound source evidence."""

import json
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

from enterprise_pdf_rag.adapters.aia_candidates import candidates_for
from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar, TextSpan
from enterprise_pdf_rag.figures.models import Verification
from enterprise_pdf_rag.processing.models import ObjectKind, PageInput

CATALOG = Path("src/enterprise_pdf_rag/resources/aia-first-20-regions.json")
SOURCE_SHA256 = "df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e"


def _page(page_index: int, span_ids: tuple[str, ...]) -> PageInput:
    spans = tuple(
        TextSpan(span_id, f"source-{index}", (10.0, 10.0, 20.0, 20.0))
        for index, span_id in enumerate(span_ids)
    )
    svg = b"<svg/>"
    return PageInput(
        "a" * 64,
        SOURCE_SHA256,
        page_index,
        960.0,
        540.0,
        AssetRef(sha256(svg).hexdigest(), "image/svg+xml", len(svg)),
        TextSidecar("source-text-v1", SOURCE_SHA256, page_index, spans),
    )


def _catalog() -> dict[str, object]:
    return cast(dict[str, object], json.loads(CATALOG.read_text()))


def _entries(page_index: int) -> list[dict[str, object]]:
    candidates = cast(list[dict[str, object]], _catalog()["candidates"])
    return [item for item in candidates if item["page_index"] == page_index]


def _page_occurrences(page_index: int) -> tuple[str, ...]:
    values: list[str] = []
    for entry in _entries(page_index):
        values.extend(cast(list[str], entry["source_span_ids"]))
        values.extend(cast(list[str], entry["page_context_span_ids"]))
    return tuple(dict.fromkeys(values))


def test_p18_locked_region_has_stable_id_and_excludes_clipped_neighbor() -> None:
    entries = _entries(17)
    locked = next(
        item for item in entries if item["object_id"] == "aia-p018-distribution-mix-v1"
    )
    ids = tuple(cast(list[str], locked["source_span_ids"]))

    result = candidates_for(_page(17, _page_occurrences(17)))

    item = next(value for value in result if value.object_id == locked["object_id"])
    assert item.kind is ObjectKind.CHART
    assert item.bbox == (18.0, 155.0, 250.0, 338.0)
    assert item.source_span_ids == ids
    assert item.verification is Verification.PENDING
    assert len(item.source_span_ids) == 7
    assert (
        "span-v1-34c623924f14d0a1b2c4e2f1c4814788e030766f21139b091d2143809020ae0f"
        not in item.source_span_ids
    )


def test_missing_or_wrong_source_identity_rejects_entire_candidate_set() -> None:
    entry = _entries(17)[0]
    ids = tuple(cast(list[str], entry["source_span_ids"]))
    missing = _page(17, ids[:-1])
    with pytest.raises(ValueError, match="unknown source occurrences"):
        candidates_for(missing)

    wrong = _page(17, ids)
    object.__setattr__(wrong, "source_sha256", "0" * 64)
    with pytest.raises(ValueError, match="pinned AIA source"):
        candidates_for(wrong)


def test_p20_reviewed_group_keeps_table_alternative_and_context_out_of_crop() -> None:
    entry = next(
        item
        for item in _entries(19)
        if item["object_id"] == "aia-p020-sensitivity-scenarios-v1"
    )
    context_ids = tuple(cast(list[str], entry["page_context_span_ids"]))
    result = candidates_for(_page(19, _page_occurrences(19)))
    item = next(value for value in result if value.object_id == entry["object_id"])

    assert item.kind is ObjectKind.GROUP
    assert item.verification is Verification.PENDING
    assert "root-source-render-review" in item.confidence.method
    assert item.context_span_ids == context_ids
    assert set(item.source_span_ids).isdisjoint(context_ids)
    assert entry["alternate_kind_candidate"] == "Table"
    assert entry["native_table_status"] == "unavailable"


def test_catalog_is_provisional_and_all_actual_occurrences_resolve_when_available() -> (
    None
):
    payload = _catalog()
    assert payload["status"] == "manual_candidates_pending_not_gold"
    candidates = cast(list[dict[str, object]], payload["candidates"])
    assert all(item["gold"] is False for item in candidates)
    assert all(item["verification"] == "pending" for item in candidates)
    assert len({item["object_id"] for item in candidates}) == len(candidates)

    current = Path("data/output/aia-2026-interim/current-manifest")
    if not current.is_file():
        pytest.skip("Optional ignored AIA source output is absent")
    store = LocalDocumentStore(Path("data/output/aia-2026-interim"))
    snapshot = store.load(current.read_text().strip())
    for page_index in sorted({cast(int, item["page_index"]) for item in candidates}):
        record = snapshot.manifest.pages[page_index]
        page = PageInput(
            snapshot.manifest_id,
            snapshot.manifest.source.sha256,
            page_index,
            record.width,
            record.height,
            record.svg,
            read_text_sidecar(store, snapshot, page_index),
        )
        assert len(candidates_for(page)) == len(_entries(page_index))
