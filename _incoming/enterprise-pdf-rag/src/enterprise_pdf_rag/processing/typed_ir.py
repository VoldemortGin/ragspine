"""Typed object payloads; inferred structure never overwrites source observations."""

from dataclasses import dataclass

from enterprise_pdf_rag.documents.models import AssetRef, Bounds
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    Confidence,
    SourceAnchor,
    Verification,
)
from enterprise_pdf_rag.processing.models import ObjectKind
from enterprise_pdf_rag.processing.table_models import TableIR


@dataclass(frozen=True, slots=True)
class ObservedText:
    source_span_id: str
    text: str
    source: SourceAnchor


@dataclass(frozen=True, slots=True)
class TextIR:
    object_id: str
    source: SourceAnchor
    fragments: tuple[ObservedText, ...]


@dataclass(frozen=True, slots=True)
class ListIR:
    object_id: str
    source: SourceAnchor
    fragments: tuple[ObservedText, ...]
    item_groups: tuple[tuple[str, ...], ...]
    ordered: bool | None
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GroupIR:
    object_id: str
    source: SourceAnchor
    child_object_ids: tuple[str, ...]
    fragments: tuple[ObservedText, ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiagramNode:
    node_id: str
    label: str
    bbox: Bounds
    source_span_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiagramEdge:
    source_node_id: str
    target_node_id: str
    label: str | None
    relationship: str
    verification: Verification = Verification.PENDING
    source_span_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiagramIR:
    object_id: str
    source: SourceAnchor
    nodes: tuple[DiagramNode, ...]
    edges: tuple[DiagramEdge, ...]
    diagnostics: tuple[str, ...]
    verification: Verification = Verification.PENDING


@dataclass(frozen=True, slots=True)
class ImageIR:
    object_id: str
    source: SourceAnchor
    visible_objects: tuple[str, ...]
    observed_labels: tuple[ObservedText, ...]
    diagnostics: tuple[str, ...]
    verification: Verification = Verification.PENDING


@dataclass(frozen=True, slots=True)
class FormulaIR:
    object_id: str
    source: SourceAnchor
    source_literal: str | None
    latex: str | None
    source_span_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]
    verification: Verification = Verification.PENDING


type TypedIR = (
    TextIR | ListIR | TableIR | ChartIR | DiagramIR | ImageIR | FormulaIR | GroupIR
)


@dataclass(frozen=True, slots=True)
class ObjectDescription:
    object_id: str
    source: SourceAnchor
    source_span_ids: tuple[str, ...]
    text: str
    producer: str
    confidence: Confidence
    verification: Verification


@dataclass(frozen=True, slots=True)
class SourceObjectResult:
    kind: ObjectKind
    source: SourceAnchor
    ir: TextIR | ListIR | GroupIR
    description: ObjectDescription
    classification_verification: Verification = Verification.PENDING


@dataclass(frozen=True, slots=True)
class LiteralQualification:
    """Qualifies exact transcription only, never chart or financial relations."""

    object_id: str
    source: SourceAnchor
    source_manifest_id: str
    source_span_ids: tuple[str, ...]
    ir: AssetRef
    description: AssetRef
    source_svg: AssetRef
    scope: str = "literal-source-transcription-v1"
