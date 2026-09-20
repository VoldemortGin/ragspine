"""The first slice exercises the real PDF boundary without remote models."""

from decimal import Decimal

from enterprise_pdf_rag.adapters.runtime import create_runtime
from enterprise_pdf_rag.figures.models import ExecutionMode, Verification


def test_offline_demo_retrieves_description_and_hydrates_its_chart() -> None:
    runtime = create_runtime(mode=ExecutionMode.OFFLINE_DEMO)
    result = runtime.run_demo(query="Revenue 2025", snapshot_id="demo-v1")

    assert result.pdf.startswith(b"%PDF-")
    assert result.svg.verification is Verification.VERIFIED
    assert result.hits[0].description_id == result.bundle.description_id
    assert result.context.chart_ir.artifact_id == result.bundle.chart_ir_artifact_id
    assert result.context.svg.binding == result.context.chart_ir.binding
    assert result.context.chart_ir.points[1].value.value == Decimal("15")
    assert result.context.snapshot_id == "demo-v1"
    assert result.context.evidence
    assert "2025" in result.hits[0].text
