"""Immutable values shared by SVG branches, pairing and snapshot resolution."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from math import isfinite


def content_id(kind: str, values: tuple[object, ...]) -> str:
    """Identify versioned, immutable domain content without time or randomness."""
    return f"{kind}:{sha256(repr(values).encode('utf-8')).hexdigest()}"


class Verification(StrEnum):
    VERIFIED = "verified"
    PENDING = "pending"
    REJECTED = "rejected"


class ExecutionMode(StrEnum):
    OFFLINE_DEMO = "offline-demo"
    PRODUCTION = "production"


class ValueKind(StrEnum):
    EXPLICIT = "explicit"
    DERIVED = "derived"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class EvidenceKind(StrEnum):
    NATIVE_ELEMENT = "native_element"
    SOURCE_TEXT_OBSERVATION = "source_text_observation"


class FailureCode(StrEnum):
    MISSING_CHART = "missing_chart"
    MISSING_ARTIFACT = "missing_artifact"
    BINDING_MISMATCH = "binding_mismatch"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    INVALID_EVIDENCE = "invalid_evidence"
    UNVERIFIED = "unverified"
    CONTENT_MISMATCH = "content_mismatch"
    UNSUPPORTED_VALUE = "unsupported_value"
    INVALID_INPUT = "invalid_input"
    EXECUTION_MODE = "execution_mode"


class FigureError(ValueError):
    """A located, fail-closed result; callers must not fall back to summaries."""

    def __init__(self, code: FailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Confidence:
    score: Decimal | None
    method: str

    def __post_init__(self) -> None:
        if not self.method.strip():
            raise ValueError("confidence requires its method or unknown explanation")
        if self.score is not None and (
            not self.score.is_finite() or not Decimal(0) <= self.score <= Decimal(1)
        ):
            raise ValueError("confidence must be unknown or finite within [0, 1]")


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    source_revision: str
    document_sha256: str
    page_index: int
    bbox: tuple[float, float, float, float]
    coordinate_frame: str = "page-top-left"
    rotation: int = 0
    transform: tuple[float, float, float, float, float, float] = (
        1.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
    )

    def __post_init__(self) -> None:
        if not self.source_revision or not self.coordinate_frame:
            raise ValueError("source revision and coordinate frame are required")
        if len(self.document_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.document_sha256
        ):
            raise ValueError("document_sha256 must be a lowercase SHA-256 digest")
        if self.page_index < 0 or self.rotation not in (0, 90, 180, 270):
            raise ValueError("invalid physical page or rotation")
        if not isinstance(self.bbox, tuple) or len(self.bbox) != 4:
            raise ValueError("bbox must be an immutable four-tuple")
        if not isinstance(self.transform, tuple) or len(self.transform) != 6:
            raise ValueError("transform must be an immutable six-tuple")
        if not all(isfinite(value) for value in self.bbox + self.transform):
            raise ValueError("source coordinates must be finite")
        if self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]:
            raise ValueError("bbox must have positive area")


@dataclass(frozen=True, slots=True)
class SvgElement:
    element_id: str
    text: str
    anchor: SourceAnchor
    evidence_kind: EvidenceKind = EvidenceKind.NATIVE_ELEMENT
    source_span_id: str | None = None
    text_range: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.evidence_kind is EvidenceKind.SOURCE_TEXT_OBSERVATION:
            if (
                not self.source_span_id
                or not isinstance(self.text_range, tuple)
                or len(self.text_range) != 2
                or not 0 <= self.text_range[0] < self.text_range[1]
                or self.text_range[1] - self.text_range[0] != len(self.text)
            ):
                raise ValueError(
                    "Source text observations require Unicode [start,end) offsets"
                )
        elif self.source_span_id is not None or self.text_range is not None:
            raise ValueError(
                "Native SVG elements must not imply an unverified text-span mapping"
            )


@dataclass(frozen=True, slots=True)
class SvgBinding:
    figure_id: str
    source_revision: str
    svg_artifact_id: str
    svg_digest: str


@dataclass(frozen=True, slots=True)
class SvgArtifact:
    figure_id: str
    source: SourceAnchor
    svg: str
    elements: tuple[SvgElement, ...]
    verification: Verification = Verification.PENDING
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.elements, tuple) or not isinstance(self.warnings, tuple):
            raise ValueError("SVG members must be immutable tuples")

    @property
    def digest(self) -> str:
        return sha256(self.svg.encode("utf-8")).hexdigest()

    @property
    def artifact_id(self) -> str:
        return content_id("svg-v2", (self,))

    @property
    def binding(self) -> SvgBinding:
        return SvgBinding(
            self.figure_id, self.source.source_revision, self.artifact_id, self.digest
        )


@dataclass(frozen=True, slots=True)
class FieldOccurrence:
    field_path: str
    element_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.field_path or not isinstance(self.element_ids, tuple):
            raise ValueError("field occurrences require a path and immutable IDs")
        if not self.element_ids or any(not value for value in self.element_ids):
            raise ValueError("field occurrences require nonempty element IDs")
        if len(set(self.element_ids)) != len(self.element_ids):
            raise ValueError("field occurrence element IDs must be unique")


@dataclass(frozen=True, slots=True)
class FigureQualification:
    """A scoped receipt obtained independently of the two model producers."""

    binding: SvgBinding
    source: SourceAnchor
    fields: tuple[FieldOccurrence, ...]
    method: str
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE_DEMO
    source_geometry_refs: tuple[str, ...] = ()
    semantic_scope: str = "explicit-labels"

    def __post_init__(self) -> None:
        if not isinstance(self.fields, tuple) or not self.fields:
            raise ValueError("qualification requires immutable field occurrences")
        paths = tuple(field.field_path for field in self.fields)
        if len(set(paths)) != len(paths) or not self.method.strip():
            raise ValueError("qualification requires unique fields and its method")
        if not isinstance(self.source_geometry_refs, tuple):
            raise ValueError("Geometry references must be immutable")

    @property
    def artifact_id(self) -> str:
        return content_id("qualification-v1", (self,))


@dataclass(frozen=True, slots=True)
class Evidence:
    element_ids: tuple[str, ...]
    verification: Verification
    confidence: Confidence

    def __post_init__(self) -> None:
        if not isinstance(self.element_ids, tuple) or not self.element_ids:
            raise ValueError("evidence requires immutable SVG element references")


@dataclass(frozen=True, slots=True)
class TextField:
    text: str
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class NumericObservation:
    value: Decimal | None
    kind: ValueKind
    evidence: Evidence

    def __post_init__(self) -> None:
        if self.value is not None and not self.value.is_finite():
            raise ValueError("numeric values must be finite Decimal values")
        if (self.kind is ValueKind.UNAVAILABLE) != (self.value is None):
            raise ValueError("only unavailable observations have no numeric value")


@dataclass(frozen=True, slots=True)
class ChartPoint:
    point_id: str
    series: TextField
    category: TextField
    unit: TextField
    value: NumericObservation


@dataclass(frozen=True, slots=True)
class ChartAxis:
    axis_id: str
    label: TextField
    unit: TextField
    scale: str


@dataclass(frozen=True, slots=True)
class ChartMark:
    """A located grammar hypothesis, not an independently validated relation."""

    mark_id: str
    kind: str
    bbox: tuple[float, float, float, float]
    color: str | None
    point_ids: tuple[str, ...]
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class ChartIR:
    binding: SvgBinding
    grammar: str
    axes: tuple[ChartAxis, ...]
    points: tuple[ChartPoint, ...]
    producer: str
    verification: Verification
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE_DEMO
    title: TextField | None = None
    period: TextField | None = None
    marks: tuple[ChartMark, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.axes, tuple)
            or not isinstance(self.points, tuple)
            or not isinstance(self.marks, tuple)
        ):
            raise ValueError("chart members must be immutable tuples")

    @property
    def artifact_id(self) -> str:
        return content_id("chart-v2", (self,))


@dataclass(frozen=True, slots=True)
class DescriptionClaim:
    text: str
    evidence: Evidence
    series: str | None = None
    category: str | None = None
    unit: str | None = None
    value: Decimal | None = None
    period: str | None = None


@dataclass(frozen=True, slots=True)
class TextDescription:
    binding: SvgBinding
    claims: tuple[DescriptionClaim, ...]
    producer: str
    verification: Verification
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE_DEMO

    def __post_init__(self) -> None:
        if not isinstance(self.claims, tuple) or not self.claims:
            raise ValueError("description requires immutable claim slices")

    @property
    def text(self) -> str:
        return " ".join(claim.text for claim in self.claims)

    @property
    def digest(self) -> str:
        return sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def artifact_id(self) -> str:
        return content_id("description-v2", (self,))


@dataclass(frozen=True, slots=True)
class QualifiedFigurePair:
    chart: ChartIR
    description: TextDescription
    receipt: FigureQualification
    raw_chart_id: str
    raw_description_id: str
    excluded_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DescriptionEmbedding:
    key: str
    vector: tuple[float, ...]
    provider_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.vector, tuple) or not self.vector:
            raise ValueError("embedding must be an immutable nonempty vector")
        if not all(isfinite(value) for value in self.vector):
            raise ValueError("embedding values must be finite")


@dataclass(frozen=True, slots=True)
class FigureBundle:
    snapshot_id: str
    figure_id: str
    source_revision: str
    svg_artifact_id: str
    svg_digest: str
    chart_ir_artifact_id: str
    description_id: str
    embedding_key: str
    qualification_id: str | None = None

    @property
    def bundle_id(self) -> str:
        return content_id("bundle-v2", (self,))


@dataclass(frozen=True, slots=True)
class FigureHit:
    snapshot_id: str
    bundle_id: str
    description_id: str
    figure_id: str
    source_revision: str
    svg_artifact_id: str
    svg_digest: str
    chart_ir_artifact_id: str
    text: str
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class DescriptionIndexRecord:
    hit: FigureHit
    embedding: DescriptionEmbedding


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    field_path: str
    elements: tuple[SvgElement, ...]
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class ReasoningView:
    snapshot_id: str
    bundle_id: str
    chart_ir: ChartIR
    svg: SvgArtifact
    evidence: tuple[FieldEvidence, ...]
