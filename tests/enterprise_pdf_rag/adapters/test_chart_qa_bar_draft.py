"""Real source admission appends one vector and preserves the published release."""

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from enterprise_pdf_rag.adapters.chart_qa_bar_promotion import (
    create_displayed_bar_draft,
)
from enterprise_pdf_rag.adapters.chart_qa_displayed import StoredDisplayResolver
from enterprise_pdf_rag.adapters.http.processing_review import create_processing_router
from enterprise_pdf_rag.adapters.processing_export import export_processing_review
from enterprise_pdf_rag.adapters.processing_retrieval import resolve_processing_context
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.processing.retrieval import (
    PinnedRetrievalHit,
    require_financial_qualification,
)
from ragspine.extraction.evidence.figures.chart_qa.displayed_service import (
    DisplayedChartQAService,
)
from ragspine.extraction.evidence.figures.chart_qa.models import (
    ChartQuestion,
    Operation,
    PointSelector,
)
from tests.enterprise_pdf_rag.adapters.chart_qa_bar_fixture import published_bar_input
from tests.enterprise_pdf_rag.processing.test_persistent_retrieval import RecordingEmbedding


def test_source_qualified_bar_draft_reuses_prior_vectors_and_is_idempotent(
    tmp_path: Path,
) -> None:
    sources, outputs, prior_id, item, _ = published_bar_input(tmp_path)
    prior = outputs.load(prior_id)
    assert prior.retrieval is not None
    prior_plan, prior_index = outputs.load_retrieval(prior.retrieval)
    embedder = RecordingEmbedding()
    release = create_displayed_bar_draft(
        sources,
        outputs,
        embedder,
        processing_id=prior_id,
        page_index=0,
        object_id=item.object_id,
    )
    assert release.reused_vectors == 1 and release.added_vectors == 1
    assert release.numeric_claim_count == 2
    # The new member embeds the chart index-text projection (ADR 0012), not the description.
    assert embedder.descriptions == [
        "Expense Ratio bar chart figure 1H21 Expense Ratio 15% 1H23 Expense Ratio 6%"
    ]
    assert outputs.load_current()[0] == prior_id
    assert release.previous_processing_id == prior_id
    assert release.previous_snapshot_id == prior.retrieval.snapshot_id
    revised = outputs.load(release.current.processing_id)
    assert revised.retrieval is not None
    plan, index = outputs.load_retrieval(revised.retrieval)
    assert plan.members[:-1] == prior_plan.members
    assert len(plan.members) == 2
    vectors = {entry.member_id: entry.vector for entry in index.entries}
    assert all(vectors[entry.member_id] == entry.vector for entry in prior_index.entries)
    member = plan.members[-1]
    assert len(member.lineage_refs) == 8
    raw_before = {stage.stage: stage for stage in prior.pages[0].objects[0].stages}
    raw_after = {stage.stage: stage for stage in revised.pages[0].objects[0].stages}
    assert all(raw_after[name] == stage for name, stage in raw_before.items())
    service = DisplayedChartQAService(
        StoredDisplayResolver(
            sources,
            ProcessingStore(outputs.root),
            processing_id=release.current.processing_id,
        )
    )
    question = ChartQuestion(
        release.current,
        Operation.LOOKUP,
        "Expense Ratio",
        "1H21",
        "%",
        (PointSelector("p-1H21", "1H21"),),
    )
    answer = service.answer(question)
    assert answer.answer is not None and answer.answer.value == Decimal("15")
    assert answer.answer.raw_display == "15%"
    assert answer.description_normalization is not None and len(answer.page_context) == 1
    unavailable = service.answer(
        replace(question, period="1H22", points=(PointSelector("p-1H22", "1H22"),))
    )
    assert unavailable.refusal_reason == "value_unavailable"
    context = resolve_processing_context(
        sources,
        outputs,
        revised.retrieval,
        PinnedRetrievalHit(plan.snapshot_id, member.member_id, 1.0),
    )
    with pytest.raises(ValueError, match="financial"):
        require_financial_qualification(context)
    repeated = create_displayed_bar_draft(
        sources,
        outputs,
        embedder,
        processing_id=prior_id,
        page_index=0,
        object_id=item.object_id,
    )
    assert repeated == release
    assert len(embedder.descriptions) == 1


def test_another_embedding_model_is_rejected_before_any_new_embedding(
    tmp_path: Path,
) -> None:
    sources, outputs, prior_id, item, _ = published_bar_input(tmp_path)

    class AnotherEmbedding(RecordingEmbedding):
        fingerprint = "different-model"

    embedder = AnotherEmbedding()
    with pytest.raises(ValueError, match="model"):
        create_displayed_bar_draft(
            sources,
            outputs,
            embedder,
            processing_id=prior_id,
            page_index=0,
            object_id=item.object_id,
        )
    assert embedder.descriptions == []
    assert outputs.load_current()[0] == prior_id


def test_stored_v2_http_and_review_distinguish_lookup_from_numeric_relations(
    tmp_path: Path,
) -> None:
    sources, outputs, prior_id, item, _ = published_bar_input(tmp_path)
    release = create_displayed_bar_draft(
        sources,
        outputs,
        RecordingEmbedding(),
        processing_id=prior_id,
        page_index=0,
        object_id=item.object_id,
    )
    review = export_processing_review(
        sources, outputs, release.current.processing_id, update_current=False
    )
    rows = json.loads(review.with_name("coverage.json").read_text())
    chart_row = next(row for row in rows if row["kind"] == "Chart")
    assert chart_row["displayed_lookup_qualified"] == 1
    assert chart_row["numeric_qualified"] == 0
    app = FastAPI()
    app.include_router(create_processing_router(sources, outputs, release.current.processing_id))
    payload: dict[str, object] = {
        "schema_version": "chart-qa-v2",
        "kind": "chart",
        "operation": "lookup",
        "processing_id": release.current.processing_id,
        "snapshot_id": release.current.snapshot_id,
        "member_id": release.current.member_id,
        "series": "Expense Ratio",
        "period": "1H23",
        "unit": "%",
        "points": [{"point_id": "p-1H23", "category": "1H23"}],
    }

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/v1/processing/status")).status_code == 200
            answer = await client.post("/v1/queries", json=payload)
            assert answer.status_code == 200
            assert answer.json()["answer"]["value"] == "6"
            assert answer.json()["semantic_scope"] == "source_display_only"
            crossed = await client.post("/v1/queries", json={**payload, "snapshot_id": "f" * 64})
            assert crossed.status_code == 409
            unsupported = await client.post(
                "/v1/queries",
                json={**payload, "operation": "percentage_point_difference"},
            )
            assert unsupported.status_code == 422

    asyncio.run(exercise())
    assert outputs.load_current()[0] == prior_id
