"""Immutable source observations; semantic qualification is deliberately pending."""

from dataclasses import dataclass
from enum import StrEnum

Bounds = tuple[float, float, float, float]


class SemanticStatus(StrEnum):
    PENDING = "pending"


class TextLayerStatus(StrEnum):
    """Whether a page's span sidecar carries the words the page paints."""

    OK = "ok"
    OUTLINED_TEXT = "outlined_text"
    GARBLED = "garbled"


@dataclass(frozen=True, slots=True)
class TextSpan:
    span_id: str
    text: str
    bbox: Bounds
    origin: tuple[float, float] = (0.0, 0.0)
    font: str = ""
    size: float = 0.0
    direction: tuple[float, float] = (1.0, 0.0)


@dataclass(frozen=True, slots=True)
class TextLayerDiagnostic:
    """Page-level text-layer counts; ``span_count`` includes garbled spans the sidecar withheld."""

    status: TextLayerStatus
    span_count: int
    char_count: int
    garbled_char_count: int
    garbled_span_count: int
    drawing_count: int

    @property
    def needs_ocr(self) -> bool:
        return self.status is not TextLayerStatus.OK


@dataclass(frozen=True, slots=True)
class PageExtraction:
    page_index: int
    width: float
    height: float
    rotation: int
    native_svg: str
    text_spans: tuple[TextSpan, ...]
    warnings: tuple[str, ...] = ()
    text_layer: TextLayerDiagnostic | None = None


@dataclass(frozen=True, slots=True)
class DocumentExtraction:
    pages: tuple[PageExtraction, ...]
    producer: str


@dataclass(frozen=True, slots=True)
class RegionExtraction:
    page_index: int
    width: float
    height: float
    rotation: int
    bbox: Bounds
    native_svg: str
    cropped_svg: str
    text_spans: tuple[TextSpan, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentSpec:
    filename: str
    sha256: str
    page_count: int
    focus_page_index: int
    focus_bbox: Bounds
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AssetRef:
    sha256: str
    media_type: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class PageRecord:
    page_index: int
    width: float
    height: float
    rotation: int
    svg: AssetRef
    text: AssetRef
    span_count: int
    warnings: tuple[str, ...]
    # None: extracted before text-layer diagnostics existed (not assessed, not "ok").
    text_layer: TextLayerDiagnostic | None = None


@dataclass(frozen=True, slots=True)
class RegionRecord:
    page_index: int
    bbox: Bounds
    native_svg: AssetRef
    cropped_svg: AssetRef
    text: AssetRef
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DocumentManifest:
    schema_version: str
    filename: str
    source: AssetRef
    producer: str
    pages: tuple[PageRecord, ...]
    region: RegionRecord
    coordinate_frame: str = "page-top-left-points; cropped SVG retains page-coordinate viewBox"
    asset_status: str = "saved"
    semantics: SemanticStatus = SemanticStatus.PENDING
    span_to_svg_mapping: SemanticStatus = SemanticStatus.PENDING
    visual_completeness: SemanticStatus = SemanticStatus.PENDING


@dataclass(frozen=True, slots=True)
class TextSidecar:
    schema_version: str
    source_sha256: str
    page_index: int
    spans: tuple[TextSpan, ...]
    coordinate_frame: str = "page-top-left-points"


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    manifest_id: str
    manifest: DocumentManifest
