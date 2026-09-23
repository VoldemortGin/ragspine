"""Identity of the single, explicitly selected business source."""

from ragspine.extraction.evidence.document.models import DocumentSpec

AIA_SPEC = DocumentSpec(
    "aia-group-2026-interim-results-presentation.pdf",
    "df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e",
    71,
    24,
    (731.5, 145.0, 944.2, 385.0),
    (
        "Page 25 visual comparison: the native SVG bottom red baseline is thinner than the PDF render; visual equivalence remains pending. See data/aia-svg-review/review.json.",
    ),
)
