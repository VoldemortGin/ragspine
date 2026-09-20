"""Deterministic source and review corrections over immutable model layout."""

from dataclasses import replace

from enterprise_pdf_rag.adapters.aia_candidates import SOURCE_SHA256, candidates_for
from enterprise_pdf_rag.figures.models import Confidence, Verification
from enterprise_pdf_rag.processing.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    PagePartition,
)
from enterprise_pdf_rag.processing.service import validate_partition

NORMALIZATION_VERSION = "layout-normalization-v2"
_P18_LOCKED_REGION = "aia-p018-distribution-mix-v1"
_P20_SENSITIVITY_REGION = "aia-p020-sensitivity-scenarios-v1"
_P05_TECHNOLOGY_SPANS = frozenset(
    {
        "span-v1-4c9ad806ee415c6ca7fed1ccea4191c9054e480b424b269a18dc4ab85081cf64",
        "span-v1-e53e852da6333c81c1813d5ccd949df5eacdec6e711e7850ff932bcb59dc0cd3",
        "span-v1-eb269616174341e4676e16a32057a8f459306c48d9d5f22d453d0a289e208a33",
        "span-v1-48e1dd342166f51388abaf972952208a06595537f68df6cdb49c89405d1737c0",
        "span-v1-94d11bd0497046ef5ee50fe4aeae57ca5b6ccd945ac518fc8f242840f6b59cf6",
        "span-v1-d2f6001ce80ade15f0edd74070d825da09fc03a991f065c583f7e0a4627102fd",
        "span-v1-50e5b080f1e0e95bf7dad7208a625f29a9fa53e15cb00c0ba081819bbe890d72",
    }
)
_P05_DIAGRAM_ID = "aia-p005-technology-flow-v1"
_P05_IMAGE_IDS = (
    "aia-p005-aia-plus-raster-v1",
    "aia-p005-aia-one-raster-v1",
)
_P08_CHECK_ITEMS = (
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
_P18_KPI_SPANS = frozenset(
    {
        frozenset(
            {
                "span-v1-a8e83f8aa033b8586f93e98e26153ad3748254acadbb2f6b08f1ad493541cca8",
                "span-v1-92d8a0676b3ce8822f7cad3e7f9f595dc710a5a08fc789212f3795ce5c39eaaa",
                "span-v1-defa3f179c468673b368c0f4272ab3ff328d099effa951f18f3655d9222a8b60",
            }
        ),
        frozenset(
            {
                "span-v1-b7ed07ac66b55fe370270840825769cf7f2922298b32ca00c3572d6e5d7d2104",
                "span-v1-8a7ef305383107235d2e718f9d43c2e71763e6cb6006070330baaf648aa06429",
                "span-v1-70882bf784e402fae504002e6b5b92166f8dc6d5f93fc827817633d2ff36caa9",
                "span-v1-4b3f60918bfc36bbac732b5a61561b290840d30dbf5f754cc4050b4912d94974",
            }
        ),
        frozenset(
            {
                "span-v1-2754a7166750f141338f9870bf8d610f6d1cfee7b5149d14b7903890cbf8e204",
                "span-v1-3441022dd588e50f69bdaf726b0600a44bfbb5982697ae25d0a869bbebcba12c",
                "span-v1-fdde496cd88dd4fad1f13775680247bc299f63201d970cde985d8d5036340349",
            }
        ),
        frozenset(
            {
                "span-v1-cc67ca47a5a09bb49bdb6f7e89e5d2d22b0892e9efc2a56baa8b00ebe14ce64b",
                "span-v1-f9af94f50605e58cd0dcd6a24a49f2c6f737b85bf976238be8364fe86e32bcf7",
                "span-v1-1fd52e10d7b628f0df51acf4cf52504b50de28dfcb2149be95615d01b48bef56",
            }
        ),
    }
)
_REVIEWED_OMISSIONS: dict[tuple[int, frozenset[str]], tuple[tuple[str, ...], str]] = {
    (
        3,
        frozenset(
            {
                "span-v1-581a847f5f17f34bc0d08112b0ac608eb6f826b3163143a553db4afd4b439331",
                "span-v1-4abffb328d5217d90ccb966044b397495a3863bb257b5b819270f8f6c34c5ad6",
            }
        ),
    ): (
        ("span-v1-3ed104fe6d54ffa9b5736c0bc7afa3be12448131a52e2916bd79a3265cea3dcd",),
        "root-source-review-omitted-span-assignment-v1:p004-returns-kpi",
    ),
    (
        4,
        frozenset(
            {
                "span-v1-72ee7e379b1e368b831b591adbd57f8cf0d115c9b44732fa958da11a1cd51c34",
                "span-v1-cc1e4da899751a16aee7de3af93794347baea6303386613e73163c16ea653116",
                "span-v1-65e7c95e74d65b6468833f92083a0c0087ead8ab9ff258c6b47c2f6bd9bbe6af",
                "span-v1-fec9e9a11f3cb9d4638b66297cb0af7ece5f95978d7da5e20d966c4ce58cdcb1",
                "span-v1-a007fc7999343c2cc86c66532d677ec5352948500ba6ada3b9ed2d7c760007a0",
            }
        ),
    ): (
        (
            "span-v1-57febf76a79d4885f19aa85199e9c4551dc3d62bf40b9bc85980d711f15f444b",
            "span-v1-7d2893cc8a32d5485e6662f1c7c293574a80d906205138497eddcfdbc74a7151",
        ),
        "root-source-review-omitted-span-assignment-v1:p005-franchise-kpi",
    ),
}


def _source_bbox(
    page: PageInput, item: LayoutObject
) -> tuple[float, float, float, float]:
    observed = {span.span_id: span for span in page.text.spans}
    boxes = tuple(observed[span_id].bbox for span_id in item.source_span_ids)
    if not boxes:
        return item.bbox
    return (
        min(item.bbox[0], *(box[0] for box in boxes)),
        min(item.bbox[1], *(box[1] for box in boxes)),
        max(item.bbox[2], *(box[2] for box in boxes)),
        max(item.bbox[3], *(box[3] for box in boxes)),
    )


def _union(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _chart_candidate(
    item: LayoutObject, candidates: tuple[LayoutObject, ...]
) -> LayoutObject | None:
    if item.kind is not ObjectKind.CHART or not item.source_span_ids:
        return None
    actual = set(item.source_span_ids)
    matches = []
    for candidate in candidates:
        if candidate.kind is not ObjectKind.CHART or not candidate.source_span_ids:
            continue
        reviewed = set(candidate.source_span_ids)
        overlap = len(actual & reviewed)
        if overlap == min(len(actual), len(reviewed)):
            matches.append((overlap, candidate))
    if not matches:
        return None
    largest = max(overlap for overlap, _ in matches)
    selected = tuple(candidate for overlap, candidate in matches if overlap == largest)
    return selected[0] if len(selected) == 1 else None


def _p20_sensitivity_candidate(
    item: LayoutObject, candidates: tuple[LayoutObject, ...]
) -> LayoutObject | None:
    if item.kind is not ObjectKind.CHART:
        return None
    return next(
        (
            candidate
            for candidate in candidates
            if candidate.object_id == _P20_SENSITIVITY_REGION
            and candidate.kind is ObjectKind.GROUP
            and frozenset(candidate.source_span_ids) == frozenset(item.source_span_ids)
        ),
        None,
    )


def normalize_partition(*, page: PageInput, partition: PagePartition) -> PagePartition:
    """Return a new partition while retaining every model field for review."""

    validate_partition(page, partition)
    if partition.schema_version == NORMALIZATION_VERSION or any(
        item.model_bbox is not None or item.model_kind is not None
        for item in partition.objects
    ):
        raise ValueError("Layout normalization requires the immutable raw partition")
    objects = []
    candidates = (
        candidates_for(page)
        if page.source_sha256 == SOURCE_SHA256
        and any(item.kind is ObjectKind.CHART for item in partition.objects)
        else ()
    )
    consumed_unassigned: set[str] = set()
    owned = {span_id for item in partition.objects for span_id in item.source_span_ids}
    p05_technology = next(
        (
            item
            for item in partition.objects
            if page.source_sha256 == SOURCE_SHA256
            and page.page_index == 4
            and item.kind is ObjectKind.TEXT
            and item.parent_id is not None
            and frozenset(item.source_span_ids) == _P05_TECHNOLOGY_SPANS
        ),
        None,
    )
    p05_parent_id = p05_technology.parent_id if p05_technology is not None else None
    if p05_parent_id is not None and not any(
        item.object_id == p05_parent_id and item.kind is ObjectKind.GROUP
        for item in partition.objects
    ):
        p05_parent_id = None
    for item in partition.objects:
        rules: list[str] = []
        source_span_ids = item.source_span_ids
        omission = _REVIEWED_OMISSIONS.get(
            (page.page_index, frozenset(item.source_span_ids))
        )
        if (
            page.source_sha256 == SOURCE_SHA256
            and omission is not None
            and set(omission[0]).issubset(partition.unassigned_span_ids)
            and set(omission[0]).isdisjoint(owned)
        ):
            source_span_ids = (*source_span_ids, *omission[0])
            consumed_unassigned.update(omission[0])
            rules.append(omission[1])
        corrected = replace(item, source_span_ids=source_span_ids)
        bbox = _source_bbox(page, corrected)
        kind = item.kind
        list_item_span_ids = item.list_item_span_ids
        list_ordered = item.list_ordered
        child_object_ids = item.child_object_ids
        if item.object_id == p05_parent_id:
            child_object_ids = (*child_object_ids, _P05_DIAGRAM_ID)
            rules.append("root-source-review-child-visual-v1:p005-technology-flow")
        if bbox != item.bbox:
            rules.append("source-span-outward-containment-v1")
        candidate = _chart_candidate(corrected, candidates)
        p20_sensitivity = _p20_sensitivity_candidate(corrected, candidates)
        extraction_region_id = item.extraction_region_id
        if candidate is not None:
            reviewed_bbox = _union(bbox, candidate.bbox)
            if reviewed_bbox != bbox:
                bbox = reviewed_bbox
                rules.append(
                    "reviewed-chart-candidate-outward-union-v1:" + candidate.object_id
                )
            if candidate.object_id == _P18_LOCKED_REGION and bbox == candidate.bbox:
                extraction_region_id = candidate.object_id
                rules.append("locked-extraction-region-v1:" + candidate.object_id)
        if p20_sensitivity is not None:
            bbox = _union(bbox, p20_sensitivity.bbox)
            kind = ObjectKind.GROUP
            rules.extend(
                (
                    "root-source-review-kind-correction-v1:Chart->Group:p020-sensitivity",
                    "reviewed-group-candidate-outward-union-v1:"
                    + p20_sensitivity.object_id,
                    "reviewed-alternate-kind-candidate-v1:Table",
                    "native-table-unavailable-does-not-disprove-table-v1",
                )
            )
        if (
            page.source_sha256 == SOURCE_SHA256
            and page.page_index == 17
            and item.kind is ObjectKind.TEXT
            and frozenset(item.source_span_ids) in _P18_KPI_SPANS
        ):
            kind = ObjectKind.GROUP
            rules.append(
                "root-source-review-kind-correction-v1:Text->Group:p018-kpi-card"
            )
        if (
            page.source_sha256 == SOURCE_SHA256
            and page.page_index == 7
            and item.kind is ObjectKind.TEXT
            and frozenset(item.source_span_ids)
            == frozenset(span for group in _P08_CHECK_ITEMS for span in group)
        ):
            kind = ObjectKind.LIST
            list_item_span_ids = _P08_CHECK_ITEMS
            list_ordered = False
            rules.append(
                "root-source-review-kind-correction-v1:Text->List:p008-check-statements"
            )
        objects.append(
            replace(
                corrected,
                kind=kind,
                bbox=bbox,
                list_item_span_ids=list_item_span_ids,
                list_ordered=list_ordered,
                child_object_ids=child_object_ids,
                model_bbox=item.bbox,
                model_kind=item.kind,
                normalization=tuple(rules),
                extraction_region_id=extraction_region_id,
            )
        )
    if p05_parent_id is not None:
        reviewed = Confidence(
            None, "root-source-render-review; uncalibrated manual candidate"
        )
        objects.extend(
            (
                LayoutObject(
                    object_id=_P05_DIAGRAM_ID,
                    kind=ObjectKind.DIAGRAM,
                    bbox=(352.0, 306.71, 591.805, 379.0),
                    source_span_ids=(),
                    interpretation=(
                        "Observed native-SVG region with two embedded raster occurrences "
                        "and a connector; node and edge semantics remain unqualified."
                    ),
                    confidence=reviewed,
                    verification=Verification.PENDING,
                    parent_id=p05_parent_id,
                    child_object_ids=_P05_IMAGE_IDS,
                    normalization=(
                        "root-source-review-added-visual-object-v1:p005-technology-flow",
                    ),
                    extraction_region_id=_P05_DIAGRAM_ID,
                ),
                LayoutObject(
                    object_id=_P05_IMAGE_IDS[0],
                    kind=ObjectKind.IMAGE,
                    bbox=(442.5, 306.71, 497.763, 362.798),
                    source_span_ids=(),
                    interpretation=(
                        "Observed embedded raster occurrence; content semantics remain "
                        "unqualified."
                    ),
                    confidence=reviewed,
                    verification=Verification.PENDING,
                    parent_id=_P05_DIAGRAM_ID,
                    normalization=(
                        "root-source-review-added-native-raster-v1:p005-image-1",
                    ),
                    extraction_region_id=_P05_IMAGE_IDS[0],
                ),
                LayoutObject(
                    object_id=_P05_IMAGE_IDS[1],
                    kind=ObjectKind.IMAGE,
                    bbox=(534.64, 307.049, 584.958, 357.367),
                    source_span_ids=(),
                    interpretation=(
                        "Observed embedded raster occurrence; content semantics remain "
                        "unqualified."
                    ),
                    confidence=reviewed,
                    verification=Verification.PENDING,
                    parent_id=_P05_DIAGRAM_ID,
                    normalization=(
                        "root-source-review-added-native-raster-v1:p005-image-2",
                    ),
                    extraction_region_id=_P05_IMAGE_IDS[1],
                ),
            )
        )
    normalized = replace(
        partition,
        schema_version=NORMALIZATION_VERSION,
        producer=f"{NORMALIZATION_VERSION}:{partition.producer}",
        objects=tuple(objects),
        unassigned_span_ids=tuple(
            span_id
            for span_id in partition.unassigned_span_ids
            if span_id not in consumed_unassigned
        ),
        diagnostics=(
            *partition.diagnostics,
            "Deterministic normalization preserves model geometry and kind; corrections remain pending.",
        ),
    )
    validate_partition(page, normalized)
    return normalized
