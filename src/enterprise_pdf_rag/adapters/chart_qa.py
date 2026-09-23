"""Read fixed members and repeat source-aware qualification before ChartQA."""

from enterprise_pdf_rag.adapters.chart_publication import resolve_chart_member
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.source_publication import validate_processing_source
from ragspine.extraction.evidence.figures.chart_qa.models import (
    ChartContext,
    ChartQueryError,
    ChartRefusal,
    QueryFailure,
    QueryPin,
    RefusalReason,
)
from ragspine.extraction.evidence.page.models import ObjectKind


class StoredChartResolver:
    def __init__(
        self,
        sources: LocalDocumentStore,
        outputs: ProcessingStore,
        *,
        processing_id: str,
    ) -> None:
        self.sources = sources
        self.outputs = outputs
        self.processing_id = processing_id

    def resolve(self, pin: QueryPin) -> ChartContext:
        if pin.processing_id != self.processing_id:
            raise ChartQueryError(
                QueryFailure.PIN_CONFLICT, "Query names another processing release"
            )
        try:
            return self._resolve(pin)
        except (ChartQueryError, ChartRefusal):
            raise
        except OSError as error:
            raise ChartQueryError(
                QueryFailure.UNAVAILABLE_EVIDENCE,
                "An immutable query dependency is unavailable; no fallback",
            ) from error
        except ValueError as error:
            raise ChartQueryError(
                QueryFailure.INVALID_EVIDENCE,
                "The pinned source, branch or qualification is inconsistent",
            ) from error

    def _resolve(self, pin: QueryPin) -> ChartContext:
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
            raise ChartRefusal(RefusalReason.UNQUALIFIED_MEMBER)
        # This seam reconstructs the native/cropped SVG, raw branches and proof.
        # No caller-supplied scope or verified flag can replace that operation.
        validated = resolve_chart_member(self.sources, self.outputs.assets, manifest.scope, member)
        return ChartContext(
            pin,
            manifest.scope.source_manifest_id,
            validated.chart,
            validated.description,
            validated.qualification,
            validated.svg,
        )
