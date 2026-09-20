"""ChartQA reopens actual immutable members and repeats source qualification."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from pydantic import TypeAdapter
from tests.adapters.test_chart_publication import member_fixture
from tests.processing.test_persistent_retrieval import RecordingEmbedding

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.chart_publication import (
    NumericLabelPublicationReceipt,
    parse_chart_receipt,
)
from enterprise_pdf_rag.adapters.chart_qa import StoredChartResolver
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.processing_review import create_processing_router
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.cli import main
from enterprise_pdf_rag.documents.models import AssetRef
from enterprise_pdf_rag.figures.chart_qa.models import (
    ChartQueryError,
    ChartQuestion,
    Operation,
    PointSelector,
    QueryFailure,
    QueryPin,
    QueryStatus,
    RefusalReason,
)
from enterprise_pdf_rag.figures.chart_qa.service import ChartQAService
from enterprise_pdf_rag.figures.models import Confidence
from enterprise_pdf_rag.processing.models import (
    CanonicalPage,
    LayoutObject,
    ObjectKind,
    ObjectProcessingRecord,
    PageInput,
    PagePartition,
    PageProcessingRecord,
    ProcessingManifest,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.service import canonical_page


def published_chart(
    tmp_path: Path, *, labels: bool = False, actual_pdf: bool = False
) -> tuple[LocalDocumentStore, ProcessingStore, QueryPin]:
    sources, assets, scope, member, _ = member_fixture(
        tmp_path, label_scope=labels, actual_pdf=actual_pdf
    )
    receipt = parse_chart_receipt(assets.get(member.qualification))
    source = sources.load(scope.source_manifest_id)
    saved_page = source.manifest.pages[0]
    page = PageInput(
        scope.source_manifest_id,
        scope.source_sha256,
        0,
        saved_page.width,
        saved_page.height,
        saved_page.svg,
        read_text_sidecar(sources, source, 0),
    )
    item = LayoutObject(
        "chart",
        ObjectKind.CHART,
        receipt.qualification.source.bbox,
        tuple(span.span_id for span in page.text.spans),
        "authored source",
        Confidence(None, "authored fixture"),
    )
    partition = PagePartition(
        "layout-v2",
        scope.source_manifest_id,
        scope.source_sha256,
        0,
        "authored",
        (item,),
        (),
    )
    outputs = ProcessingStore(assets.root)
    canonical_ref = assets.put(
        TypeAdapter(CanonicalPage).dump_json(canonical_page(page)),
        media_type="application/json",
    )
    partition_ref = assets.put(
        TypeAdapter(PagePartition).dump_json(partition), media_type="application/json"
    )
    refs: tuple[tuple[str, AssetRef], ...] = (
        ("qualified_ir", member.ir),
        ("qualified_description", member.description),
        ("qualification", member.qualification),
        ("svg", member.source_svg),
        ("ir", receipt.raw_chart),
        ("description", receipt.raw_description),
        ("model_view", receipt.view),
    )
    if isinstance(receipt, NumericLabelPublicationReceipt):
        refs += (("source_paint_proof", receipt.source_paint_proof),)
    record = ObjectProcessingRecord(
        "chart",
        ObjectKind.CHART,
        tuple(
            StageOutcome(name, str(index) * 64, StageState.SUCCEEDED, "authored", ref)
            for index, (name, ref) in enumerate(refs, 1)
        ),
    )
    publication = ProcessingRetrieval(sources, outputs, RecordingEmbedding()).build(
        scope, ((0, record),)
    )
    processing_id = outputs.publish(
        ProcessingManifest(
            "processing-v1",
            scope,
            "authored",
            (
                PageProcessingRecord(
                    0,
                    StageOutcome(
                        "canonical",
                        "a" * 64,
                        StageState.SUCCEEDED,
                        "authored",
                        canonical_ref,
                    ),
                    StageOutcome(
                        "partition",
                        "b" * 64,
                        StageState.SUCCEEDED,
                        "authored",
                        partition_ref,
                    ),
                    (record,),
                ),
            ),
            publication,
        ),
        sources=sources,
    )
    plan, _ = outputs.load_retrieval(publication)
    return (
        sources,
        outputs,
        QueryPin(processing_id, publication.snapshot_id, plan.members[0].member_id),
    )


def stored_question(pin: QueryPin) -> ChartQuestion:
    return ChartQuestion(
        pin, Operation.LOOKUP, "VONB", "1H26", "%", (PointSelector("Agency", "Agency"),)
    )


def test_query_reopens_member_and_source_chain_then_refuses_crossed_pins(
    tmp_path: Path,
) -> None:
    sources, outputs, pin = published_chart(tmp_path)
    resolver = StoredChartResolver(sources, outputs, processing_id=pin.processing_id)
    context = resolver.resolve(pin)
    point = context.chart.points[0]
    query = replace(
        stored_question(pin),
        points=(PointSelector(point.point_id, point.category.text),),
    )
    result = ChartQAService(resolver).answer(query)
    assert result.status is QueryStatus.ANSWERED
    assert result.inputs[0].citations[0].svg_artifact_id == context.svg.artifact_id
    for crossed in (
        replace(pin, processing_id="f" * 64),
        replace(pin, snapshot_id="f" * 64),
        replace(pin, member_id="f" * 64),
    ):
        with pytest.raises(ChartQueryError) as caught:
            resolver.resolve(crossed)
        assert caught.value.code is QueryFailure.PIN_CONFLICT


def test_missing_raw_branch_and_modified_asset_fail_after_prior_success(
    tmp_path: Path,
) -> None:
    sources, outputs, pin = published_chart(tmp_path)
    resolver = StoredChartResolver(sources, outputs, processing_id=pin.processing_id)
    resolver.resolve(pin)
    manifest = outputs.load(pin.processing_id)
    assert manifest.retrieval is not None
    plan, _ = outputs.load_retrieval(manifest.retrieval)
    path = outputs.assets.asset_path(plan.members[0].lineage_refs[0])
    content = path.read_bytes()
    path.unlink()
    with pytest.raises(ChartQueryError) as caught:
        resolver.resolve(pin)
    assert caught.value.code is QueryFailure.UNAVAILABLE_EVIDENCE
    path.write_bytes(content + b" ")
    with pytest.raises(ChartQueryError) as caught:
        resolver.resolve(pin)
    assert caught.value.code is QueryFailure.INVALID_EVIDENCE


def test_old_labels_only_member_is_still_a_business_refusal(tmp_path: Path) -> None:
    sources, outputs, pin = published_chart(tmp_path, labels=True)
    result = ChartQAService(
        StoredChartResolver(sources, outputs, processing_id=pin.processing_id)
    ).answer(stored_question(pin))
    assert result.status is QueryStatus.ABSTAINED
    assert result.refusal_reason is RefusalReason.UNQUALIFIED_MEMBER
    assert result.answer is None


def test_processing_router_and_cli_share_the_source_bound_query(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources, outputs, pin = published_chart(tmp_path)
    context = StoredChartResolver(
        sources, outputs, processing_id=pin.processing_id
    ).resolve(pin)
    point = context.chart.points[0]
    body = {
        "kind": "chart",
        "processing_id": pin.processing_id,
        "snapshot_id": pin.snapshot_id,
        "member_id": pin.member_id,
        "operation": "lookup",
        "series": "VONB",
        "period": "1H26",
        "unit": "%",
        "points": [{"point_id": point.point_id, "category": point.category.text}],
    }
    app = FastAPI()
    app.include_router(create_processing_router(sources, outputs, pin.processing_id))

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/v1/queries", json=body)
            assert response.status_code == 200, response.text
            assert response.json()["answer"]["value"] == "72"
            assert "/v1/queries" in app.openapi()["paths"]

    asyncio.run(exercise())
    request = tmp_path / "query.json"
    request.write_text(json.dumps(body))
    assert (
        main(
            [
                "chart-qa",
                "--request",
                str(request),
                "--source-store",
                str(sources.root),
                "--processing-store",
                str(outputs.root),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["processing_id"] == pin.processing_id
    assert result["answer"]["value"] == "72"
