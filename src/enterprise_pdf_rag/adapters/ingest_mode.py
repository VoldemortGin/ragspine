"""Ingest modes (ADR 0025): which model calls an ingest sends, and what stands in for the rest.

``full`` is the pipeline as it has always run. ``lite`` keeps every call a fact is read from —
the page layout, a chart's IR and both diagram branches — and replaces the calls nothing
retrievable reads: an image's two branches (images are never retrievable), a formula's two
branches (its proof reads the PDF, never the model), a chart's description (derived from its
IR's printed labels instead), the page-metadata call (derived from the page's own text
geometry instead) and the document tree. It also writes no review pages.

Every switch is one field of ``IngestPlan`` so each can be tested and moved on its own; the
mode name only picks a preset. ``layout`` is the selection point for the page partitioner:
both modes use the model layout today, and the deterministic text-page layout of the next
step plugs in there (``make_partitioner``).
"""

from dataclasses import dataclass
from typing import Final, Literal, get_args

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.page_metadata_extraction import PAGE_METADATA_DETERMINISTIC
from enterprise_pdf_rag.adapters.page_partition import ModelPagePartitioner
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.extraction.evidence.page.models import ProcessingManifest
from ragspine.extraction.evidence.page.ports import PagePartitioner

type IngestMode = Literal["full", "lite"]
# Which partitioner proposes a page's objects. Only the model layout exists so far.
type LayoutPolicy = Literal["model"]
INGEST_MODES: Final[tuple[str, ...]] = get_args(IngestMode.__value__)
# The call categories a mode may leave unsent, as ``IngestionSummary.skipped_calls`` keys.
SKIPPED_CALL_KINDS: Final = ("image", "formula", "chart_description", "page_metadata")
# What a stage a mode chose not to run says, so it reads apart from a failure or a budget.
SKIPPED_CODE: Final = "skipped_by_ingest_mode"


@dataclass(frozen=True, slots=True)
class IngestPlan:
    """Every switch an ingest mode sets; the defaults are exactly ``full``."""

    mode: IngestMode
    layout: LayoutPolicy = "model"
    # Send an Image object's two model branches (images are never retrievable either way).
    image_semantics: bool = True
    # Send a Formula object's two model branches (lineage only; the proof reads the PDF).
    formula_semantics: bool = True
    # A chart's description: its own model call, or derived from its IR's printed labels.
    chart_description: Literal["model", "from-ir"] = "model"
    # Page metadata: one text-only model call per page, or derived from the page's geometry.
    page_metadata: Literal["model", "deterministic"] = "model"
    # Write the per-run review pages (source review and processing review).
    review_exports: bool = True
    # Build the document tree after publication when the caller does not say.
    build_tree: bool = True

    @property
    def object_variant(self) -> str | None:
        """The suffix lite adds to the object producer; ``None`` keeps full's bytes."""
        default = IngestPlan(self.mode)
        changed = (
            self.image_semantics,
            self.formula_semantics,
            self.chart_description,
        ) != (
            default.image_semantics,
            default.formula_semantics,
            default.chart_description,
        )
        return "lite-v1" if changed else None


_PLANS: Final[dict[str, IngestPlan]] = {
    "full": IngestPlan("full"),
    "lite": IngestPlan(
        "lite",
        image_semantics=False,
        formula_semantics=False,
        chart_description="from-ir",
        page_metadata="deterministic",
        review_exports=False,
        build_tree=False,
    ),
}


def check_ingest_mode(value: str) -> IngestMode:
    """``value`` as an ``IngestMode``, or a ``ValueError`` naming the accepted modes."""
    if value not in _PLANS:
        raise ValueError(f"ingest_mode must be one of {list(INGEST_MODES)}, not {value!r}")
    return _PLANS[value].mode


def ingest_plan(mode: IngestMode) -> IngestPlan:
    """The switches one mode sets."""
    return _PLANS[check_ingest_mode(mode)]


def make_partitioner(
    plan: IngestPlan, client: JsonCompletionClient, sources: LocalDocumentStore
) -> PagePartitioner:
    """The page partitioner a plan selects; the one layout seam a deterministic one joins."""
    if plan.layout == "model":
        return ModelPagePartitioner(client, sources)
    raise ValueError(f"unknown layout policy {plan.layout!r}")


def published_ingest_mode(manifest: ProcessingManifest) -> IngestMode | None:
    """The mode a saved snapshot was ingested in, read from its page-metadata producers.

    Lite derives page metadata deterministically and full asks the model, so the producer
    of any page's metadata stage names the mode; ``None`` for a snapshot without one.
    """
    producers = {page.metadata.producer for page in manifest.pages if page.metadata is not None}
    if not producers:
        return None
    return "lite" if PAGE_METADATA_DETERMINISTIC in producers else "full"
