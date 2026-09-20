"""Source-proven formula tokens: every token quotes a span range; every structure quotes a path."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Literal

from enterprise_pdf_rag.documents.models import AssetRef, Bounds
from enterprise_pdf_rag.figures.models import SourceAnchor


class TokenRole(StrEnum):
    OPERAND = "operand"
    NUMBER = "number"
    OPERATOR = "operator"
    RELATION = "relation"
    GREEK = "greek"
    UNIT = "unit"
    BRACKET = "bracket"
    RADICAL = "radical"


class ScriptPosition(StrEnum):
    BASE = "base"
    SUPERSCRIPT = "superscript"
    SUBSCRIPT = "subscript"


class StructureKind(StrEnum):
    FRACTION = "fraction"
    SQRT = "sqrt"


type ScriptProof = Literal["text_rise", "derived"]
type ProofLevel = Literal["full", "literal"]
Matrix = tuple[float, float, float, float, float, float]
Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class ScriptEvidence:
    """The three source numbers a script decision is made from; superscript_flag is recorded only."""

    rise: float | None
    size_ratio: float
    baseline_offset: float
    superscript_flag: bool


@dataclass(frozen=True, slots=True)
class FormulaToken:
    index: int
    text: str
    source_span_id: str
    char_start: int
    char_end: int
    bbox: Bounds
    role: TokenRole
    script: ScriptPosition = ScriptPosition.BASE
    script_proof: ScriptProof | None = None
    base_token_index: int | None = None
    script_evidence: ScriptEvidence | None = None

    def __post_init__(self) -> None:
        if not self.text or self.text != self.text.strip() or not self.source_span_id:
            raise ValueError("Formula tokens quote a non-empty, unpadded span substring")
        if not 0 <= self.char_start < self.char_end or self.char_end - self.char_start != len(
            self.text
        ):
            raise ValueError("Formula token offsets must cover exactly its text")
        x0, y0, x1, y1 = self.bbox
        if not all(isfinite(value) for value in self.bbox) or x0 >= x1 or y0 >= y1:
            raise ValueError("Formula token bbox must be finite with positive area")
        is_base = self.script is ScriptPosition.BASE
        if is_base != (self.script_proof is None) or is_base != (self.base_token_index is None):
            raise ValueError("Script tokens carry a proof and a base; base tokens carry neither")
        if not is_base and (self.script_evidence is None or self.base_token_index == self.index):
            raise ValueError("Script tokens carry their evidence and a distinct base")


@dataclass(frozen=True, slots=True)
class PathEvidence:
    path_index: int
    kind: Literal["line", "rect", "polyline"]
    points: tuple[Point, ...]
    width: float

    def __post_init__(self) -> None:
        if (
            self.path_index < 0
            or len(self.points) < 2
            or not all(isfinite(value) for point in self.points for value in point)
        ):
            raise ValueError("Path evidence needs a nonnegative index and finite points")


@dataclass(frozen=True, slots=True)
class FormulaStructure:
    kind: StructureKind
    path: PathEvidence
    first: tuple[int, ...]
    second: tuple[int, ...] = ()
    radical_token_index: int | None = None

    def __post_init__(self) -> None:
        if not self.first or (self.kind is StructureKind.FRACTION) != bool(self.second):
            raise ValueError("A fraction has both sides; a sqrt has only a radicand")
        members = (*self.first, *self.second)
        if len(set(members)) != len(members) or any(index < 0 for index in members):
            raise ValueError("Structure members are unique token indices")


@dataclass(frozen=True, slots=True)
class ObservedChar:
    text: str
    bbox: Bounds


@dataclass(frozen=True, slots=True)
class ObservedRun:
    """One pdfspine span with exactly the fields the proof reads; ids follow pdfspine_document."""

    span_id: str
    text: str
    bbox: Bounds
    origin: Point
    size: float
    font: str
    direction: Point
    ctm: Matrix
    text_matrix: Matrix
    flags: int
    chars: tuple[ObservedChar, ...]


@dataclass(frozen=True, slots=True)
class ObservedPath:
    path_index: int
    paint: str
    width: float
    closed: bool
    items: tuple[tuple[str, tuple[Point, ...]], ...]


@dataclass(frozen=True, slots=True)
class FormulaSourceObservation:
    schema_version: str
    sdk: str
    source_sha256: str
    page_index: int
    page_height: float
    bbox: Bounds
    runs: tuple[ObservedRun, ...]
    paths: tuple[ObservedPath, ...]


@dataclass(frozen=True, slots=True)
class FormulaQualification:
    """Qualifies exact token transcription and path-backed structure only; never a financial relation."""

    object_id: str
    source: SourceAnchor
    source_manifest_id: str
    source_span_ids: tuple[str, ...]
    ir: AssetRef
    description: AssetRef
    source_svg: AssetRef
    observation: AssetRef
    proof_level: ProofLevel
    token_count: int
    structure_count: int
    derived_script_token_indices: tuple[int, ...]
    model_literal_agreement: Literal["agrees", "disagrees", "unavailable"]
    lineage: tuple[AssetRef, ...] = ()
    scope: str = "formula-source-tokens-v1"
    method: str = "exact-span-tiling+path-geometry+text-rise-v1"

    def __post_init__(self) -> None:
        if (self.proof_level == "full") != (not self.derived_script_token_indices):
            raise ValueError("Full proof means no derived script; literal proof means at least one")
        if self.token_count < 1 or self.structure_count < 0 or not self.source_span_ids:
            raise ValueError("A qualified formula has tokens and source occurrences")
