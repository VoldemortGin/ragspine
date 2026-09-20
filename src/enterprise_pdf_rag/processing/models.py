"""Immutable processing records separate source observations from inference."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from enterprise_pdf_rag.documents.models import AssetRef, Bounds, TextSidecar
from enterprise_pdf_rag.figures.models import Confidence, SourceAnchor, Verification


class ObjectKind(StrEnum):
    TEXT = "Text"
    LIST = "List"
    TABLE = "Table"
    CHART = "Chart"
    DIAGRAM = "Diagram"
    IMAGE = "Image"
    FORMULA = "Formula"
    GROUP = "Group"


class StageState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class ProcessingScope:
    source_manifest_id: str
    source_sha256: str
    source_page_count: int
    selected_page_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        for digest in (self.source_manifest_id, self.source_sha256):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Processing source identities require SHA-256 digests")
        selected = self.selected_page_indices
        if len(set(selected)) != len(selected) or tuple(sorted(selected)) != selected:
            raise ValueError("Selected pages must be unique and ordered")
        if (
            self.source_page_count <= 0
            or not selected
            or any(not 0 <= index < self.source_page_count for index in selected)
        ):
            raise ValueError("Processing selected pages must exist in the source")

    @property
    def physical_pages(self) -> tuple[int, ...]:
        return tuple(index + 1 for index in self.selected_page_indices)


@dataclass(frozen=True, slots=True)
class PageInput:
    source_manifest_id: str
    source_sha256: str
    page_index: int
    width: float
    height: float
    native_svg: AssetRef
    text: TextSidecar

    def __post_init__(self) -> None:
        if (
            self.text.source_sha256 != self.source_sha256
            or self.text.page_index != self.page_index
            or self.page_index < 0
        ):
            raise ValueError("Page input is outside the selected source or scope")
        if not all(isfinite(value) and value > 0 for value in (self.width, self.height)):
            raise ValueError("Page dimensions must be finite and positive")
        ids = tuple(span.span_id for span in self.text.spans)
        if len(set(ids)) != len(ids):
            raise ValueError("Source text occurrence IDs must be unique")


@dataclass(frozen=True, slots=True)
class CanonicalText:
    element_id: str
    source_span_id: str
    source: SourceAnchor
    text: str


@dataclass(frozen=True, slots=True)
class CanonicalPage:
    schema_version: str
    source_manifest_id: str
    source_sha256: str
    page_index: int
    page_element_id: str
    native_svg: AssetRef
    text: tuple[CanonicalText, ...]


@dataclass(frozen=True, slots=True)
class LayoutObject:
    object_id: str
    kind: ObjectKind
    bbox: Bounds
    source_span_ids: tuple[str, ...]
    interpretation: str
    confidence: Confidence
    verification: Verification = Verification.PENDING
    parent_id: str | None = None
    context_span_ids: tuple[str, ...] = ()
    list_item_span_ids: tuple[tuple[str, ...], ...] = ()
    list_ordered: bool | None = None
    child_object_ids: tuple[str, ...] = ()
    model_bbox: Bounds | None = None
    model_kind: ObjectKind | None = None
    normalization: tuple[str, ...] = ()
    extraction_region_id: str | None = None


@dataclass(frozen=True, slots=True)
class PagePartition:
    schema_version: str
    source_manifest_id: str
    source_sha256: str
    page_index: int
    producer: str
    objects: tuple[LayoutObject, ...]
    unassigned_span_ids: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()
    model_request_fingerprint: str | None = None
    model_output_digest: str | None = None
    raw_model_json: str | None = None


@dataclass(frozen=True, slots=True)
class StageOutcome:
    stage: str
    input_fingerprint: str
    state: StageState
    producer: str
    artifact: AssetRef | None = None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if not self.stage or not self.input_fingerprint or not self.producer:
            raise ValueError("Stage identity and producer are required")
        if self.state is StageState.SUCCEEDED:
            if self.artifact is None or self.diagnostic is not None:
                raise ValueError("Succeeded stages require an actual artifact")
        elif not self.diagnostic or not self.diagnostic.strip():
            raise ValueError("Unfinished stages require a concrete diagnostic")


@dataclass(frozen=True, slots=True)
class ObjectProcessingRecord:
    object_id: str
    kind: ObjectKind
    stages: tuple[StageOutcome, ...]
    qualified_claim_count: int = 0


@dataclass(frozen=True, slots=True)
class PageProcessingRecord:
    page_index: int
    canonical: StageOutcome
    partition: StageOutcome
    objects: tuple[ObjectProcessingRecord, ...]
    raw_partition: StageOutcome | None = None


@dataclass(frozen=True, slots=True)
class RetrievalPublication:
    snapshot_id: str
    plan: AssetRef
    index: AssetRef
    dependencies: tuple[AssetRef, ...]


@dataclass(frozen=True, slots=True)
class ProcessingManifest:
    schema_version: str
    scope: ProcessingScope
    producer: str
    pages: tuple[PageProcessingRecord, ...]
    retrieval: RetrievalPublication | None = None

    def __post_init__(self) -> None:
        if tuple(page.page_index for page in self.pages) != self.scope.selected_page_indices:
            raise ValueError("Processing manifest must cover every selected page exactly")
