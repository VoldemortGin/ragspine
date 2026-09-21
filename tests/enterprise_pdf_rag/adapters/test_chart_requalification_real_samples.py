"""Read-only smoke over the pinned AIA release: charts re-project from stored bytes only.

No model, no network, no writes: the whole run is a ``dry_run`` re-projection of the
published snapshot's own ``svg`` / ``ir`` / ``description`` / ``model_view`` branches.
"""

import pytest

from enterprise_pdf_rag.adapters.aia_ingestion import AIA_OUTPUT
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.visual_requalification import (
    ObjectRequalification,
    requalify_visual_objects,
)
from enterprise_pdf_rag.processing.models import ObjectKind

_PROCESSING = AIA_OUTPUT / "pages-001-020"
_POINTER = _PROCESSING / "current-processing"
# The page-18 "Product Mix" donut: four sectors, every label and percentage printed in the
# figure, the category "Traditional Protection" wrapped over two source spans.
_PRODUCT_MIX = "layout-object-v1:6c33c6c33cf76610ff0cd69f0cdf19a1e095bc251e2f8f41001b6078c8bceb41"
# The page-18 "Distribution Mix" donut, already admitted under the ADR 0008 geometry and
# source-paint proof. Re-projecting it verbatim would weaken it, so it must be left alone.
_DISTRIBUTION_MIX = (
    "layout-object-v1:23696d47e6598f4ff6644d9ba1e5ef8d641eb78b161c600b5ceb1181301b2fa0"
)

pytestmark = pytest.mark.skipif(
    not _POINTER.is_file(), reason="the pinned AIA release is not present"
)


@pytest.fixture(scope="module")
def verdicts() -> dict[str, ObjectRequalification]:
    summary = requalify_visual_objects(
        LocalDocumentStore(AIA_OUTPUT, activate_on_publish=False),
        ProcessingStore(_PROCESSING),
        processing_id=_POINTER.read_text().strip(),
        dry_run=True,
    )
    assert summary.draft_processing_id is None, "a dry run must not save a draft"
    return {item.object_id: item for item in summary.objects}


def test_every_chart_in_the_release_gets_a_verdict(
    verdicts: dict[str, ObjectRequalification],
) -> None:
    charts = [item for item in verdicts.values() if item.kind is ObjectKind.CHART]
    assert len(charts) == 29
    assert not [item for item in charts if item.outcome == "withheld" and item.diagnostic is None]


def test_the_product_mix_donut_keeps_its_four_printed_points(
    verdicts: dict[str, ObjectRequalification],
) -> None:
    """Its four percentages and four categories are all printed inside the figure."""
    verdict = verdicts[_PRODUCT_MIX]

    assert verdict.kind is ObjectKind.CHART
    assert verdict.outcome == "qualified"
    assert verdict.diagnostic is None
    assert verdict.qualified_claim_count == 4


def test_a_geometry_proved_chart_is_never_re_projected(
    verdicts: dict[str, ObjectRequalification],
) -> None:
    """``explicit-distribution-shares`` is the stronger proof; this module must not touch it."""
    verdict = verdicts[_DISTRIBUTION_MIX]

    assert verdict.outcome == "unchanged"
    assert verdict.diagnostic is None
