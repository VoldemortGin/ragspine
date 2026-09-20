"""Real source projections persist independently reviewable typed artifacts."""

from pathlib import Path

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.object_processing import ProcessingObjectAdapter
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.documents.models import TextSidecar, TextSpan
from enterprise_pdf_rag.figures.models import Confidence
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput
from enterprise_pdf_rag.processing.typed_ir import ObjectDescription, TextIR


def test_source_object_persists_actual_ir_description_and_scoped_literal_receipt(
    tmp_path: Path,
) -> None:
    sources = LocalDocumentStore(tmp_path / "source")
    svg = sources.put(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"><path d="M0 0L10 10"/></svg>',
        media_type="image/svg+xml",
    )
    page = PageInput(
        "a" * 64,
        "b" * 64,
        0,
        100.0,
        100.0,
        svg,
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            0,
            (TextSpan("label", "Dividend policy", (10.0, 10.0, 80.0, 20.0)),),
        ),
    )
    item = LayoutObject(
        "object",
        ObjectKind.TEXT,
        (0.0, 0.0, 100.0, 40.0),
        ("label",),
        "source text",
        Confidence(None, "test"),
    )
    outputs = ProcessingStore(tmp_path / "processed")
    processor = ProcessingObjectAdapter(sources, outputs)
    record = processor.process(page, item)
    stages = {stage.stage: stage for stage in record.stages}
    assert {"svg", "source_text", "ir", "description", "qualification"} <= set(stages)
    assert all(
        stage.state == "succeeded" and stage.artifact is not None for stage in stages.values()
    )
    ir_ref = stages["ir"].artifact
    description_ref = stages["description"].artifact
    receipt_ref = stages["qualification"].artifact
    assert ir_ref is not None and description_ref is not None and receipt_ref is not None
    ir = TypeAdapter(TextIR).validate_json(outputs.assets.get(ir_ref))
    description = TypeAdapter(ObjectDescription).validate_json(outputs.assets.get(description_ref))
    assert ir.fragments[0].source_span_id == "label"
    assert (
        description.text == "Dividend policy"
        and description.producer == "exact-source-transcription-v1"
    )
    assert b"literal-source-transcription-v1" in outputs.assets.get(receipt_ref)
    assert processor.process(page, item) == record


def test_container_group_retains_children_and_independent_source_region_description(
    tmp_path: Path,
) -> None:
    from enterprise_pdf_rag.figures.models import Verification
    from enterprise_pdf_rag.processing.typed_ir import GroupIR

    sources = LocalDocumentStore(tmp_path / "source")
    svg = sources.put(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"/>',
        media_type="image/svg+xml",
    )
    page = PageInput(
        "a" * 64,
        "b" * 64,
        0,
        100.0,
        100.0,
        svg,
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            0,
            (TextSpan("child-label", "72%", (10.0, 10.0, 30.0, 20.0)),),
        ),
    )
    group = LayoutObject(
        "group",
        ObjectKind.GROUP,
        (0.0, 0.0, 100.0, 40.0),
        (),
        "proposed card",
        Confidence(None, "layout"),
        child_object_ids=("child",),
    )
    outputs = ProcessingStore(tmp_path / "out")
    record = ProcessingObjectAdapter(sources, outputs).process(page, group)
    stages = {stage.stage: stage for stage in record.stages}
    ir_ref = stages["ir"].artifact
    desc_ref = stages["description"].artifact
    assert ir_ref is not None and desc_ref is not None
    ir = TypeAdapter(GroupIR).validate_json(outputs.assets.get(ir_ref))
    desc = TypeAdapter(ObjectDescription).validate_json(outputs.assets.get(desc_ref))
    assert ir.child_object_ids == ("child",) and ir.fragments == ()
    assert desc.source_span_ids == ("child-label",) and "72%" in desc.text
    assert desc.verification is Verification.PENDING
    assert stages["qualification"].state == "unavailable"
    sidecar_ref = stages["source_text"].artifact
    assert sidecar_ref is not None
    sidecar = TypeAdapter(TextSidecar).validate_json(outputs.assets.get(sidecar_ref))
    assert set(desc.source_span_ids) <= {span.span_id for span in sidecar.spans}


def test_container_region_tolerates_model_rendered_float_noise(tmp_path: Path) -> None:
    sources = LocalDocumentStore(tmp_path / "source")
    svg = sources.put(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"/>',
        media_type="image/svg+xml",
    )
    page = PageInput(
        "a" * 64,
        "b" * 64,
        0,
        100.0,
        100.0,
        svg,
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            0,
            (TextSpan("child-label", "72%", (10.0, 10.0, 30.0, 40.00000000000001)),),
        ),
    )
    group = LayoutObject(
        "group",
        ObjectKind.GROUP,
        (0.0, 0.0, 100.0, 40.0),
        (),
        "proposed card",
        Confidence(None, "layout"),
        child_object_ids=("child",),
    )
    record = ProcessingObjectAdapter(sources, ProcessingStore(tmp_path / "out")).process(
        page, group
    )
    stages = {stage.stage: stage for stage in record.stages}
    desc_ref = stages["description"].artifact
    assert desc_ref is not None
