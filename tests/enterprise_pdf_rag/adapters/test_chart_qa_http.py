"""The public contract distinguishes cited answers, refusals and pin failures."""

import asyncio
from typing import Never

from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from enterprise_pdf_rag.adapters.http.chart_qa import create_chart_qa_router
from enterprise_pdf_rag.figures.chart_qa.models import (
    ChartQueryError,
    QueryFailure,
    QueryPin,
)
from enterprise_pdf_rag.figures.chart_qa.service import ChartQAService
from tests.enterprise_pdf_rag.figures.test_chart_qa import PinnedResolver, qualified_context


def test_public_lookup_difference_refusal_and_strict_request() -> None:
    context = qualified_context()
    app = FastAPI()
    app.include_router(create_chart_qa_router(ChartQAService(PinnedResolver(context))))
    base: dict[str, object] = {
        "kind": "chart",
        "processing_id": context.pin.processing_id,
        "snapshot_id": context.pin.snapshot_id,
        "member_id": context.pin.member_id,
        "operation": "lookup",
        "series": "VONB",
        "period": "1H26",
        "unit": "%",
        "points": [{"point_id": context.chart.points[0].point_id, "category": "Agency"}],
    }

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            lookup = await client.post("/v1/queries", json=base)
            assert lookup.status_code == 200, lookup.text
            payload = lookup.json()
            assert payload["schema_version"] == "chart-qa-v1"
            assert payload["status"] == "answered"
            assert payload["answer"] == {
                "value": "72",
                "unit": "%",
                "value_kind": "explicit",
                "raw_display": "72%",
                "verification": "verified",
                "confidence": {
                    "score": None,
                    "method": "source-qualified explicit percentage; deterministic field-to-occurrence verification",
                },
            }
            assert len(payload["inputs"][0]["citations"]) == 5
            assert payload["calculation_receipt"] is None
            difference = await client.post(
                "/v1/queries",
                json={
                    **base,
                    "operation": "percentage_point_difference",
                    "points": [
                        {"point_id": p.point_id, "category": p.category.text}
                        for p in context.chart.points
                    ],
                },
            )
            assert difference.status_code == 200
            receipt = difference.json()["calculation_receipt"]
            assert receipt["receipt_id"].startswith("chart-calculation-v1:")
            assert receipt["output_value"] == "44"
            assert receipt["output_unit"] == "percentage_points"
            refusal = await client.post("/v1/queries", json={**base, "period": "1H25"})
            assert refusal.status_code == 200
            assert refusal.json()["status"] == "abstained"
            assert refusal.json()["answer"] is None
            assert refusal.json()["refusal_reason"] == "period_mismatch"
            for extra in ({"value": "73"}, {"verified": True}, {"formula": "72 - 28"}):
                invalid = await client.post("/v1/queries", json={**base, **extra})
                assert invalid.status_code == 422
            cardinality = await client.post(
                "/v1/queries", json={**base, "operation": "percentage_point_difference"}
            )
            assert cardinality.status_code == 422
            crossed = await client.post("/v1/queries", json={**base, "snapshot_id": "f" * 64})
            assert crossed.status_code == 409
            assert crossed.json()["error"]["code"] == "pin_conflict"

    asyncio.run(exercise())


def test_unavailable_dependency_is_503_not_a_business_refusal() -> None:
    class MissingResolver(PinnedResolver):
        def resolve(self, pin: QueryPin) -> Never:
            raise ChartQueryError(
                QueryFailure.UNAVAILABLE_EVIDENCE,
                "Required immutable source is missing",
            )

    context = qualified_context()
    app = FastAPI()
    app.include_router(create_chart_qa_router(ChartQAService(MissingResolver(context))))

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/queries",
                json={
                    "kind": "chart",
                    "processing_id": context.pin.processing_id,
                    "snapshot_id": context.pin.snapshot_id,
                    "member_id": context.pin.member_id,
                    "operation": "lookup",
                    "series": "VONB",
                    "period": "1H26",
                    "unit": "%",
                    "points": [{"point_id": "agency", "category": "Agency"}],
                },
            )
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "unavailable_evidence"

    asyncio.run(exercise())
