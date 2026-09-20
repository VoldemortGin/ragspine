"""Description vectors resolve source-qualified IR inside one frozen snapshot."""

import json
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.object_processing import ProcessingObjectAdapter
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.documents.models import (
    DocumentManifest,
    PageRecord,
    RegionRecord,
    TextSidecar,
    TextSpan,
)
from enterprise_pdf_rag.figures.models import Confidence
from enterprise_pdf_rag.processing.models import (
    CanonicalPage,
    LayoutObject,
    ObjectKind,
    PageInput,
    PagePartition,
    PageProcessingRecord,
    ProcessingManifest,
    ProcessingScope,
    RetrievalPublication,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.retrieval import (
    IndexEntry,
    RetrievalEmbedding,
    RetrievalIndex,
    RetrievalPlan,
    require_financial_qualification,
    retrieval_dependencies,
)
from enterprise_pdf_rag.processing.service import canonical_page
from enterprise_pdf_rag.processing.typed_ir import (
    LiteralQualification,
    ObjectDescription,
    TextIR,
)


class RecordingEmbedding:
    fingerprint = "test-explicit-offline-vector-v1"

    def __init__(self) -> None:
        self.descriptions: list[str] = []

    def embed_description(self, text: str) -> tuple[float, ...]:
        self.descriptions.append(text)
        return (1.0, 0.0)

    def embed_query(self, text: str) -> tuple[float, ...]:
        assert text.strip()
        return (1.0, 0.0)


@pytest.mark.parametrize("tampering", ("producer", "crop", "anchor", "confidence"))
def test_persisted_description_search_hydrates_typed_ir_without_granting_financial_qa(
    tmp_path: Path,
    tampering: str,
) -> None:
    sources = LocalDocumentStore(tmp_path / "source")
    pdf = sources.put(b"unit-test source", media_type="application/pdf")
    svg = sources.put(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"><path d="M0 0L10 10"/></svg>',
        media_type="image/svg+xml",
    )
    sidecar = TextSidecar(
        "source-text-v1",
        pdf.sha256,
        0,
        (TextSpan("number", "Agency 72%", (10.0, 10.0, 70.0, 20.0)),),
    )
    text = sources.put(json.dumps(asdict(sidecar)).encode(), media_type="application/json")
    source_id = sources.publish(
        DocumentManifest(
            "source-ingestion-v1",
            "test.pdf",
            pdf,
            "test",
            (PageRecord(0, 100.0, 100.0, 0, svg, text, 1, ()),),
            RegionRecord(0, (0.0, 0.0, 100.0, 40.0), svg, svg, text, ()),
        )
    )
    page = PageInput(source_id, pdf.sha256, 0, 100.0, 100.0, svg, sidecar)
    item = LayoutObject(
        "object",
        ObjectKind.TEXT,
        (0.0, 0.0, 100.0, 40.0),
        ("number",),
        "literal transcription only",
        Confidence(None, "test"),
    )
    outputs = ProcessingStore(tmp_path / "processed")
    record = ProcessingObjectAdapter(sources, outputs).process(page, item)
    scope = ProcessingScope(source_id, pdf.sha256, 1, (0,))
    embedder = RecordingEmbedding()
    retrieval = ProcessingRetrieval(sources, outputs, embedder)
    publication = retrieval.build(scope, ((0, record),))
    assert embedder.descriptions == ["Agency 72%"]  # Never SVG or serialized ChartIR.
    assert retrieval.build(scope, ((0, record),)) == publication
    assert embedder.descriptions == ["Agency 72%"]
    reopened = ProcessingRetrieval(sources, ProcessingStore(outputs.root), embedder)
    hit = reopened.search(publication, "Agency", limit=1)[0]
    context = reopened.resolve(publication, hit)
    assert context.description.text == "Agency 72%" and context.member.object_id == "object"
    assert context.scope == "literal-source-transcription-v1"
    canonical_ref = outputs.assets.put(
        TypeAdapter(CanonicalPage).dump_json(canonical_page(page)),
        media_type="application/json",
    )
    partition = PagePartition("layout-v2", source_id, pdf.sha256, 0, "test", (item,), ())
    partition_ref = outputs.assets.put(
        TypeAdapter(PagePartition).dump_json(partition), media_type="application/json"
    )
    page_record = PageProcessingRecord(
        0,
        StageOutcome("canonical", "1" * 64, StageState.SUCCEEDED, "test", canonical_ref),
        StageOutcome("partition", "2" * 64, StageState.SUCCEEDED, "test", partition_ref),
        (record,),
    )
    manifest = ProcessingManifest("processing-v1", scope, "test", (page_record,), publication)
    published = outputs.publish(manifest, sources=sources)
    with pytest.raises(ValueError, match="financial"):
        require_financial_qualification(context)
    with pytest.raises(ValueError, match="snapshot"):
        reopened.resolve(publication, replace(hit, snapshot_id="f" * 64))
    stages = {stage.stage: stage for stage in record.stages}
    receipt_ref = stages["qualification"].artifact
    description_ref = stages["description"].artifact
    ir_ref = stages["ir"].artifact
    assert receipt_ref is not None and description_ref is not None and ir_ref is not None
    receipt = TypeAdapter(LiteralQualification).validate_json(outputs.assets.get(receipt_ref))
    description = TypeAdapter(ObjectDescription).validate_json(outputs.assets.get(description_ref))
    ir = TypeAdapter(TextIR).validate_json(outputs.assets.get(ir_ref))
    if tampering == "producer":
        description = replace(description, producer="forged-producer")
    elif tampering == "confidence":
        description = replace(
            description,
            confidence=Confidence(Decimal("1.0"), "independently verified financial relation"),
        )
    elif tampering == "anchor":
        anchor = replace(receipt.source, bbox=(0.0, 0.0, 5.0, 5.0))
        receipt, description, ir = (
            replace(receipt, source=anchor),
            replace(description, source=anchor),
            replace(ir, source=anchor),
        )
    else:
        fake_svg = outputs.assets.put(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="5" height="5" viewBox="0 0 5 5"/>',
            media_type="image/svg+xml",
        )
        receipt = replace(receipt, source_svg=fake_svg)
        stages["svg"] = replace(stages["svg"], artifact=fake_svg)
    description_ref = outputs.assets.put(
        TypeAdapter(ObjectDescription).dump_json(description),
        media_type="application/json",
    )
    ir_ref = outputs.assets.put(TypeAdapter(TextIR).dump_json(ir), media_type="application/json")
    receipt = replace(receipt, description=description_ref, ir=ir_ref)
    receipt_ref = outputs.assets.put(
        TypeAdapter(LiteralQualification).dump_json(receipt),
        media_type="application/json",
    )
    for name, ref in (
        ("description", description_ref),
        ("ir", ir_ref),
        ("qualification", receipt_ref),
    ):
        stages[name] = replace(stages[name], artifact=ref)
    forged = replace(record, stages=tuple(stages.values()))
    with pytest.raises(ValueError, match=r"producer|crop|anchor|confidence"):
        retrieval.build(scope, ((0, forged),))
    plan, _ = outputs.load_retrieval(publication)
    member = plan.members[0]
    vector = TypeAdapter(RetrievalEmbedding).validate_json(outputs.assets.get(member.embedding))
    vector = replace(vector, description_sha256=description_ref.sha256)
    vector_ref = outputs.assets.put(
        TypeAdapter(RetrievalEmbedding).dump_json(vector), media_type="application/json"
    )
    forged_member = replace(
        member,
        ir=ir_ref,
        description=description_ref,
        qualification=receipt_ref,
        source_svg=receipt.source_svg,
        embedding=vector_ref,
    )
    forged_plan = replace(plan, members=(forged_member,))
    plan_ref = outputs.assets.put(
        TypeAdapter(RetrievalPlan).dump_json(forged_plan), media_type="application/json"
    )
    index_ref = outputs.assets.put(
        TypeAdapter(RetrievalIndex).dump_json(
            RetrievalIndex(
                forged_plan.snapshot_id,
                forged_plan.index_version,
                (IndexEntry(forged_member.member_id, vector.vector),),
            )
        ),
        media_type="application/json",
    )
    forged_publication = RetrievalPublication(
        forged_plan.snapshot_id,
        plan_ref,
        index_ref,
        retrieval_dependencies(forged_plan),
    )
    forged_manifest = replace(
        manifest,
        pages=(replace(page_record, objects=(forged,)),),
        retrieval=forged_publication,
    )
    with pytest.raises(ValueError, match=r"producer|crop|anchor|confidence"):
        outputs.publish(forged_manifest, sources=sources)
    assert (outputs.root / "current-processing").read_text().strip() == published
    member_ir = context.member.ir
    outputs.assets.asset_path(member_ir).unlink()
    with pytest.raises(FileNotFoundError):
        reopened.resolve(publication, hit)
