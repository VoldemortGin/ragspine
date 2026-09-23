"""Explicit composition root for the executable offline figure slice."""

from dataclasses import dataclass

from enterprise_pdf_rag.adapters.memory import (
    MemoryDescriptionIndex,
    MemoryFigureRepository,
)
from enterprise_pdf_rag.adapters.offline import (
    OfflineChartExtractor,
    OfflineDescriptionEmbedder,
    OfflineDescriptionGenerator,
)
from enterprise_pdf_rag.adapters.qualification import AuthoredFixtureQualifier
from ragspine.extraction.evidence.figures.models import (
    ExecutionMode,
    FigureBundle,
    FigureHit,
    ReasoningView,
    SvgArtifact,
)
from ragspine.extraction.evidence.figures.service import FiguresService


@dataclass(frozen=True, slots=True)
class DemoResult:
    pdf: bytes
    svg: SvgArtifact
    bundle: FigureBundle
    hits: tuple[FigureHit, ...]
    context: ReasoningView


@dataclass(frozen=True, slots=True)
class Runtime:
    service: FiguresService
    source: AuthoredFixtureQualifier

    def run_demo(self, *, query: str, snapshot_id: str) -> DemoResult:
        pdf, svg = self.source.create_source()
        bundle = self.service.build(svg, snapshot_id=snapshot_id)
        hits = self.service.search(query, snapshot_id=snapshot_id)
        if not hits:
            raise ValueError("No demo hit found")
        return DemoResult(
            pdf,
            svg,
            bundle,
            hits,
            self.service.resolve(hits[0], snapshot_id=snapshot_id),
        )


def create_runtime(*, mode: ExecutionMode) -> Runtime:
    if mode is not ExecutionMode.OFFLINE_DEMO:
        raise ValueError(
            "Production requires qualified source and model adapters; no mock fallback is allowed"
        )
    qualifier = AuthoredFixtureQualifier()
    return Runtime(
        FiguresService(
            OfflineChartExtractor(),
            OfflineDescriptionGenerator(),
            OfflineDescriptionEmbedder(),
            MemoryFigureRepository(),
            MemoryDescriptionIndex(),
            mode=mode,
            qualifier=qualifier,
        ),
        qualifier,
    )
