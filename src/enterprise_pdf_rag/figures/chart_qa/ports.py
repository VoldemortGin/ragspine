"""Resolve the pinned immutable membership and independently repeat its proof."""

from typing import Protocol, runtime_checkable

from enterprise_pdf_rag.figures.chart_qa.models import ChartContext, QueryPin


@runtime_checkable
class ChartContextResolver(Protocol):
    def resolve(self, pin: QueryPin) -> ChartContext:
        """Verify source, dependencies, raw branches and qualification; no inference."""
        ...
