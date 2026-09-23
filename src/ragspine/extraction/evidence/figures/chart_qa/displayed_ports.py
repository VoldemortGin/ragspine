"""A displayed-bar proof is a source adapter responsibility, never user input."""

from typing import Protocol, runtime_checkable

from ragspine.extraction.evidence.figures.chart_qa.displayed_models import DisplayedLookupContext
from ragspine.extraction.evidence.figures.chart_qa.models import QueryPin


@runtime_checkable
class DisplayedContextResolver(Protocol):
    def resolve(self, pin: QueryPin) -> DisplayedLookupContext:
        """Repeat trusted PDF/profile/paint/label/raw/normalization checks at pin."""
        ...
