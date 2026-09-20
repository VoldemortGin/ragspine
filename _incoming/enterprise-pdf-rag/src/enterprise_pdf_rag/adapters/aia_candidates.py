"""Load pinned, provisional AIA regions without promoting them to gold labels."""

from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    FiniteFloat,
    ValidationError,
    field_validator,
)

from enterprise_pdf_rag.core.settings import resource_path
from enterprise_pdf_rag.figures.models import Confidence, Verification
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput

SOURCE_SHA256 = "df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e"
DEFAULT_CATALOG = resource_path("resources/aia-first-20-regions.json")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Source(_StrictModel):
    sha256: str
    page_count: int
    selected_physical_pages: tuple[int, int]


class _Candidate(_StrictModel):
    object_id: str
    page_number: int
    page_index: int
    bbox: tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]
    kind: ObjectKind
    method: Literal["manual-source-render-review", "root-source-render-review"]
    verification: Literal["pending"]
    gold: Literal[False]
    bbox_status: Literal["locked", "provisional"]
    source_span_ids: tuple[str, ...]
    page_context_span_ids: tuple[str, ...]
    interpretation: str
    completeness_gaps: tuple[str, ...]
    alternate_kind_candidate: ObjectKind | None
    native_table_status: Literal["unavailable"] | None

    @field_validator("source_span_ids", "page_context_span_ids")
    @classmethod
    def _unique_occurrences(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError("Candidate occurrence IDs must be nonempty and unique")
        return value


class _Catalog(_StrictModel):
    schema_version: Literal["aia-first-20-region-candidates-v1"]
    status: Literal["manual_candidates_pending_not_gold"]
    source: _Source
    coordinate_frame: Literal["page-top-left-points"]
    selection_rule: str
    candidates: tuple[_Candidate, ...]


def _load_catalog(path: Path) -> _Catalog:
    try:
        catalog = _Catalog.model_validate_json(path.read_bytes(), strict=True)
    except (OSError, ValidationError) as error:
        raise ValueError("AIA candidate catalog is missing or invalid") from error
    if (
        catalog.source.sha256 != SOURCE_SHA256
        or catalog.source.page_count != 71
        or catalog.source.selected_physical_pages != (1, 20)
    ):
        raise ValueError("AIA candidate catalog source identity is invalid")
    identities = tuple(item.object_id for item in catalog.candidates)
    if len(identities) != len(set(identities)):
        raise ValueError("AIA candidate object IDs must be unique")
    return catalog


def candidates_for(
    page: PageInput, *, catalog_path: Path = DEFAULT_CATALOG
) -> tuple[LayoutObject, ...]:
    """Return source-bound candidates for one selected physical page.

    The catalog records review proposals. Returning a candidate never qualifies
    its semantic type, relationships, or completeness.
    """

    if page.source_sha256 != SOURCE_SHA256:
        raise ValueError("Candidate catalog only accepts the pinned AIA source")
    if not 0 <= page.page_index < 20:
        raise ValueError("Candidate catalog only accepts physical pages 1-20")
    catalog = _load_catalog(catalog_path)
    observed = {span.span_id for span in page.text.spans}
    result: list[LayoutObject] = []
    for candidate in catalog.candidates:
        if candidate.page_index != page.page_index:
            continue
        if candidate.page_number != candidate.page_index + 1:
            raise ValueError("Candidate physical page identity is inconsistent")
        x0, y0, x1, y1 = candidate.bbox
        if not (0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height):
            raise ValueError("Candidate bbox is outside the source page")
        referenced = set(candidate.source_span_ids) | set(
            candidate.page_context_span_ids
        )
        if not referenced <= observed:
            raise ValueError("Candidate references unknown source occurrences")
        if set(candidate.source_span_ids) & set(candidate.page_context_span_ids):
            raise ValueError("Page context cannot be reused as crop evidence")
        result.append(
            LayoutObject(
                object_id=candidate.object_id,
                kind=candidate.kind,
                bbox=(
                    candidate.bbox[0],
                    candidate.bbox[1],
                    candidate.bbox[2],
                    candidate.bbox[3],
                ),
                source_span_ids=candidate.source_span_ids,
                interpretation=candidate.interpretation,
                confidence=Confidence(
                    None,
                    f"{candidate.method}; uncalibrated manual candidate",
                ),
                verification=Verification.PENDING,
                context_span_ids=candidate.page_context_span_ids,
            )
        )
    return tuple(result)
