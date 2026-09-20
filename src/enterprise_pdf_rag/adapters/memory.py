"""Process-local demo storage; this is not a durable release catalog."""

from dataclasses import replace
from math import sqrt

from enterprise_pdf_rag.figures.models import (
    ChartIR,
    DescriptionEmbedding,
    DescriptionIndexRecord,
    FigureBundle,
    FigureHit,
    SvgArtifact,
    TextDescription,
)


class MemoryFigureRepository:
    def __init__(self) -> None:
        self._bundles: dict[tuple[str, str], FigureBundle] = {}
        self._svgs: dict[str, SvgArtifact] = {}
        self._charts: dict[str, ChartIR] = {}
        self._descriptions: dict[str, TextDescription] = {}
        self._embeddings: dict[str, DescriptionEmbedding] = {}

    def save(
        self,
        bundle: FigureBundle,
        svg: SvgArtifact,
        chart: ChartIR,
        description: TextDescription,
    ) -> None:
        self._svgs[svg.artifact_id] = svg
        self._charts[chart.artifact_id] = chart
        self._descriptions[description.artifact_id] = description
        self._bundles[bundle.snapshot_id, bundle.bundle_id] = bundle

    def get_bundle(self, snapshot_id: str, bundle_id: str) -> FigureBundle | None:
        return self._bundles.get((snapshot_id, bundle_id))

    def get_svg(self, artifact_id: str) -> SvgArtifact | None:
        return self._svgs.get(artifact_id)

    def get_chart(self, artifact_id: str) -> ChartIR | None:
        return self._charts.get(artifact_id)

    def get_description(self, artifact_id: str) -> TextDescription | None:
        return self._descriptions.get(artifact_id)

    def get_embedding(self, key: str) -> DescriptionEmbedding | None:
        return self._embeddings.get(key)

    def save_embedding(self, embedding: DescriptionEmbedding) -> None:
        self._embeddings[embedding.key] = embedding


class MemoryDescriptionIndex:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], DescriptionIndexRecord] = {}

    def add(self, record: DescriptionIndexRecord) -> None:
        self._records[record.hit.snapshot_id, record.hit.bundle_id] = record

    def search(
        self, vector: tuple[float, ...], *, snapshot_id: str, limit: int
    ) -> tuple[FigureHit, ...]:
        hits: list[FigureHit] = []
        for (snapshot, _), record in self._records.items():
            if snapshot != snapshot_id:
                continue
            indexed = record.embedding.vector
            if len(indexed) != len(vector):
                raise ValueError("Embedding dimensions differ")
            divisor = sqrt(
                sum(value * value for value in vector) * sum(value * value for value in indexed)
            )
            score = (
                sum(left * right for left, right in zip(vector, indexed, strict=True)) / divisor
                if divisor
                else 0.0
            )
            hits.append(replace(record.hit, score=score))
        return tuple(sorted(hits, key=lambda hit: (-hit.score, hit.bundle_id))[:limit])
