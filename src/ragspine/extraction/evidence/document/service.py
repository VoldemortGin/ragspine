"""The source identity is checked before SDK or persistence calls."""

import json
from dataclasses import asdict
from hashlib import sha256
from math import isfinite

from ragspine.extraction.evidence.document.models import (
    DocumentManifest,
    DocumentSpec,
    PageRecord,
    RegionRecord,
    TextSidecar,
)
from ragspine.extraction.evidence.document.ports import AssetStore, SourceExtractor


def verify_source(pdf: bytes, spec: DocumentSpec) -> None:
    if sha256(pdf).hexdigest() != spec.sha256:
        raise ValueError("The selected source SHA-256 does not match; no other PDF is accepted")


def ingest_document(
    pdf: bytes, *, spec: DocumentSpec, extractor: SourceExtractor, store: AssetStore
) -> str:
    verify_source(pdf, spec)
    source = store.put(pdf, media_type="application/pdf")
    extracted = extractor.extract_document(pdf)
    if len(extracted.pages) != spec.page_count or tuple(
        page.page_index for page in extracted.pages
    ) != tuple(range(spec.page_count)):
        raise ValueError("Incomplete or ambiguous page coverage; manifest was not published")
    for page in extracted.pages:
        if not all(isfinite(value) and value > 0 for value in (page.width, page.height)):
            raise ValueError(f"Invalid source page geometry at page_index={page.page_index}")
        for span in page.text_spans:
            if not all(
                isfinite(value) for value in (*span.bbox, *span.origin, span.size, *span.direction)
            ):
                raise ValueError(f"Invalid source span geometry at page_index={page.page_index}")
    if not 0 <= spec.focus_page_index < len(extracted.pages) or not all(
        isfinite(value) for value in spec.focus_bbox
    ):
        raise ValueError("Invalid focus region geometry")
    region = extractor.extract_region(pdf, page_index=spec.focus_page_index, bbox=spec.focus_bbox)
    focus = extracted.pages[spec.focus_page_index]
    if (
        region.page_index != spec.focus_page_index
        or region.bbox != spec.focus_bbox
        or region.native_svg != focus.native_svg
        or (region.width, region.height, region.rotation)
        != (focus.width, focus.height, focus.rotation)
    ):
        raise ValueError("Region is not bound to the same source page")
    pages: list[PageRecord] = []
    for page in extracted.pages:
        if page.width <= 0 or page.height <= 0:
            raise ValueError("Invalid source page geometry")
        text = TextSidecar("text-spans-v1", spec.sha256, page.page_index, page.text_spans)
        pages.append(
            PageRecord(
                page.page_index,
                page.width,
                page.height,
                page.rotation,
                store.put(page.native_svg.encode(), media_type="image/svg+xml"),
                store.put(
                    json.dumps(asdict(text), ensure_ascii=False, sort_keys=True).encode(),
                    media_type="application/json",
                ),
                len(page.text_spans),
                page.warnings,
                page.text_layer,
            )
        )
    region_text = TextSidecar("text-spans-v1", spec.sha256, region.page_index, region.text_spans)
    record = RegionRecord(
        region.page_index,
        region.bbox,
        pages[region.page_index].svg,
        store.put(region.cropped_svg.encode(), media_type="image/svg+xml"),
        store.put(
            json.dumps(asdict(region_text), ensure_ascii=False, sort_keys=True).encode(),
            media_type="application/json",
        ),
        region.warnings + spec.diagnostics,
    )
    manifest = DocumentManifest(
        "source-ingestion-v1",
        spec.filename,
        source,
        extracted.producer,
        tuple(pages),
        record,
    )
    return store.publish(manifest)
