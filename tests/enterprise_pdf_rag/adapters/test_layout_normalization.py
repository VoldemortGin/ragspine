"""Reviewed layout corrections remain distinct from immutable model output."""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

from enterprise_pdf_rag.adapters.layout_normalization import (
    NORMALIZATION_VERSION,
    normalize_partition,
)
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar, TextSpan
from enterprise_pdf_rag.figures.models import Confidence, Verification
from enterprise_pdf_rag.processing.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    PagePartition,
)


def _page(*, source: str = "b" * 64, page_index: int = 0, spans: tuple[TextSpan, ...]) -> PageInput:
    svg = b"<svg/>"
    return PageInput(
        "a" * 64,
        source,
        page_index,
        960.0,
        540.0,
        AssetRef(sha256(svg).hexdigest(), "image/svg+xml", len(svg)),
        TextSidecar("source-text-v1", source, page_index, spans),
    )


def _object(
    object_id: str,
    kind: ObjectKind,
    bbox: tuple[float, float, float, float],
    span_ids: tuple[str, ...],
) -> LayoutObject:
    return LayoutObject(
        object_id,
        kind,
        bbox,
        span_ids,
        "model inference",
        Confidence(None, "uncalibrated model layout inference"),
    )


def _partition(page: PageInput, objects: tuple[LayoutObject, ...]) -> PagePartition:
    assigned = {span_id for item in objects for span_id in item.source_span_ids}
    return PagePartition(
        "layout-enrichment-v2",
        page.source_manifest_id,
        page.source_sha256,
        page.page_index,
        "raw-layout-model-v2",
        objects,
        tuple(span.span_id for span in page.text.spans if span.span_id not in assigned),
    )


def test_source_occurrence_rounding_expands_roi_without_overwriting_model_geometry() -> None:
    span = TextSpan(
        "title",
        "Attractive New Business Profile",
        (28.104, 32.27499999999998, 434.806, 62.379999999999995),
    )
    page = _page(spans=(span,))
    raw_object = _object(
        "title-object",
        ObjectKind.TEXT,
        (28.104, 32.275, 434.806, 62.38),
        (span.span_id,),
    )
    raw = _partition(page, (raw_object,))

    normalized = normalize_partition(page=page, partition=raw)

    assert raw.objects[0].bbox == (28.104, 32.275, 434.806, 62.38)
    assert normalized.objects[0].bbox == (
        28.104,
        32.27499999999998,
        434.806,
        62.38,
    )
    assert normalized.objects[0].model_bbox == raw_object.bbox
    assert normalized.objects[0].model_kind is ObjectKind.TEXT
    assert normalized.objects[0].verification is Verification.PENDING
    assert normalized.objects[0].normalization == ("source-span-outward-containment-v1",)
    assert normalized.schema_version == NORMALIZATION_VERSION
    assert normalized.producer == f"{NORMALIZATION_VERSION}:{raw.producer}"
    with pytest.raises(ValueError, match="raw partition"):
        normalize_partition(page=page, partition=normalized)


def test_reviewed_p18_chart_bounds_expand_model_roi_and_lock_first_donut() -> None:
    payload = cast(
        dict[str, object],
        json.loads(Path("src/enterprise_pdf_rag/resources/aia-first-20-regions.json").read_text()),
    )
    entries = [
        cast(dict[str, object], item)
        for item in cast(list[object], payload["candidates"])
        if cast(dict[str, object], item)["page_index"] == 17
    ]
    first = next(entry for entry in entries if entry["object_id"] == "aia-p018-distribution-mix-v1")
    right = next(entry for entry in entries if entry["object_id"] == "aia-p018-chart-03-v1")
    span_boxes: dict[str, tuple[float, float, float, float]] = {}
    for entry in entries:
        x0, y0, _x1, _y1 = cast(list[float], entry["bbox"])
        for span_id in cast(list[str], entry["source_span_ids"]):
            span_boxes.setdefault(span_id, (x0 + 10.0, y0 + 10.0, x0 + 11.0, y0 + 11.0))
    for span_id in cast(list[str], first["source_span_ids"]):
        span_boxes[span_id] = (100.0, 200.0, 101.0, 201.0)
    for span_id in cast(list[str], right["source_span_ids"]):
        span_boxes[span_id] = (700.0, 200.0, 701.0, 201.0)
    spans = tuple(TextSpan(span_id, span_id, bbox) for span_id, bbox in span_boxes.items())
    page = _page(
        source="df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e",
        page_index=17,
        spans=spans,
    )
    first_raw = _object(
        "model-first-donut",
        ObjectKind.CHART,
        (24.216, 167.87, 249.85, 334.808),
        tuple(cast(list[str], first["source_span_ids"])),
    )
    right_raw = _object(
        "model-right-bars",
        ObjectKind.CHART,
        (621.94, 161.814, 854.533, 371.266),
        tuple(cast(list[str], right["source_span_ids"])),
    )
    raw = _partition(page, (first_raw, right_raw))

    normalized = normalize_partition(page=page, partition=raw)
    first_result, right_result = normalized.objects

    assert first_result.object_id == first_raw.object_id
    assert first_result.bbox == (18.0, 155.0, 250.0, 338.0)
    assert first_result.model_bbox == first_raw.bbox
    assert first_result.extraction_region_id == "aia-p018-distribution-mix-v1"
    assert first_result.normalization == (
        "reviewed-chart-candidate-outward-union-v1:aia-p018-distribution-mix-v1",
        "locked-extraction-region-v1:aia-p018-distribution-mix-v1",
    )
    assert right_result.bbox == (537.0, 126.0, 873.0, 376.0)
    assert right_result.model_bbox == right_raw.bbox
    assert right_result.extraction_region_id is None
    assert right_result.normalization == (
        "reviewed-chart-candidate-outward-union-v1:aia-p018-chart-03-v1",
    )


def test_reviewed_p18_kpi_cards_become_pending_groups_with_raw_kind_retained() -> None:
    groups = (
        (
            "span-v1-a8e83f8aa033b8586f93e98e26153ad3748254acadbb2f6b08f1ad493541cca8",
            "span-v1-92d8a0676b3ce8822f7cad3e7f9f595dc710a5a08fc789212f3795ce5c39eaaa",
            "span-v1-defa3f179c468673b368c0f4272ab3ff328d099effa951f18f3655d9222a8b60",
        ),
        (
            "span-v1-b7ed07ac66b55fe370270840825769cf7f2922298b32ca00c3572d6e5d7d2104",
            "span-v1-8a7ef305383107235d2e718f9d43c2e71763e6cb6006070330baaf648aa06429",
            "span-v1-70882bf784e402fae504002e6b5b92166f8dc6d5f93fc827817633d2ff36caa9",
            "span-v1-4b3f60918bfc36bbac732b5a61561b290840d30dbf5f754cc4050b4912d94974",
        ),
        (
            "span-v1-2754a7166750f141338f9870bf8d610f6d1cfee7b5149d14b7903890cbf8e204",
            "span-v1-3441022dd588e50f69bdaf726b0600a44bfbb5982697ae25d0a869bbebcba12c",
            "span-v1-fdde496cd88dd4fad1f13775680247bc299f63201d970cde985d8d5036340349",
        ),
        (
            "span-v1-cc67ca47a5a09bb49bdb6f7e89e5d2d22b0892e9efc2a56baa8b00ebe14ce64b",
            "span-v1-f9af94f50605e58cd0dcd6a24a49f2c6f737b85bf976238be8364fe86e32bcf7",
            "span-v1-1fd52e10d7b628f0df51acf4cf52504b50de28dfcb2149be95615d01b48bef56",
        ),
    )
    spans = tuple(
        TextSpan(span_id, span_id, (100.0 + index, 410.0, 101.0 + index, 420.0))
        for index, span_id in enumerate(span for group in groups for span in group)
    )
    payload = cast(
        dict[str, object],
        json.loads(Path("src/enterprise_pdf_rag/resources/aia-first-20-regions.json").read_text()),
    )
    catalog_ids = tuple(
        span_id
        for item in cast(list[dict[str, object]], payload["candidates"])
        if item["page_index"] == 17
        for span_id in cast(list[str], item["source_span_ids"])
    )
    present = {span.span_id for span in spans}
    spans += tuple(
        TextSpan(span_id, span_id, (500.0, 200.0, 501.0, 201.0))
        for span_id in dict.fromkeys(catalog_ids)
        if span_id not in present
    )
    page = _page(
        source="df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e",
        page_index=17,
        spans=spans,
    )
    objects = tuple(
        _object(f"kpi-{index}", ObjectKind.TEXT, (40.0, 390.0, 900.0, 470.0), ids)
        for index, ids in enumerate(groups)
    )
    heading = _object("heading", ObjectKind.TEXT, (10.0, 10.0, 20.0, 20.0), ())
    raw = _partition(page, (*objects, heading))

    normalized = normalize_partition(page=page, partition=raw)

    assert tuple(item.kind for item in normalized.objects[:4]) == (ObjectKind.GROUP,) * 4
    assert all(
        item.model_kind is ObjectKind.TEXT
        and item.verification is Verification.PENDING
        and item.normalization
        == ("root-source-review-kind-correction-v1:Text->Group:p018-kpi-card",)
        for item in normalized.objects[:4]
    )
    assert normalized.objects[-1].kind is ObjectKind.TEXT
    assert normalized.objects[-1].model_kind is ObjectKind.TEXT
    assert raw.objects[0].kind is ObjectKind.TEXT


def test_reviewed_p20_sensitivity_panel_becomes_pending_group() -> None:
    payload = cast(
        dict[str, object],
        json.loads(Path("src/enterprise_pdf_rag/resources/aia-first-20-regions.json").read_text()),
    )
    reviewed = next(
        item
        for item in cast(list[dict[str, object]], payload["candidates"])
        if item["object_id"] == "aia-p020-sensitivity-scenarios-v1"
    )
    span_ids = tuple(cast(list[str], reviewed["source_span_ids"]))
    page_span_ids = tuple(
        dict.fromkeys(
            span_id
            for item in cast(list[dict[str, object]], payload["candidates"])
            if item["page_index"] == 19
            for field in ("source_span_ids", "page_context_span_ids")
            for span_id in cast(list[str], item[field])
        )
    )
    spans = tuple(
        TextSpan(span_id, span_id, (700.0, 120.0 + i, 710.0, 121.0 + i))
        for i, span_id in enumerate(page_span_ids)
    )
    page = _page(
        source="df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e",
        page_index=19,
        spans=spans,
    )
    raw_object = _object(
        "model-sensitivity-chart",
        ObjectKind.CHART,
        (675.0, 104.426, 895.0, 446.0),
        span_ids,
    )

    normalized = normalize_partition(page=page, partition=_partition(page, (raw_object,)))

    result = normalized.objects[0]
    assert result.kind is ObjectKind.GROUP
    assert result.bbox == (654.0, 104.0, 934.0, 466.0)
    assert result.model_kind is ObjectKind.CHART
    assert result.verification is Verification.PENDING
    assert result.normalization == (
        "root-source-review-kind-correction-v1:Chart->Group:p020-sensitivity",
        "reviewed-group-candidate-outward-union-v1:aia-p020-sensitivity-scenarios-v1",
        "reviewed-alternate-kind-candidate-v1:Table",
        "native-table-unavailable-does-not-disprove-table-v1",
    )


def test_reviewed_p8_check_statements_become_an_unordered_three_item_list() -> None:
    items = (
        (
            "span-v1-670aab27b242147ed7346aefc1a5ff931ebe2ef1bde26166b1fb2a8a6be9fbd9",
            "span-v1-872d7fc60dbaf76855955e6e1bd8daed3c614d602479c87bd1560b13ca3f00e4",
            "span-v1-2e376bc9aa87283de28f174043c00cec8f159e33256611aba9058baa0f93f385",
            "span-v1-01d69b040f36a3226aff685a0e5a2c5f124d0cf62377113bc31f3169340e9325",
        ),
        (
            "span-v1-773cb7e8c2ff935d992b7d44250daba840dc35d4d3567d73ecd6910e591e80a1",
            "span-v1-b054d9fc14677760553e66b6086b0b444d536568e246beb04e7144d9326c2033",
            "span-v1-ec204ac3dc32febca7a6cf6bff330161f7525d7e7cb1323c98af19136de9e1b3",
        ),
        (
            "span-v1-0bacd8a1959e2a1a15dd48111ba5b866ccf5e1c42957fc0b3652a3bb4a423867",
            "span-v1-2249c668a5a9e1320e5d527b65948667a6688a80d2ced81a016e2160e2e155d0",
            "span-v1-58bcb566d60189ad061c04e074540edda64c4d34a2f73a3f6e2abaeb21327641",
            "span-v1-df48fc6134dfeedeec0fac59693f8f61f4770774b84401a6db561156633917db",
        ),
    )
    spans = tuple(
        TextSpan(span_id, span_id, (550.0, 150.0 + i, 700.0, 151.0 + i))
        for i, span_id in enumerate(span for item in items for span in item)
    )
    page = _page(
        source="df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e",
        page_index=7,
        spans=spans,
    )
    raw_object = _object(
        "model-check-statements",
        ObjectKind.TEXT,
        (489.0, 125.0, 944.0, 407.0),
        tuple(span for item in items for span in item),
    )

    result = normalize_partition(page=page, partition=_partition(page, (raw_object,))).objects[0]

    assert result.kind is ObjectKind.LIST
    assert result.model_kind is ObjectKind.TEXT
    assert result.list_item_span_ids == items
    assert result.list_ordered is False
    assert result.verification is Verification.PENDING
    assert result.normalization == (
        "root-source-review-kind-correction-v1:Text->List:p008-check-statements",
    )


def test_reviewed_p5_rasters_and_connector_become_pending_visual_children() -> None:
    span_ids = (
        "span-v1-4c9ad806ee415c6ca7fed1ccea4191c9054e480b424b269a18dc4ab85081cf64",
        "span-v1-e53e852da6333c81c1813d5ccd949df5eacdec6e711e7850ff932bcb59dc0cd3",
        "span-v1-eb269616174341e4676e16a32057a8f459306c48d9d5f22d453d0a289e208a33",
        "span-v1-48e1dd342166f51388abaf972952208a06595537f68df6cdb49c89405d1737c0",
        "span-v1-94d11bd0497046ef5ee50fe4aeae57ca5b6ccd945ac518fc8f242840f6b59cf6",
        "span-v1-d2f6001ce80ade15f0edd74070d825da09fc03a991f065c583f7e0a4627102fd",
        "span-v1-50e5b080f1e0e95bf7dad7208a625f29a9fa53e15cb00c0ba081819bbe890d72",
    )
    spans = tuple(
        TextSpan(span_id, span_id, (352.0, 320.0 + i, 591.805, 321.0 + i))
        for i, span_id in enumerate(span_ids)
    )
    page = _page(
        source="df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e",
        page_index=4,
        spans=spans,
    )
    parent = replace(
        _object("middle-panel", ObjectKind.GROUP, (336.0, 99.0, 618.0, 469.0), ()),
        child_object_ids=("technology-text",),
    )
    text = replace(
        _object(
            "technology-text",
            ObjectKind.TEXT,
            (352.0, 320.0, 493.0, 379.0),
            span_ids,
        ),
        parent_id=parent.object_id,
    )
    raw = _partition(page, (parent, text))

    normalized = normalize_partition(page=page, partition=raw)

    objects = {item.object_id: item for item in normalized.objects}
    diagram = objects["aia-p005-technology-flow-v1"]
    first = objects["aia-p005-aia-plus-raster-v1"]
    second = objects["aia-p005-aia-one-raster-v1"]
    assert len(normalized.objects) == 5
    assert objects[parent.object_id].child_object_ids == (
        text.object_id,
        diagram.object_id,
    )
    assert diagram.kind is ObjectKind.DIAGRAM
    assert diagram.bbox == (352.0, 306.71, 591.805, 379.0)
    assert diagram.parent_id == parent.object_id
    assert diagram.child_object_ids == (first.object_id, second.object_id)
    assert first.kind is second.kind is ObjectKind.IMAGE
    assert first.bbox == (442.5, 306.71, 497.763, 362.798)
    assert second.bbox == (534.64, 307.049, 584.958, 357.367)
    assert first.parent_id == second.parent_id == diagram.object_id
    assert diagram.source_span_ids == first.source_span_ids == second.source_span_ids == ()
    assert all(
        item.verification is Verification.PENDING
        and item.model_bbox is None
        and item.model_kind is None
        and item.extraction_region_id == item.object_id
        for item in (diagram, first, second)
    )
    assert raw.objects == (parent, text)


def test_reviewed_omitted_spans_are_assigned_to_exact_p4_and_p5_source_groups() -> None:
    cases = (
        (
            3,
            (
                "span-v1-581a847f5f17f34bc0d08112b0ac608eb6f826b3163143a553db4afd4b439331",
                "span-v1-4abffb328d5217d90ccb966044b397495a3863bb257b5b819270f8f6c34c5ad6",
            ),
            ("span-v1-3ed104fe6d54ffa9b5736c0bc7afa3be12448131a52e2916bd79a3265cea3dcd",),
            "root-source-review-omitted-span-assignment-v1:p004-returns-kpi",
        ),
        (
            4,
            (
                "span-v1-72ee7e379b1e368b831b591adbd57f8cf0d115c9b44732fa958da11a1cd51c34",
                "span-v1-cc1e4da899751a16aee7de3af93794347baea6303386613e73163c16ea653116",
                "span-v1-65e7c95e74d65b6468833f92083a0c0087ead8ab9ff258c6b47c2f6bd9bbe6af",
                "span-v1-fec9e9a11f3cb9d4638b66297cb0af7ece5f95978d7da5e20d966c4ce58cdcb1",
                "span-v1-a007fc7999343c2cc86c66532d677ec5352948500ba6ada3b9ed2d7c760007a0",
            ),
            (
                "span-v1-57febf76a79d4885f19aa85199e9c4551dc3d62bf40b9bc85980d711f15f444b",
                "span-v1-7d2893cc8a32d5485e6662f1c7c293574a80d906205138497eddcfdbc74a7151",
            ),
            "root-source-review-omitted-span-assignment-v1:p005-franchise-kpi",
        ),
    )
    for page_index, assigned, omitted, rule in cases:
        spans = tuple(
            TextSpan(span_id, span_id, (100.0 + i * 10, 100.0, 105.0 + i * 10, 110.0))
            for i, span_id in enumerate((*assigned, *omitted))
        )
        page = _page(
            source="df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e",
            page_index=page_index,
            spans=spans,
        )
        item = _object(
            f"page-{page_index}-reviewed-group",
            ObjectKind.TEXT,
            (90.0, 90.0, 160.0, 120.0),
            assigned,
        )
        raw = _partition(page, (item,))
        assert raw.unassigned_span_ids == omitted

        normalized = normalize_partition(page=page, partition=raw)

        assert normalized.objects[0].source_span_ids == (*assigned, *omitted)
        assert normalized.unassigned_span_ids == ()
        assert normalized.objects[0].normalization[0] == rule
        assert normalized.objects[0].bbox[2] >= max(span.bbox[2] for span in spans)
        assert raw.objects[0].source_span_ids == assigned
