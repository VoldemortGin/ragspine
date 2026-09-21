"""Read-only catalog of published documents and pinned read-only mounts; no model calls."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.chart_qa import StoredChartResolver
from enterprise_pdf_rag.adapters.chart_qa_displayed import StoredDisplayResolver
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.adapters.processing_retrieval import (
    CONTEXTUAL_POLICIES,
    ProcessingRetrieval,
    member_anchor,
    member_text,
    resolve_processing_context,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.source_publication import validate_processing_source
from enterprise_pdf_rag.answers.ports import MemberText
from enterprise_pdf_rag.documents.models import AssetRef, Bounds
from enterprise_pdf_rag.figures.chart_qa.displayed_models import DisplayedLookupContext
from enterprise_pdf_rag.figures.chart_qa.models import ChartContext, QueryPin
from enterprise_pdf_rag.figures.ports import EmbeddingPort
from enterprise_pdf_rag.processing.column_regions import (
    EMPTY,
    MIN_COLUMNS,
    ColumnBinding,
    PageColumn,
    PageRegionSpan,
    bind_columns,
)
from enterprise_pdf_rag.processing.models import (
    ObjectKind,
    ProcessingManifest,
    RetrievalPublication,
)
from enterprise_pdf_rag.processing.page_metadata import PageMetadata
from enterprise_pdf_rag.processing.retrieval import (
    PinnedRetrievalHit,
    RetrievalContext,
    RetrievalMember,
    RetrievalPlan,
)

type CatalogOrigin = Literal["ingestion", "legacy"]
type CatalogRetrievalStatus = Literal["ready", "not_indexed", "corrupt"]

_DOCUMENT_ID = re.compile(r"[0-9a-f]{64}")
_MOUNT_REFUSAL = "Embedding provider differs from the published index; refusing to mount"


class CatalogEntry(BoundaryModel):
    document_id: str
    origin: CatalogOrigin
    source_store: str
    processing_store: str
    retrieval_status: CatalogRetrievalStatus
    reason: str | None = None
    source_sha256: str | None = None
    source_manifest_id: str | None = None
    current_processing_id: str | None = None
    retrieval_snapshot_id: str | None = None
    member_count: int | None = None
    embedding_fingerprint: str | None = None
    embedding_dimensions: tuple[int, ...] | None = None
    document_label: str | None = None
    source_page_count: int | None = None
    selected_physical_pages: tuple[int, ...] | None = None
    source_activated: bool | None = None
    # Automatic document metadata (ADR 0013): verbatim page values folded deterministically.
    display_title: str | None = None
    report_period: str | None = None
    language: str | None = None
    years: tuple[int, ...] = ()
    regions: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        """The readable name: the cover title when known, else the source filename or id."""
        return self.display_title or self.document_label or self.document_id


class DocumentCatalog(BoundaryModel):
    catalog_policy: Literal["published-current-processing-with-retrieval-v1"] = (
        "published-current-processing-with-retrieval-v1"
    )
    ingestion_root: str
    legacy_roots: tuple[str, ...]
    documents: tuple[CatalogEntry, ...]
    unpublished: tuple[str, ...]

    @property
    def ready(self) -> tuple[CatalogEntry, ...]:
        return tuple(entry for entry in self.documents if entry.retrieval_status == "ready")

    def entry(self, document_id: str) -> CatalogEntry | None:
        return next((entry for entry in self.documents if entry.document_id == document_id), None)


def _pointer(path: Path) -> str | None:
    return path.read_text().strip() if path.is_file() else None


def _span_union(span_ids: Sequence[str], boxes: Mapping[str, Bounds]) -> Bounds | None:
    """The rectangle covering a page value's evidence spans, or ``None`` if none are known."""
    known = [boxes[span_id] for span_id in span_ids if span_id in boxes]
    if not known:
        return None
    return (
        min(box[0] for box in known),
        min(box[1] for box in known),
        max(box[2] for box in known),
        max(box[3] for box in known),
    )


def _file_state(path: Path) -> tuple[int, int]:
    """Size and modification time: what says a pinned file has not been touched at all."""
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


@dataclass(slots=True)
class _Probe:
    """Facts read so far; whatever a corrupt entry managed to read stays visible."""

    document_id: str
    origin: CatalogOrigin
    source_root: Path
    processing_root: Path
    source_sha256: str | None = None
    source_manifest_id: str | None = None
    current_processing_id: str | None = None
    retrieval_snapshot_id: str | None = None
    member_count: int | None = None
    embedding_fingerprint: str | None = None
    embedding_dimensions: tuple[int, ...] | None = None
    document_label: str | None = None
    source_page_count: int | None = None
    selected_physical_pages: tuple[int, ...] | None = None
    source_activated: bool | None = None
    display_title: str | None = None
    report_period: str | None = None
    language: str | None = None
    years: tuple[int, ...] = ()
    regions: tuple[str, ...] = ()

    def entry(self, status: CatalogRetrievalStatus, reason: str | None) -> CatalogEntry:
        return CatalogEntry(
            document_id=self.document_id,
            origin=self.origin,
            source_store=str(self.source_root),
            processing_store=str(self.processing_root),
            retrieval_status=status,
            reason=reason,
            source_sha256=self.source_sha256,
            source_manifest_id=self.source_manifest_id,
            current_processing_id=self.current_processing_id,
            retrieval_snapshot_id=self.retrieval_snapshot_id,
            member_count=self.member_count,
            embedding_fingerprint=self.embedding_fingerprint,
            embedding_dimensions=self.embedding_dimensions,
            document_label=self.document_label,
            source_page_count=self.source_page_count,
            selected_physical_pages=self.selected_physical_pages,
            source_activated=self.source_activated,
            display_title=self.display_title,
            report_period=self.report_period,
            language=self.language,
            years=self.years,
            regions=self.regions,
        )


def _inspect(
    *,
    document_id: str | None,
    source_root: Path,
    processing_root: Path,
    origin: CatalogOrigin,
    expected_sha: str | None,
) -> CatalogEntry:
    """Follow the processing pointer and verify the whole release; reads only."""
    probe = _Probe(document_id or source_root.name, origin, source_root, processing_root)
    try:
        outputs = ProcessingStore(processing_root)
        sources = LocalDocumentStore(source_root, activate_on_publish=False)
        processing_id = _pointer(processing_root / "current-processing")
        if processing_id is None:
            raise FileNotFoundError(
                f"Missing discovery pointer {processing_root / 'current-processing'}"
            )
        probe.current_processing_id = processing_id
        manifest = outputs.load(processing_id)
        scope = manifest.scope
        if document_id is None:
            probe.document_id = scope.source_sha256
        probe.source_sha256 = scope.source_sha256
        probe.source_manifest_id = scope.source_manifest_id
        probe.source_page_count = scope.source_page_count
        probe.selected_physical_pages = scope.physical_pages
        metadata = manifest.document_metadata
        if metadata is not None:
            probe.display_title = (
                None if metadata.display_title is None else metadata.display_title.text
            )
            probe.report_period = (
                None
                if metadata.report_period is None
                else metadata.report_period.normalized or metadata.report_period.text
            )
            probe.language = metadata.language
            probe.years = metadata.years
            probe.regions = tuple(region.text for region in metadata.regions)
        if expected_sha is not None and scope.source_sha256 != expected_sha:
            raise ValueError(
                "Document directory name does not match the processing scope source sha256"
            )
        snapshot = sources.load(scope.source_manifest_id)
        if snapshot.manifest.source.sha256 != scope.source_sha256:
            raise ValueError("Source manifest does not carry the processing scope source sha256")
        probe.document_label = snapshot.manifest.filename
        probe.source_activated = (
            _pointer(source_root / "current-manifest") == scope.source_manifest_id
        )
        if manifest.retrieval is None:
            return probe.entry("not_indexed", "current processing has no retrieval publication")
        probe.retrieval_snapshot_id = manifest.retrieval.snapshot_id
        plan, _ = outputs.load_retrieval(manifest.retrieval)
        validate_processing_source(
            sources=sources, artifacts=outputs.assets, manifest=manifest, plan=plan
        )
        probe.member_count = len(plan.members)
        if not plan.members:
            return probe.entry("not_indexed", "retrieval publication has no retrievable members")
        fingerprints = {member.embedding_fingerprint for member in plan.members}
        if len(fingerprints) != 1:
            raise ValueError("Retrieval snapshot mixes embedding providers")
        probe.embedding_fingerprint = fingerprints.pop()
        probe.embedding_dimensions = tuple(
            sorted({member.embedding_dimensions for member in plan.members})
        )
        return probe.entry("ready", None)
    except (ValueError, OSError) as error:
        return probe.entry("corrupt", str(error) or type(error).__name__)


def scan_catalog(ingestion_root: Path, *, legacy_roots: Sequence[Path] = ()) -> DocumentCatalog:
    """List every published document under the roots without writing or embedding.

    A missing ingestion root is an empty catalog. Ingestion subdirectories without a
    ``current-processing`` pointer are drafts and are only named in ``unpublished``.
    Every legacy root is a processing store whose parent is its source store; a
    missing pointer there is a configuration error and surfaces as ``corrupt``.
    """
    ingestion_root = ingestion_root.expanduser().resolve()
    entries: list[CatalogEntry] = []
    unpublished: list[str] = []
    if ingestion_root.is_dir():
        for child in sorted(ingestion_root.iterdir()):
            if not child.is_dir() or _DOCUMENT_ID.fullmatch(child.name) is None:
                continue
            processing = child / "processing"
            if not (processing / "current-processing").is_file():
                unpublished.append(child.name)
                continue
            entries.append(
                _inspect(
                    document_id=child.name,
                    source_root=child / "source",
                    processing_root=processing,
                    origin="ingestion",
                    expected_sha=child.name,
                )
            )
    resolved_legacy = tuple(root.expanduser().resolve() for root in legacy_roots)
    for processing_root in resolved_legacy:
        entries.append(
            _inspect(
                document_id=None,
                source_root=processing_root.parent,
                processing_root=processing_root,
                origin="legacy",
                expected_sha=None,
            )
        )
    seen: set[str] = set()
    documents: list[CatalogEntry] = []
    for entry in entries:
        if entry.document_id in seen:
            entry = entry.model_copy(
                update={
                    "retrieval_status": "corrupt",
                    "reason": f"duplicate document id {entry.document_id}",
                }
            )
        seen.add(entry.document_id)
        documents.append(entry)
    documents.sort(key=lambda entry: entry.document_id)
    return DocumentCatalog(
        ingestion_root=str(ingestion_root),
        legacy_roots=tuple(str(root) for root in resolved_legacy),
        documents=tuple(documents),
        unpublished=tuple(unpublished),
    )


class QueryEmbeddingUnavailable(RuntimeError):
    """The mount has no query embedder; evidence reads still work."""


class MountedDocument:
    """One published document pinned by processing id; never touches discovery pointers."""

    def __init__(
        self,
        entry: CatalogEntry,
        sources: LocalDocumentStore,
        outputs: ProcessingStore,
        manifest: ProcessingManifest,
        publication: RetrievalPublication,
        retrieval: ProcessingRetrieval | None,
        *,
        verify_every_request: bool = False,
    ) -> None:
        if entry.current_processing_id is None or entry.embedding_fingerprint is None:
            raise ValueError("A mount requires the pinned processing id and embedding fingerprint")
        self._entry = entry
        self._sources = sources
        self._outputs = outputs
        self._pinned = manifest
        self._publication = publication
        self._retrieval = retrieval
        self._processing_id = entry.current_processing_id
        self._embedding_fingerprint = entry.embedding_fingerprint
        self._charts = StoredChartResolver(sources, outputs, processing_id=self._processing_id)
        self._displayed = StoredDisplayResolver(sources, outputs, processing_id=self._processing_id)
        # Immutable with the pinned manifest: read once, reused by every member_texts().
        self._page_metadata = outputs.load_page_metadata(manifest)
        self._contexts = outputs.index_contexts(manifest)
        # Derived from the same pinned release, on the first ``member_texts()`` that needs it.
        self._columns: dict[int, ColumnBinding] | None = None
        self._verify_every_request = verify_every_request
        # The one file a request must watch: the pinned manifest object. Its name is its
        # digest, so any rewrite of the release is a digest mismatch here.
        self._pinned_path = outputs.assets.content_path(self._processing_id)
        self._pinned_stat = _file_state(self._pinned_path)
        # Evidence hydrated under this pinned manifest; every entry was fully verified on
        # its first read, and the manifest guard below invalidates the whole mount on drift.
        self._resolved: dict[str, RetrievalContext] = {}

    @property
    def entry(self) -> CatalogEntry:
        return self._entry

    @property
    def document_id(self) -> str:
        return self._entry.document_id

    @property
    def source_sha256(self) -> str:
        return self._pinned.scope.source_sha256

    @property
    def processing_id(self) -> str:
        return self._processing_id

    @property
    def retrieval_snapshot_id(self) -> str:
        return self._publication.snapshot_id

    @property
    def embedding_fingerprint(self) -> str:
        return self._embedding_fingerprint

    def manifest(self) -> ProcessingManifest:
        """Re-read what the mounted release is pinned to, and refuse any drift from it.

        The whole release — every asset digest, the source it was cut from, every member's
        evidence — was verified once when this document was mounted. What a request re-reads
        is the manifest object that names all of it: the file is content-addressed, so its
        digest *is* the pinned processing id and no rewrite of the release can keep it. Size
        and mtime are only a shortcut past re-hashing a file nothing has touched; a file that
        moved at all is re-hashed, and a digest that no longer matches falls through to the
        full mount-time validation, which refuses. ``verify_every_request`` skips the
        shortcut and revalidates the whole release on every call.
        """
        if not self._verify_every_request:
            state = _file_state(self._pinned_path)
            if state == self._pinned_stat:
                return self._pinned
            if sha256(self._pinned_path.read_bytes()).hexdigest() == self._processing_id:
                self._pinned_stat = state
                return self._pinned
        manifest = self._outputs.load(self._processing_id)
        if manifest != self._pinned:
            raise ValueError("Immutable processing manifest changed")
        return manifest

    def search(self, query: str, *, limit: int = 5) -> tuple[PinnedRetrievalHit, ...]:
        self.manifest()
        if self._retrieval is None:
            raise QueryEmbeddingUnavailable(
                "Local query embedding is not configured for this mount; no substitute"
            )
        return self._retrieval.search(self._publication, query, limit=limit)

    def resolve(self, hit: PinnedRetrievalHit) -> RetrievalContext:
        """Hydrate one member's verified evidence; the same member is hydrated once.

        A member's evidence is immutable with the manifest this mount pinned, and the guard
        above refuses any drift from it, so re-proving the same table or chart several times
        within one request proves nothing new. The first read still runs the full proof.
        """
        self.manifest()
        if self._verify_every_request:
            return resolve_processing_context(self._sources, self._outputs, self._publication, hit)
        if hit.snapshot_id != self._publication.snapshot_id:
            raise ValueError("Retrieval hit belongs to another semantic snapshot")
        context = self._resolved.get(hit.member_id)
        if context is None:
            context = resolve_processing_context(
                self._sources, self._outputs, self._publication, hit
            )
            self._resolved[hit.member_id] = context
        return context

    def member_texts(self) -> tuple[MemberText, ...]:
        """Every pinned member's embedded index text, ordered by member id."""
        self.manifest()
        plan, _ = self._outputs.load_retrieval(self._publication)
        columns = self._column_bindings(plan)
        texts = [
            self._member_text(
                plan,
                member,
                self._page_metadata.get(member.page_index),
                columns.get(member.page_index, EMPTY),
            )
            for member in plan.members
        ]
        return tuple(sorted(texts, key=lambda item: item.member_id))

    def _column_bindings(self, plan: RetrievalPlan) -> dict[int, ColumnBinding]:
        """Which page region names each chart, on pages that print several side by side.

        Derived from the pinned release itself — the charts' own rectangles and the
        rectangles of the spans the page's regions were copied from — so a snapshot
        published before this existed binds its columns without being re-indexed.
        """
        if self._columns is not None:
            return self._columns
        charts: dict[int, list[PageColumn]] = {}
        for member in plan.members:
            if member.kind is not ObjectKind.CHART:
                continue
            bbox = member_anchor(self._outputs.assets, member)
            if bbox is not None:
                charts.setdefault(member.page_index, []).append(PageColumn(member.member_id, bbox))
        bindings: dict[int, ColumnBinding] = {}
        for page_index, columns in charts.items():
            metadata = self._page_metadata.get(page_index)
            if metadata is None or len(columns) < MIN_COLUMNS:
                continue
            boxes = self._span_boxes(page_index)
            if boxes is None:
                continue
            binding = bind_columns(
                tuple(
                    PageRegionSpan(region.text, _span_union(region.evidence.span_ids, boxes))
                    for region in metadata.regions
                ),
                tuple(sorted(columns, key=lambda column: (column.bbox[0], column.member_id))),
            )
            if binding is not EMPTY:
                bindings[page_index] = binding
        self._columns = bindings
        return bindings

    def _span_boxes(self, page_index: int) -> dict[str, Bounds] | None:
        """Every source span's rectangle on one page; ``None`` when the page cannot be read.

        Best-effort, exactly like ``member_anchor``: geometry read for a refinement must
        never fail a mount the evidence itself supports.
        """
        try:
            source = self._sources.load(self._pinned.scope.source_manifest_id)
            sidecar = read_text_sidecar(self._sources, source, page_index)
        except (ValueError, KeyError, OSError, IndexError):
            return None
        return {span.span_id: span.bbox for span in sidecar.spans}

    def _member_text(
        self,
        plan: RetrievalPlan,
        member: RetrievalMember,
        metadata: PageMetadata | None,
        binding: ColumnBinding,
    ) -> MemberText:
        context = self._contexts.get(member.page_index)
        text = member_text(self._outputs.assets, plan, member, context)
        # Reported only when the policy actually prefixed it, so ``body`` stays exact.
        header = (
            context.header()
            if context is not None and plan.qualification_policy in CONTEXTUAL_POLICIES
            else ""
        )
        bbox = member_anchor(self._outputs.assets, member)
        column = binding.by_member.get(member.member_id, ())
        if column and header:
            # The lexical channel reads one more phrase than the embedding saw: the column's
            # own heading, so `Thailand 1H26 VONB` scores the Thailand chart over its
            # neighbours. The stored vectors are untouched, so no release is re-indexed.
            body = text[len(header) + 1 :] if text.startswith(header + "\n") else text
            header = " | ".join((header, *column))
            text = f"{header}\n{body}"
        if metadata is None:
            return MemberText(
                member.member_id,
                member.kind,
                member.page_index,
                text,
                header=header,
                bbox=bbox,
            )
        return MemberText(
            member.member_id,
            member.kind,
            member.page_index,
            text,
            page_title=None if metadata.title is None else metadata.title.text,
            section=None if metadata.section is None else metadata.section.text,
            page_type=metadata.page_type.value,
            periods=metadata.normalized_periods,
            regions=tuple(region.text for region in metadata.regions),
            member_regions=binding.regions_for(member.member_id),
            header=header,
            bbox=bbox,
        )

    def _pin(self, hit: PinnedRetrievalHit) -> QueryPin:
        self.manifest()
        if hit.snapshot_id != self._publication.snapshot_id:
            raise ValueError("Retrieval hit belongs to another semantic snapshot")
        return QueryPin(self._processing_id, hit.snapshot_id, hit.member_id)

    def chart_context(self, hit: PinnedRetrievalHit) -> ChartContext:
        """Requalify a chart member with its SVG evidence; non-chart members are refused."""
        return self._charts.resolve(self._pin(hit))

    def displayed_context(self, hit: PinnedRetrievalHit) -> DisplayedLookupContext:
        """Requalify a displayed-value bar member; any other member is refused."""
        return self._displayed.resolve(self._pin(hit))

    def read_asset(self, ref: AssetRef) -> bytes:
        """Digest-checked evidence bytes from the processing store, else the source store."""
        try:
            return self._outputs.assets.get(ref)
        except FileNotFoundError:
            return self._sources.get(ref)


def mount_document(
    entry: CatalogEntry,
    *,
    embedder: EmbeddingPort | None,
    verify_every_request: bool = False,
) -> MountedDocument:
    """Open one ready entry by its pinned ids; the embedder is injected, never built.

    ``None`` mounts evidence only: ``resolve`` works and ``search`` raises
    ``QueryEmbeddingUnavailable``. A fingerprint that differs from the published
    index is refused before any store is opened and without any model call.

    The whole release is verified here, once. ``verify_every_request`` makes every later
    request repeat that verification instead of re-reading the pinned manifest alone — the
    audit setting, orders of magnitude slower on a real document.
    """
    if (
        entry.retrieval_status != "ready"
        or entry.current_processing_id is None
        or entry.retrieval_snapshot_id is None
        or entry.embedding_fingerprint is None
    ):
        raise ValueError(
            f"Catalog entry {entry.document_id} is not mountable: "
            f"{entry.reason or entry.retrieval_status}"
        )
    if embedder is not None and entry.embedding_fingerprint != embedder.fingerprint:
        raise ValueError(_MOUNT_REFUSAL)
    sources = LocalDocumentStore(Path(entry.source_store), activate_on_publish=False)
    outputs = ProcessingStore(
        Path(entry.processing_store), verify_every_request=verify_every_request
    )
    manifest = outputs.load(entry.current_processing_id)
    publication = manifest.retrieval
    if publication is None or publication.snapshot_id != entry.retrieval_snapshot_id:
        raise ValueError("Pinned retrieval snapshot differs from the catalog entry")
    if manifest.scope.source_sha256 != entry.source_sha256:
        raise ValueError("Pinned processing scope differs from the catalog entry")
    plan, _ = outputs.load_retrieval(publication)
    if {member.embedding_fingerprint for member in plan.members} != {entry.embedding_fingerprint}:
        raise ValueError(_MOUNT_REFUSAL)
    validate_processing_source(
        sources=sources, artifacts=outputs.assets, manifest=manifest, plan=plan
    )
    return MountedDocument(
        entry,
        sources,
        outputs,
        manifest,
        publication,
        None if embedder is None else ProcessingRetrieval(sources, outputs, embedder),
        verify_every_request=verify_every_request,
    )


@dataclass(frozen=True, slots=True)
class MountedCatalog:
    catalog: DocumentCatalog
    embedding_fingerprint: str | None
    documents: Mapping[str, MountedDocument]
    failures: Mapping[str, str]


def mount_catalog(
    catalog: DocumentCatalog,
    *,
    embedder: EmbeddingPort | None,
    verify_every_request: bool = False,
) -> MountedCatalog:
    """Mount every ready entry; refusals are recorded per document, never hidden or raised."""
    documents: dict[str, MountedDocument] = {}
    failures: dict[str, str] = {}
    for entry in catalog.documents:
        if entry.retrieval_status != "ready":
            failures[entry.document_id] = entry.reason or entry.retrieval_status
            continue
        try:
            documents[entry.document_id] = mount_document(
                entry, embedder=embedder, verify_every_request=verify_every_request
            )
        except (ValueError, OSError) as error:
            failures[entry.document_id] = str(error) or type(error).__name__
    return MountedCatalog(
        catalog,
        None if embedder is None else embedder.fingerprint,
        documents,
        failures,
    )
