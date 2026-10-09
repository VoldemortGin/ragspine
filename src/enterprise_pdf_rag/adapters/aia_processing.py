"""Selected-page processing uses saved source assets, never the old focus region."""

import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from hashlib import sha256
from typing import TypeVar

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.layout_normalization import (
    NORMALIZATION_VERSION,
    normalize_partition,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.shared_pdf import shared_pdfs
from ragspine.extraction.evidence.document.models import DocumentSnapshot
from ragspine.extraction.evidence.page.models import (
    CanonicalPage,
    LayoutObject,
    ObjectProcessingRecord,
    PageInput,
    PagePartition,
    PageProcessingRecord,
    ProcessingManifest,
    ProcessingScope,
    StageOutcome,
    StageState,
)
from ragspine.extraction.evidence.page.ports import ObjectProcessor, PagePartitioner
from ragspine.extraction.evidence.page.service import canonical_page, validate_partition

# Upper bound of ``page_concurrency`` (ADR 0045), the same as ``MAX_PARALLEL_DOCUMENTS``.
MAX_PAGE_CONCURRENCY = 16
# How often a thread waiting on a page polls, so an interrupt is seen (ADR 0033 §7).
_POLL_SECONDS = 0.2

_T = TypeVar("_T")


def stage_fingerprint(stage: str, producer: str, inputs: tuple[object, ...]) -> str:
    return sha256(repr(("processing-stage-v1", stage, producer, inputs)).encode()).hexdigest()


class ProcessingPipeline:
    def __init__(
        self,
        sources: LocalDocumentStore,
        outputs: ProcessingStore,
        partitioner: PagePartitioner | None,
        object_processor: ObjectProcessor | None,
        *,
        normalize_layout: bool = True,
        activate: bool = True,
        producer: str = "aia-processing-v1",
        page_concurrency: int = 1,
    ) -> None:
        if not 1 <= page_concurrency <= MAX_PAGE_CONCURRENCY:
            raise ValueError(f"page_concurrency must be within 1..{MAX_PAGE_CONCURRENCY}")
        self.sources = sources
        self.outputs = outputs
        self.partitioner = partitioner
        self.object_processor = object_processor
        self.normalize_layout = normalize_layout
        self.activate = activate
        self.producer = producer
        self.page_concurrency = page_concurrency

    @shared_pdfs()
    def run(
        self,
        source_manifest_id: str,
        *,
        selected_page_indices: tuple[int, ...],
        on_page: Callable[[int, int], None] | None = None,
    ) -> tuple[str, ProcessingManifest]:
        """Process every selected page; ``on_page(done, total)`` follows each finished page."""
        source = self.sources.load(source_manifest_id)
        scope = ProcessingScope(
            source_manifest_id,
            source.manifest.source.sha256,
            len(source.manifest.pages),
            selected_page_indices,
        )
        if self.page_concurrency > 1:
            pages = self._run_at_once(source, scope, on_page)
        else:
            pages = []
            for page_index in scope.selected_page_indices:
                # One backend transaction per page (ADR 0036 §7.2): every stage entry and small
                # object of the page commits together; a crash loses at most this page, and its
                # model calls replay from the model cache. A no-op on the file layout.
                with self.outputs.transaction():
                    pages.append(self._page(self._page_input(source, scope, page_index)))
                if on_page is not None:
                    on_page(len(pages), len(scope.selected_page_indices))
        manifest = ProcessingManifest("processing-v1", scope, self.producer, tuple(pages))
        save = self.outputs.publish if self.activate else self.outputs.save_draft
        return save(manifest, sources=self.sources), manifest

    def _page_input(
        self, source: DocumentSnapshot, scope: ProcessingScope, page_index: int
    ) -> PageInput:
        observed = source.manifest.pages[page_index]
        return PageInput(
            scope.source_manifest_id,
            scope.source_sha256,
            page_index,
            observed.width,
            observed.height,
            observed.svg,
            read_text_sidecar(self.sources, source, page_index),
        )

    def _page(
        self,
        page: PageInput,
        *,
        gate: AbstractContextManager[object] | None = None,
        objects: Callable[[PageInput, tuple[LayoutObject, ...]], tuple[ObjectProcessingRecord, ...]]
        | None = None,
    ) -> PageProcessingRecord:
        canonical = self._canonical(page)
        partition_stage, partition = self._partition(page, canonical, gate=gate)
        raw_partition_stage = partition_stage if self.normalize_layout else None
        if partition is not None and self.normalize_layout:
            partition_stage, partition = self._normalize(page, partition_stage, partition)
        records = (
            ()
            if partition is None
            else tuple(self._object(page, item) for item in partition.objects)
            if objects is None
            else objects(page, partition.objects)
        )
        return PageProcessingRecord(
            page.page_index, canonical, partition_stage, records, raw_partition_stage
        )

    def _run_at_once(
        self,
        source: DocumentSnapshot,
        scope: ProcessingScope,
        on_page: Callable[[int, int], None] | None,
    ) -> list[PageProcessingRecord]:
        """Pages on one pool, objects on another, model-bearing work under one semaphore of N.

        Results are taken, and ``on_page`` called, in page order; every object record keeps
        its object's place. No page transaction (a worker must not hold the store's write
        lock across a model call); each write commits alone. Workers run in a copy of this
        run's context, so they share its ``shared_pdfs()`` scope (ADR 0045).
        """
        gate = threading.BoundedSemaphore(self.page_concurrency)
        page_pool = ThreadPoolExecutor(self.page_concurrency, thread_name_prefix="ingest-page")
        object_pool = ThreadPoolExecutor(self.page_concurrency, thread_name_prefix="ingest-object")

        def gated_object(page: PageInput, item: LayoutObject) -> ObjectProcessingRecord:
            with gate:
                return self._object(page, item)

        def objects(
            page: PageInput, items: tuple[LayoutObject, ...]
        ) -> tuple[ObjectProcessingRecord, ...]:
            if self.object_processor is None:
                return tuple(self._object(page, item) for item in items)
            futures = [
                object_pool.submit(contextvars.copy_context().run, gated_object, page, item)
                for item in items
            ]
            return tuple(_result(future) for future in futures)

        def one_page(page_index: int) -> PageProcessingRecord:
            return self._page(
                self._page_input(source, scope, page_index), gate=gate, objects=objects
            )

        pages: list[PageProcessingRecord] = []
        try:
            futures = [
                page_pool.submit(contextvars.copy_context().run, one_page, page_index)
                for page_index in scope.selected_page_indices
            ]
            for future in futures:
                pages.append(_result(future))
                if on_page is not None:
                    on_page(len(pages), len(scope.selected_page_indices))
        finally:
            # Queued pages never start; running ones finish their calls (and objects) so no
            # claim is left behind, then the object pool is drained (ADR 0033 §7).
            page_pool.shutdown(wait=True, cancel_futures=True)
            object_pool.shutdown(wait=True, cancel_futures=True)
        return pages

    def _canonical(self, page: PageInput) -> StageOutcome:
        canonical = canonical_page(page)
        payload = TypeAdapter(CanonicalPage).dump_json(canonical)
        fingerprint = stage_fingerprint(
            "canonical", "canonical-source-v1", (sha256(payload).hexdigest(),)
        )
        cached = self.outputs.cached(fingerprint)
        if cached is not None:
            return cached
        return self.outputs.cache_output("canonical", fingerprint, "canonical-source-v1", payload)

    def _partition(
        self,
        page: PageInput,
        canonical: StageOutcome,
        *,
        gate: AbstractContextManager[object] | None = None,
    ) -> tuple[StageOutcome, PagePartition | None]:
        if self.partitioner is None:
            return StageOutcome(
                "partition",
                stage_fingerprint("partition", "source-only-v1", (canonical.artifact,)),
                StageState.DEFERRED,
                "source-only-v1",
                diagnostic="Source stage only: layout and object semantics have not run.",
            ), None
        fingerprint = stage_fingerprint(
            "partition",
            self.partitioner.fingerprint,
            (canonical.artifact, page.native_svg),
        )
        cached = self.outputs.cached(fingerprint)
        if cached is not None:
            assert cached.artifact is not None
            partition = TypeAdapter(PagePartition).validate_json(
                self.outputs.assets.get(cached.artifact), strict=True
            )
            validate_partition(page, partition)
            return cached, partition
        try:
            with nullcontext() if gate is None else gate:
                partition = self.partitioner.partition(page)
            validate_partition(page, partition)
        except (ValueError, OSError) as error:
            return StageOutcome(
                "partition",
                fingerprint,
                StageState.FAILED,
                self.partitioner.fingerprint,
                diagnostic=str(error),
            ), None
        outcome = self.outputs.cache_output(
            "partition",
            fingerprint,
            self.partitioner.fingerprint,
            TypeAdapter(PagePartition).dump_json(partition),
        )
        return outcome, partition

    def _normalize(
        self, page: PageInput, raw_stage: StageOutcome, raw: PagePartition
    ) -> tuple[StageOutcome, PagePartition]:
        fingerprint = stage_fingerprint(
            "normalized_partition",
            NORMALIZATION_VERSION,
            (raw_stage.artifact, page.native_svg, page.source_manifest_id),
        )
        expected = normalize_partition(page=page, partition=raw)
        cached = self.outputs.cached(fingerprint)
        if cached is not None:
            assert cached.artifact is not None
            if (
                TypeAdapter(PagePartition).validate_json(self.outputs.assets.get(cached.artifact))
                != expected
            ):
                raise ValueError("Normalized layout cache differs from its source rule")
            return cached, expected
        outcome = self.outputs.cache_output(
            "normalized_partition",
            fingerprint,
            NORMALIZATION_VERSION,
            TypeAdapter(PagePartition).dump_json(expected),
        )
        return outcome, expected

    def _object(self, page: PageInput, item: LayoutObject) -> ObjectProcessingRecord:
        if self.object_processor is None:
            return ObjectProcessingRecord(
                item.object_id,
                item.kind,
                (
                    StageOutcome(
                        "ir",
                        stage_fingerprint("ir", "layout-only-v1", (page.source_manifest_id, item)),
                        StageState.DEFERRED,
                        "layout-only-v1",
                        diagnostic="Layout-only phase: semantic object processing has not run.",
                    ),
                ),
            )
        try:
            record = self.object_processor.process(page, item)
            if record.object_id != item.object_id or record.kind is not item.kind:
                raise ValueError("Object result belongs to another layout object")
            return record
        except (ValueError, OSError) as error:
            return ObjectProcessingRecord(
                item.object_id,
                item.kind,
                (
                    StageOutcome(
                        "ir",
                        stage_fingerprint(
                            "ir", "object-processor", (page.source_manifest_id, item)
                        ),
                        StageState.FAILED,
                        "object-processor",
                        diagnostic=str(error),
                    ),
                ),
            )


def _result(future: "Future[_T]") -> _T:
    """``future.result()``, polled so a waiting main thread still sees an interrupt."""
    while True:
        try:
            return future.result(timeout=_POLL_SECONDS)
        except TimeoutError:
            continue
