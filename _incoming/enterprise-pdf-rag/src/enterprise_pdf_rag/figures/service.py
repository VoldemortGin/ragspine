"""A small figure use-case facade with explicit branch and storage dependencies."""

from enterprise_pdf_rag.figures.models import (
    ChartIR,
    DescriptionEmbedding,
    DescriptionIndexRecord,
    Evidence,
    ExecutionMode,
    FailureCode,
    FieldEvidence,
    FigureBundle,
    FigureError,
    FigureHit,
    FigureQualification,
    QualifiedFigurePair,
    ReasoningView,
    SvgArtifact,
    TextDescription,
    ValueKind,
    Verification,
    content_id,
)
from enterprise_pdf_rag.figures.ports import (
    ChartExtractor,
    DescriptionGenerator,
    DescriptionIndex,
    EmbeddingPort,
    FigureQualificationProvider,
    FigureRepository,
    ScopedFigureQualificationProvider,
)
from enterprise_pdf_rag.figures.validation import validate_pair, validate_svg


class FiguresService:
    """Build complete figure bundles and resolve only their pinned dependencies."""

    def __init__(
        self,
        extractor: ChartExtractor,
        describer: DescriptionGenerator,
        embedder: EmbeddingPort,
        repository: FigureRepository,
        index: DescriptionIndex,
        *,
        qualifier: FigureQualificationProvider | ScopedFigureQualificationProvider,
        mode: ExecutionMode,
    ) -> None:
        self.extractor = extractor
        self.describer = describer
        self.embedder = embedder
        self.repository = repository
        self.index = index
        self.qualifier = qualifier
        self.mode = mode

    def build(self, svg: SvgArtifact, *, snapshot_id: str) -> FigureBundle:
        if self.mode is ExecutionMode.OFFLINE_DEMO:
            self._check_svg_qualification(svg)
        elif not isinstance(self.qualifier, ScopedFigureQualificationProvider):
            raise FigureError(
                FailureCode.EXECUTION_MODE,
                "Production requires an independent scoped qualifier",
            )
        else:
            validate_svg(svg)
        chart = self.extractor.extract(svg)
        description = self.describer.generate(svg)
        if chart is None:
            raise FigureError(
                FailureCode.MISSING_CHART, "ChartIR branch produced no artifact"
            )
        return self.pair(svg, chart, description, snapshot_id=snapshot_id)

    def pair(
        self,
        svg: SvgArtifact,
        chart: ChartIR,
        description: TextDescription,
        *,
        snapshot_id: str,
    ) -> FigureBundle:
        qualified = self.qualify_pair(svg, chart, description)
        chart, description = qualified.chart, qualified.description
        if chart.binding != svg.binding or description.binding != svg.binding:
            raise FigureError(
                FailureCode.BINDING_MISMATCH,
                "Both branches must bind exactly the same SVG and source revision",
            )
        validate_pair(svg, chart, description)
        key = content_id(
            "embedding-v1", (description.digest, self.embedder.fingerprint)
        )
        embedding = self.repository.get_embedding(key)
        if embedding is None:
            embedding = DescriptionEmbedding(
                key,
                self.embedder.embed_description(description.text),
                self.embedder.fingerprint,
            )
            self.repository.save_embedding(embedding)
        bundle = FigureBundle(
            snapshot_id,
            svg.figure_id,
            svg.source.source_revision,
            svg.artifact_id,
            svg.digest,
            chart.artifact_id,
            description.artifact_id,
            key,
            qualified.receipt.artifact_id
            if self.mode is ExecutionMode.PRODUCTION
            else None,
        )
        self.repository.save(bundle, svg, chart, description)
        hit = FigureHit(
            snapshot_id,
            bundle.bundle_id,
            description.artifact_id,
            svg.figure_id,
            svg.source.source_revision,
            svg.artifact_id,
            svg.digest,
            chart.artifact_id,
            description.text,
        )
        self.index.add(DescriptionIndexRecord(hit, embedding))
        return bundle

    def qualify_pair(
        self, svg: SvgArtifact, chart: ChartIR, description: TextDescription
    ) -> QualifiedFigurePair:
        """Qualify immutable projections without embedding or repository writes."""
        if chart.binding != svg.binding or description.binding != svg.binding:
            raise FigureError(
                FailureCode.BINDING_MISMATCH,
                "Both branches must bind exactly the same source SVG",
            )
        if (
            chart.execution_mode is not self.mode
            or description.execution_mode is not self.mode
        ):
            raise FigureError(
                FailureCode.EXECUTION_MODE,
                "Artifact mode differs from the requested qualification mode",
            )
        if any(
            status is Verification.REJECTED
            for status in (
                svg.verification,
                chart.verification,
                description.verification,
                *(evidence.verification for _, evidence in _chart_evidence(chart)),
                *(claim.evidence.verification for claim in description.claims),
            )
        ):
            raise FigureError(
                FailureCode.UNVERIFIED,
                "Rejected fields or claims cannot be promoted by another stage",
            )
        if self.mode is ExecutionMode.PRODUCTION:
            if not isinstance(self.qualifier, ScopedFigureQualificationProvider):
                raise FigureError(
                    FailureCode.EXECUTION_MODE,
                    "Production requires an independent scoped source qualifier",
                )
            validate_svg(svg)
            qualified = self.qualifier.qualify_pair(svg, chart, description)
            if (
                qualified.raw_chart_id != chart.artifact_id
                or qualified.raw_description_id != description.artifact_id
                or not qualified.receipt.source_geometry_refs
            ):
                raise FigureError(
                    FailureCode.INVALID_EVIDENCE,
                    "Qualified projection lineage or geometry receipt is incomplete",
                )
        else:
            qualified = QualifiedFigurePair(
                chart,
                description,
                self._check_svg_qualification(svg),
                chart.artifact_id,
                description.artifact_id,
            )
        if (
            qualified.chart.binding != svg.binding
            or qualified.description.binding != svg.binding
        ):
            raise FigureError(
                FailureCode.BINDING_MISMATCH,
                "Qualified projections changed source bindings",
            )
        self._check_qualification(
            svg, qualified.chart, qualified.description, qualified.receipt
        )
        validate_pair(svg, qualified.chart, qualified.description)
        return qualified

    def _check_svg_qualification(self, svg: SvgArtifact) -> FigureQualification:
        if self.mode is ExecutionMode.PRODUCTION or not isinstance(
            self.qualifier, FigureQualificationProvider
        ):
            raise FigureError(
                FailureCode.EXECUTION_MODE,
                "This source qualification contract is limited to offline fixtures",
            )
        if svg.verification is not Verification.VERIFIED:
            raise FigureError(
                FailureCode.UNVERIFIED, "SVG source completeness has not been verified"
            )
        validate_svg(svg)
        qualification = self.qualifier.qualification_for(svg)
        if (
            qualification is None
            or qualification.binding != svg.binding
            or qualification.source != svg.source
        ):
            raise FigureError(
                FailureCode.UNVERIFIED,
                "No independent qualification for this exact SVG and PDF source",
            )
        return qualification

    def _check_qualification(
        self,
        svg: SvgArtifact,
        chart: ChartIR,
        description: TextDescription,
        qualification: FigureQualification,
    ) -> None:
        if (
            qualification.binding != svg.binding
            or qualification.source != svg.source
            or qualification.execution_mode is not self.mode
        ):
            raise FigureError(
                FailureCode.INVALID_EVIDENCE,
                "Receipt is not scoped to the exact source and execution mode",
            )
        if (
            chart.execution_mode is not self.mode
            or description.execution_mode is not self.mode
        ):
            raise FigureError(
                FailureCode.EXECUTION_MODE,
                "Artifact execution mode does not match this service",
            )
        statuses = [chart.verification, description.verification]
        statuses.extend(evidence.verification for _, evidence in _chart_evidence(chart))
        statuses.extend(claim.evidence.verification for claim in description.claims)
        if any(status is not Verification.VERIFIED for status in statuses):
            raise FigureError(
                FailureCode.UNVERIFIED,
                "All fields and claims require scoped verification; confidence is not approval",
            )
        actual_fields = _chart_evidence(chart)
        expected = {
            field.field_path: field.element_ids for field in qualification.fields
        }
        if (
            len(actual_fields) != len(expected)
            or len({path for path, _ in actual_fields}) != len(actual_fields)
            or any(
                expected.get(path) != evidence.element_ids
                for path, evidence in actual_fields
            )
        ):
            raise FigureError(
                FailureCode.INVALID_EVIDENCE,
                "Chart field evidence differs from independently qualified source occurrences",
            )
        allowed_ids = {element_id for ids in expected.values() for element_id in ids}
        if any(
            not set(claim.evidence.element_ids).issubset(allowed_ids)
            for claim in description.claims
        ):
            raise FigureError(
                FailureCode.INVALID_EVIDENCE,
                "Description cites an occurrence outside the independently qualified fields",
            )

    def search(
        self, query: str, *, snapshot_id: str, limit: int = 5
    ) -> tuple[FigureHit, ...]:
        return self.index.search(
            self.embedder.embed_query(query), snapshot_id=snapshot_id, limit=limit
        )

    def resolve(self, hit: FigureHit, *, snapshot_id: str) -> ReasoningView:
        if hit.snapshot_id != snapshot_id:
            raise FigureError(
                FailureCode.SNAPSHOT_MISMATCH, "Hit belongs to another pinned snapshot"
            )
        bundle = self.repository.get_bundle(snapshot_id, hit.bundle_id)
        if bundle is None:
            raise FigureError(
                FailureCode.MISSING_ARTIFACT, "No figure bundle in this snapshot"
            )
        svg = self.repository.get_svg(bundle.svg_artifact_id)
        chart = self.repository.get_chart(bundle.chart_ir_artifact_id)
        description = self.repository.get_description(bundle.description_id)
        if svg is None or chart is None or description is None:
            raise FigureError(
                FailureCode.MISSING_ARTIFACT,
                "Figure dependencies are unavailable; no summary fallback",
            )
        expected = FigureHit(
            bundle.snapshot_id,
            bundle.bundle_id,
            bundle.description_id,
            bundle.figure_id,
            bundle.source_revision,
            bundle.svg_artifact_id,
            bundle.svg_digest,
            bundle.chart_ir_artifact_id,
            description.text,
            hit.score,
        )
        if hit != expected or bundle.snapshot_id != snapshot_id:
            raise FigureError(
                FailureCode.BINDING_MISMATCH,
                "Hit does not match the immutable bundle membership",
            )
        if (
            svg.figure_id,
            svg.source.source_revision,
            svg.artifact_id,
            svg.digest,
            chart.artifact_id,
            description.artifact_id,
        ) != (
            bundle.figure_id,
            bundle.source_revision,
            bundle.svg_artifact_id,
            bundle.svg_digest,
            bundle.chart_ir_artifact_id,
            bundle.description_id,
        ):
            raise FigureError(
                FailureCode.BINDING_MISMATCH,
                "Stored artifact content does not match its immutable reference",
            )
        if chart.binding != svg.binding or description.binding != svg.binding:
            raise FigureError(
                FailureCode.BINDING_MISMATCH,
                "Resolved dependencies use different SVG bindings",
            )
        qualified = self.qualify_pair(svg, chart, description)
        if (
            qualified.chart != chart
            or qualified.description != description
            or (
                self.mode is ExecutionMode.PRODUCTION
                and bundle.qualification_id != qualified.receipt.artifact_id
            )
        ):
            raise FigureError(
                FailureCode.INVALID_EVIDENCE,
                "Stored qualified projection or receipt changed after snapshot publication",
            )
        validate_pair(svg, chart, description)
        if (
            chart.grammar == "unknown"
            or not chart.points
            or any(point.value.kind is not ValueKind.EXPLICIT for point in chart.points)
        ):
            raise FigureError(
                FailureCode.UNSUPPORTED_VALUE,
                "Exact context requires verified explicit values; estimates, missing data and unimplemented derivations cannot substitute",
            )
        elements = {element.element_id: element for element in svg.elements}
        references = tuple(
            FieldEvidence(
                path,
                tuple(elements[element_id] for element_id in evidence.element_ids),
                evidence.confidence,
            )
            for path, evidence in _chart_evidence(chart)
        )
        return ReasoningView(snapshot_id, bundle.bundle_id, chart, svg, references)


def _chart_evidence(chart: ChartIR) -> tuple[tuple[str, Evidence], ...]:
    items: list[tuple[str, Evidence]] = []
    for name, field in (("title", chart.title), ("period", chart.period)):
        if field is not None:
            items.append((name, field.evidence))
    items.extend((f"marks.{mark.mark_id}", mark.evidence) for mark in chart.marks)
    for axis in chart.axes:
        items.extend(
            (
                (f"axes.{axis.axis_id}.label", axis.label.evidence),
                (f"axes.{axis.axis_id}.unit", axis.unit.evidence),
            )
        )
    for point in chart.points:
        items.extend(
            (f"points.{point.point_id}.{name}", field.evidence)
            for name, field in (
                ("series", point.series),
                ("category", point.category),
                ("unit", point.unit),
                ("value", point.value),
            )
        )
    return tuple(items)
