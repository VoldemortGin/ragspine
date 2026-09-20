"""Live layout commands require explicit scope and bounded new-call budgets."""

import pytest

from enterprise_pdf_rag import cli
from enterprise_pdf_rag.adapters.processing_runtime import (
    ProcessingRunSummary,
    process_aia_layout,
)


@pytest.mark.parametrize(
    ("pages", "budget"), [((21,), 1), ((0,), 1), ((18, 18), 2), ((18,), 2), ((18,), -1)]
)
def test_layout_command_rejects_scope_and_budget_before_loading_provider(
    pages: tuple[int, ...], budget: int
) -> None:
    with pytest.raises(ValueError, match=r"scope|budget"):
        process_aia_layout(physical_pages=pages, max_live_calls=budget)


def test_explicit_layout_retry_forwards_one_attempt_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(
        *,
        physical_pages: tuple[int, ...],
        max_live_calls: int,
        timeout: float,
        retry_failed: bool,
    ) -> ProcessingRunSummary:
        assert physical_pages == (18,) and max_live_calls == 1
        assert timeout == 180.0 and retry_failed is True
        return ProcessingRunSummary(
            processing_id="a" * 64,
            source_page_count=71,
            selected_physical_pages=(18,),
            layout_succeeded_pages=1,
            object_count=3,
            semantic_status="deferred",
            review_path="data/output/review.html",
        )

    monkeypatch.setattr(cli, "process_aia_layout", run)
    assert (
        cli.main(
            [
                "process-aia-layout",
                "--page",
                "18",
                "--max-live-calls",
                "1",
                "--timeout",
                "180",
                "--retry-failed",
            ]
        )
        == 0
    )
