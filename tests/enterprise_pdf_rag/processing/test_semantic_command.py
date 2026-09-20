"""Source semantic calls have an explicit bounded page scope and call budget."""

import pytest

from enterprise_pdf_rag.adapters.processing_runtime import process_aia_semantics


@pytest.mark.parametrize(
    "pages,budget",
    [((21,), 2), ((0,), 2), ((1, 1), 2), ((), 2), ((1,), -1), ((1,), 201)],
)
def test_semantic_command_rejects_invalid_scope_before_loading_providers(
    pages: tuple[int, ...], budget: int
) -> None:
    with pytest.raises(ValueError, match=r"scope|budget"):
        process_aia_semantics(physical_pages=pages, max_live_calls=budget)
