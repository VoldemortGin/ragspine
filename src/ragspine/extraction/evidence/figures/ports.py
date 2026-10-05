"""I/O contracts; concrete storage, model and vector SDKs live in adapters."""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ragspine.extraction.evidence.figures.models import (
    ChartIR,
    DescriptionEmbedding,
    DescriptionIndexRecord,
    FigureBundle,
    FigureHit,
    FigureQualification,
    QualifiedFigurePair,
    SvgArtifact,
    TextDescription,
)


@runtime_checkable
class FigureQualificationProvider(Protocol):
    def qualification_for(self, svg: SvgArtifact) -> FigureQualification | None: ...


@runtime_checkable
class ScopedFigureQualificationProvider(Protocol):
    def qualify_pair(
        self, svg: SvgArtifact, chart: ChartIR, description: TextDescription
    ) -> QualifiedFigurePair: ...


@runtime_checkable
class ChartExtractor(Protocol):
    def extract(self, svg: SvgArtifact) -> ChartIR | None: ...


@runtime_checkable
class DescriptionGenerator(Protocol):
    def generate(self, svg: SvgArtifact) -> TextDescription: ...


@runtime_checkable
class EmbeddingPort(Protocol):
    @property
    def fingerprint(self) -> str: ...

    def embed_description(self, text: str) -> tuple[float, ...]: ...

    def embed_query(self, text: str) -> tuple[float, ...]: ...


@runtime_checkable
class BatchEmbeddingPort(EmbeddingPort, Protocol):
    """Optional capability: many descriptions per request, each vector exactly its own.

    ``embed_descriptions(texts)[i]`` must equal ``embed_description(texts[i])``; batching is
    transport only. ``request_count`` counts the requests sent, so callers can report it.
    """

    @property
    def request_count(self) -> int: ...

    def embed_descriptions(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


@runtime_checkable
class FigureRepository(Protocol):
    def save(
        self,
        bundle: FigureBundle,
        svg: SvgArtifact,
        chart: ChartIR,
        description: TextDescription,
    ) -> None: ...

    def get_bundle(self, snapshot_id: str, bundle_id: str) -> FigureBundle | None: ...

    def get_svg(self, artifact_id: str) -> SvgArtifact | None: ...

    def get_chart(self, artifact_id: str) -> ChartIR | None: ...

    def get_description(self, artifact_id: str) -> TextDescription | None: ...

    def get_embedding(self, key: str) -> DescriptionEmbedding | None: ...

    def save_embedding(self, embedding: DescriptionEmbedding) -> None: ...


@runtime_checkable
class DescriptionIndex(Protocol):
    def add(self, record: DescriptionIndexRecord) -> None: ...

    def search(
        self, vector: tuple[float, ...], *, snapshot_id: str, limit: int
    ) -> tuple[FigureHit, ...]: ...
