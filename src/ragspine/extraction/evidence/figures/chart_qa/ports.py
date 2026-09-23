"""Resolve the pinned immutable membership and independently repeat its proof."""

from typing import Protocol, runtime_checkable

from ragspine.extraction.evidence.figures.chart_qa.models import ChartContext, QueryPin


@runtime_checkable
class ChartContextResolver(Protocol):
    def resolve(self, pin: QueryPin) -> ChartContext:
        """Verify source, dependencies, raw branches and qualification; no inference."""
        ...
