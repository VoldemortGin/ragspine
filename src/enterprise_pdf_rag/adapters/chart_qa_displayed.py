"""Pin a displayed bar before invoking its independent trusted source validator."""

from typing import Protocol, runtime_checkable

from enterprise_pdf_rag.adapters.bar_publication import resolve_displayed_bar_member
from enterprise_pdf_rag.adapters.chart_member_validation import (
    uses_displayed_bar_policy,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.source_publication import validate_processing_source
from enterprise_pdf_rag.figures.chart_qa.displayed_models import (
    DisplayedLookupContext,
    DisplayedRefusal,
    DisplayedRefusalReason,
    ValidatedDisplayedBar,
)
from enterprise_pdf_rag.figures.chart_qa.models import (
    ChartQueryError,
    QueryFailure,
    QueryPin,
)
from enterprise_pdf_rag.processing.models import ObjectKind, ProcessingScope
from enterprise_pdf_rag.processing.retrieval import RetrievalMember


@runtime_checkable
class DisplayedMemberValidator(Protocol):
    def __call__(
        self,
        sources: LocalDocumentStore,
        assets: LocalDocumentStore,
        scope: ProcessingScope,
        member: RetrievalMember,
    ) -> ValidatedDisplayedBar:
        """Reconstruct PDF/profile/native/crop/raw/normalized branches and proof."""
        ...


class StoredDisplayResolver:
    def __init__(
        self,
        sources: LocalDocumentStore,
        outputs: ProcessingStore,
        *,
        processing_id: str,
        validator: DisplayedMemberValidator | None = resolve_displayed_bar_member,
    ) -> None:
        self.sources = sources
        self.outputs = outputs
        self.processing_id = processing_id
        self.validator = validator

    def resolve(self, pin: QueryPin) -> DisplayedLookupContext:
        if pin.processing_id != self.processing_id:
            raise ChartQueryError(
                QueryFailure.PIN_CONFLICT, "Query names another processing release"
            )
        try:
            return self._resolve(pin)
        except (ChartQueryError, DisplayedRefusal):
            raise
        except OSError as error:
            raise ChartQueryError(
                QueryFailure.UNAVAILABLE_EVIDENCE,
                "A displayed-bar source dependency is unavailable; no fallback",
            ) from error
        except ValueError as error:
            raise ChartQueryError(
                QueryFailure.INVALID_EVIDENCE,
                "The displayed-bar source, branch or qualification is inconsistent",
            ) from error

    def _resolve(self, pin: QueryPin) -> DisplayedLookupContext:
        manifest = self.outputs.load(pin.processing_id)
        if manifest.retrieval is None or manifest.retrieval.snapshot_id != pin.snapshot_id:
            raise ChartQueryError(
                QueryFailure.PIN_CONFLICT, "Query names another retrieval snapshot"
            )
        plan, _ = self.outputs.load_retrieval(manifest.retrieval)
        member = next((item for item in plan.members if item.member_id == pin.member_id), None)
        if member is None:
            raise ChartQueryError(
                QueryFailure.PIN_CONFLICT,
                "Query member is absent from the pinned snapshot",
            )
        validate_processing_source(
            sources=self.sources,
            artifacts=self.outputs.assets,
            manifest=manifest,
            plan=None,
        )
        if member.kind is not ObjectKind.CHART:
            raise DisplayedRefusal(DisplayedRefusalReason.UNQUALIFIED_MEMBER)
        if not uses_displayed_bar_policy(self.outputs.assets.get(member.qualification)):
            raise DisplayedRefusal(DisplayedRefusalReason.UNQUALIFIED_MEMBER)
        if self.validator is None:
            raise ChartQueryError(
                QueryFailure.UNAVAILABLE_EVIDENCE,
                "Displayed-bar trusted source profile and paint validator are unavailable",
            )
        validated = self.validator(self.sources, self.outputs.assets, manifest.scope, member)
        return DisplayedLookupContext(
            pin,
            manifest.scope.source_manifest_id,
            validated.raw_chart,
            validated.chart,
            validated.description,
            validated.qualification,
            validated.svg,
            validated.point_periods,
            validated.page_context,
            validated.description_normalization,
        )
