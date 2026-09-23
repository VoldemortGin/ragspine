"""Typed object payloads; inferred structure never overwrites source observations."""

from dataclasses import dataclass

from ragspine.extraction.evidence.document.models import AssetRef, Bounds
from ragspine.extraction.evidence.figures.models import (
    ChartIR,
    Confidence,
    SourceAnchor,
    Verification,
)
from ragspine.extraction.evidence.objects.formulas.formula_models import (
    FormulaStructure,
    FormulaToken,
    ProofLevel,
    ScriptPosition,
)
from ragspine.extraction.evidence.objects.tables.table_models import TableIR
from ragspine.extraction.evidence.page.models import ObjectKind


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
    # Filled by the model-free qualification branch; the model branch keeps the defaults.
    tokens: tuple[FormulaToken, ...] = ()
    structures: tuple[FormulaStructure, ...] = ()
    linear: str | None = None
    readable: str | None = None
    proof_level: ProofLevel | None = None

    def __post_init__(self) -> None:
        qualified = bool(self.tokens)
        if qualified != (self.linear is not None) or qualified != (self.readable is not None):
            raise ValueError(
                "Token IR carries its linear and readable forms; model IR carries neither"
            )
        if qualified != (self.proof_level is not None):
            raise ValueError("Proof level accompanies tokens only")
        if self.verification is Verification.VERIFIED and self.proof_level != "full":
            raise ValueError("Only a fully proven formula is verified")
        if tuple(token.index for token in self.tokens) != tuple(range(len(self.tokens))):
            raise ValueError("Token indices are dense and ordered")
        indices = {token.index for token in self.tokens}
        for token in self.tokens:
            if token.base_token_index is not None and (
                token.base_token_index not in indices
                or self.tokens[token.base_token_index].script is not ScriptPosition.BASE
            ):
                raise ValueError("A script token attaches to an existing base token")
        for structure in self.structures:
            if not set((*structure.first, *structure.second)) <= indices:
                raise ValueError("Structure members are existing tokens")


type TypedIR = TextIR | ListIR | TableIR | ChartIR | DiagramIR | ImageIR | FormulaIR | GroupIR


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
    # ADR 0014: set only when the IR's grid re-proves from the page's own rulings.
    grid_scope: str | None = None
    ruling_digest: str | None = None
