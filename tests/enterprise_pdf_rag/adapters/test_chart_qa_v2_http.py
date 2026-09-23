"""The explicit v2 boundary never falls back to v1 or broadens bar operations."""

import asyncio

from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from enterprise_pdf_rag.adapters.http.chart_qa import create_chart_qa_router
from enterprise_pdf_rag.adapters.http.chart_qa_schemas import PointSelectorInput
from enterprise_pdf_rag.adapters.http.chart_qa_v2_schemas import (
    DisplayedChartQueryRequest,
)
from ragspine.extraction.evidence.figures.chart_qa.displayed_service import (
    DisplayedChartQAService,
)
from ragspine.extraction.evidence.figures.chart_qa.service import ChartQAService
from tests.enterprise_pdf_rag.figures.test_chart_qa import PinnedResolver, qualified_context
from tests.enterprise_pdf_rag.figures.test_chart_qa_displayed import (
    DisplayedResolver,
    displayed_context,
)


def test_explicit_v2_lookup_has_its_own_scoped_response_and_original_occurrences() -> None:
    context = displayed_context()
    displayed = DisplayedResolver(context)
    legacy = PinnedResolver(qualified_context())
    app = FastAPI()
    app.include_router(
        create_chart_qa_router(
            ChartQAService(legacy), displayed_service=DisplayedChartQAService(displayed)
        )
    )
    payload = DisplayedChartQueryRequest(
        schema_version="chart-qa-v2",
        kind="chart",
        processing_id=context.pin.processing_id,
        snapshot_id=context.pin.snapshot_id,
        member_id=context.pin.member_id,
        operation="lookup",
        series="Expense Ratio",
        period="1H24",
        unit="%",
        points=[PointSelectorInput(point_id="point-1h24", category="1H24")],
    ).model_dump(mode="json")

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/queries", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["schema_version"] == "chart-qa-v2"
        assert data["semantic_scope"] == "source_display_only"
        assert data["answer"]["value"] == "8.2"
        assert data["answer"]["raw_display"] == "8.2%"
        assert data["calculation_receipt"] is None
        citations = {item["role"]: item for item in data["inputs"][0]["citations"]}
        assert citations["category"]["citation"] == citations["period"]["citation"]
        assert citations["period"]["raw_field_path"] == "points.point-1h24.category"
        assert data["page_context"][0]["scope"] == "page_context"
        assert data["page_context"][0]["text"] == context.page_context[0].text

    asyncio.run(exercise())
    assert legacy.calls == [] and displayed.calls == [context.pin]


def test_v2_rejects_unsupported_operations_and_caller_asserted_qualification() -> None:
    context = displayed_context()
    displayed = DisplayedResolver(context)
    legacy = PinnedResolver(qualified_context())
    app = FastAPI()
    app.include_router(
        create_chart_qa_router(
            ChartQAService(legacy), displayed_service=DisplayedChartQAService(displayed)
        )
    )
    base: dict[str, object] = {
        "schema_version": "chart-qa-v2",
        "kind": "chart",
        "operation": "lookup",
        "processing_id": context.pin.processing_id,
        "snapshot_id": context.pin.snapshot_id,
        "member_id": context.pin.member_id,
        "series": "Expense Ratio",
        "period": "1H24",
        "unit": "%",
        "points": [{"point_id": "point-1h24", "category": "1H24"}],
    }

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for update in (
                {"operation": "percentage_point_difference"},
                {"operation": "ratio"},
                {"schema_version": "chart-qa-v3"},
                {"value": "8.2"},
                {"verified": True},
                {"semantic_scope": "source_display_only"},
                {"formula": "8.2 - 6.9"},
                {
                    "points": [
                        {"point_id": "point-1h24", "category": "1H24"},
                        {"point_id": "point-1h26", "category": "1H26"},
                    ]
                },
            ):
                invalid = await client.post("/v1/queries", json={**base, **update})
                assert invalid.status_code == 422
            assert legacy.calls == [] and displayed.calls == []
            missing = await client.post(
                "/v1/queries",
                json={
                    **base,
                    "period": "1H25",
                    "points": [{"point_id": "point-1h25", "category": "1H25"}],
                },
            )
            assert missing.status_code == 200
            assert missing.json()["status"] == "abstained"
            assert missing.json()["refusal_reason"] == "value_unavailable"
            assert missing.json()["answer"] is None
            crossed = await client.post("/v1/queries", json={**base, "snapshot_id": "f" * 64})
            assert crossed.status_code == 409
            assert crossed.json()["error"]["code"] == "pin_conflict"

    asyncio.run(exercise())


def test_v2_without_a_trusted_bar_port_is_503_with_no_v1_fallback() -> None:
    context = displayed_context()
    legacy = PinnedResolver(qualified_context())
    app = FastAPI()
    app.include_router(create_chart_qa_router(ChartQAService(legacy)))

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post(
                "/v1/queries",
                json={
                    "schema_version": "chart-qa-v2",
                    "kind": "chart",
                    "operation": "lookup",
                    "processing_id": context.pin.processing_id,
                    "snapshot_id": context.pin.snapshot_id,
                    "member_id": context.pin.member_id,
                    "series": "Expense Ratio",
                    "period": "1H24",
                    "unit": "%",
                    "points": [{"point_id": "point-1h24", "category": "1H24"}],
                },
            )
            assert result.status_code == 503
            assert result.json()["error"]["code"] == "unavailable_evidence"
            assert legacy.calls == []

    asyncio.run(exercise())
