"""Persistent bar projections requalify raw branches and separate footer paint."""

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from tests.adapters.bar_source_fixture import BarSource, bar_source

from enterprise_pdf_rag.adapters.bar_publication import (
    build_displayed_bar_candidate,
    parse_displayed_bar_receipt,
    resolve_displayed_bar_member,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.figure_reasoning import FigureModelView, prepare_figure
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.source_paint_bar import SourcePaintBarProof
from enterprise_pdf_rag.documents.models import DocumentSpec
from enterprise_pdf_rag.documents.service import ingest_document
from enterprise_pdf_rag.figures.models import ChartIR, Confidence, TextDescription
from enterprise_pdf_rag.processing.models import (
    LayoutObject,
    ObjectKind,
    ObjectProcessingRecord,
    ProcessingScope,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.retrieval import RetrievalMember


def publication_input(
    tmp_path: Path,
) -> tuple[
    LocalDocumentStore,
    LocalDocumentStore,
    ProcessingScope,
    LayoutObject,
    ObjectProcessingRecord,
    BarSource,
]:
    source = bar_source()
    sources = LocalDocumentStore(tmp_path / "sources")
    assets = LocalDocumentStore(tmp_path / "assets")
    spec = DocumentSpec(
        "authored-bar.pdf",
        source.page.source_sha256,
        1,
        0,
        source.prepared.svg.source.bbox,
    )
    manifest = ingest_document(
        source.pdf, spec=spec, extractor=PdfspineDocumentAdapter(), store=sources
    )
    page = replace(source.page, source_manifest_id=manifest)
    native = sources.get(sources.load(manifest).manifest.pages[0].svg)
    prepared = prepare_figure(
        page=page,
        native_svg=native,
        bbox=source.prepared.svg.source.bbox,
        region_id="authored-bar",
    )
    assert prepared.svg == source.prepared.svg
    source = replace(source, page=page, prepared=prepared)
    payloads = (
        ("ir", TypeAdapter(ChartIR).dump_json(source.chart)),
        (
            "description",
            TypeAdapter(TextDescription).dump_json(source.previous_description),
        ),
        ("description_raw", source.raw_description),
        ("model_view", TypeAdapter(FigureModelView).dump_json(prepared.view)),
        ("svg", prepared.svg.svg.encode()),
    )
    stages = tuple(
        StageOutcome(
            name,
            "source",
            StageState.SUCCEEDED,
            "authored",
            assets.put(
                payload,
                media_type="image/svg+xml" if name == "svg" else "application/json",
            ),
        )
        for name, payload in payloads
    )
    item = LayoutObject(
        "authored-object",
        ObjectKind.CHART,
        prepared.svg.source.bbox,
        tuple(
            dict.fromkeys(
                element.source_span_id
                for element in prepared.svg.elements
                if element.source_span_id is not None
            )
        ),
        "source chart",
        Confidence(None, "authored"),
        extraction_region_id="authored-bar",
    )
    return (
        sources,
        assets,
        ProcessingScope(manifest, page.source_sha256, 1, (0,)),
        item,
        ObjectProcessingRecord(item.object_id, item.kind, stages),
        source,
    )


def test_candidate_resolves_with_complete_source_and_independent_eight_asset_lineage(
    tmp_path: Path,
) -> None:
    sources, assets, scope, item, record, _ = publication_input(tmp_path)
    old = sources.load_current().manifest_id
    candidate = build_displayed_bar_candidate(
        sources, assets, scope, page_index=0, item=item, record=record
    )
    receipt = parse_displayed_bar_receipt(assets.get(candidate.qualification))
    assert candidate.numeric_claim_count == 2
    assert len(candidate.lineage_refs) == 8
    member = RetrievalMember(
        item.object_id,
        item.kind,
        0,
        candidate.ir,
        candidate.description,
        candidate.qualification,
        assets.put(b"offline-vector", media_type="application/json"),
        candidate.source_svg,
        "offline-only",
        2,
        candidate.lineage_refs,
    )
    resolved = resolve_displayed_bar_member(sources, assets, scope, member)
    assert resolved.chart.period is None
    assert len(resolved.chart.points) == 2
    assert tuple(claim.text for claim in resolved.description.claims) == (
        "Expense Ratio for 1H21: 15 %.",
        "Expense Ratio for 1H23: 6 %.",
    )
    assert (
        resolved.page_context[0].source_text_sha256
        == sources.load(scope.source_manifest_id).manifest.pages[0].text.sha256
    )
    assert receipt.included_claim_paths == ("claims.0", "claims.2")
    assert sources.load_current().manifest_id == old
    assert not (assets.root / "current-processing").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "source_paint",
        "footer_paint",
        "normalization",
        "projection",
        "missing_lineage",
        "wrong_page",
    ],
)
def test_reidentified_tampering_still_fails_source_requalification(
    tmp_path: Path, mutation: str
) -> None:
    sources, assets, scope, item, record, _ = publication_input(tmp_path)
    candidate = build_displayed_bar_candidate(
        sources, assets, scope, page_index=0, item=item, record=record
    )
    receipt = parse_displayed_bar_receipt(assets.get(candidate.qualification))
    member = RetrievalMember(
        item.object_id,
        item.kind,
        0,
        candidate.ir,
        candidate.description,
        candidate.qualification,
        assets.put(b"offline-vector", media_type="application/json"),
        candidate.source_svg,
        "offline-only",
        2,
        candidate.lineage_refs,
    )
    if mutation in {"source_paint", "footer_paint"}:
        field = (
            "source_paint_proof" if mutation == "source_paint" else "page_context_proof"
        )
        old = getattr(receipt, field)
        proof = TypeAdapter(SourcePaintBarProof).validate_json(
            assets.get(old), strict=True
        )
        changed = replace(
            proof, glyphs=(replace(proof.glyphs[0], character="X"), *proof.glyphs[1:])
        )
        new = assets.put(
            TypeAdapter(SourcePaintBarProof).dump_json(changed),
            media_type="application/json",
        )
        receipt = receipt.model_copy(update={field: new})
        member = replace(
            member,
            lineage_refs=tuple(
                new if ref == old else ref for ref in member.lineage_refs
            ),
        )
    elif mutation == "normalization":
        receipt = receipt.model_copy(
            update={
                "description_normalization_citation": replace(
                    receipt.description_normalization_citation,
                    raw_response_sha256="f" * 64,
                )
            }
        )
    elif mutation == "projection":
        description = TypeAdapter(TextDescription).validate_json(
            assets.get(member.description), strict=True
        )
        changed_description = replace(
            description,
            claims=(
                replace(
                    description.claims[0],
                    text=description.claims[0].text + " It improved.",
                ),
                *description.claims[1:],
            ),
        )
        new = assets.put(
            TypeAdapter(TextDescription).dump_json(changed_description),
            media_type="application/json",
        )
        receipt = receipt.model_copy(update={"description": new})
        member = replace(member, description=new)
    elif mutation == "missing_lineage":
        member = replace(member, lineage_refs=member.lineage_refs[:-1])
    else:
        member = replace(member, page_index=1)
    member = replace(
        member,
        qualification=assets.put(
            receipt.model_dump_json().encode(), media_type="application/json"
        ),
    )
    with pytest.raises(
        ValueError, match=r"revalidation_mismatch|lineage_required|source_mismatch"
    ):
        resolve_displayed_bar_member(sources, assets, scope, member)
