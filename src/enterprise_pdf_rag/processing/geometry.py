"""Containment between a model-rendered region and canonical source geometry.

Layout and semantics models receive canonical coordinates in the prompt and echo
them back in their shortest decimal form: pdfspine's ``42.400000000000006`` comes
back as ``42.4`` and ``307.9999999999998`` as ``308``. That rendering noise is
below 1e-12 pt for page-sized values, while the smallest real layout offset is
orders of magnitude above 1e-3 pt, so a fixed 1e-6 pt slack on the outer box
neither unbinds a span from its own region nor admits a span the region misses.
"""

from enterprise_pdf_rag.documents.models import Bounds

COORDINATE_TOLERANCE = 1e-6


def contains(outer: Bounds, inner: Bounds, *, tolerance: float = COORDINATE_TOLERANCE) -> bool:
    """``inner`` is a non-degenerate box lying within ``outer``, up to ``tolerance`` pt per edge.

    ``outer`` is the box a model rendered; ``inner`` keeps its canonical coordinates.
    """
    return (
        outer[0] - tolerance <= inner[0] < inner[2] <= outer[2] + tolerance
        and outer[1] - tolerance <= inner[1] < inner[3] <= outer[3] + tolerance
    )
