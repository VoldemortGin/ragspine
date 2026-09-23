"""Selected-page processing uses saved source assets, never the old focus region."""

from hashlib import sha256

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.layout_normalization import (
    NORMALIZATION_VERSION,
    normalize_partition,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
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
    ) -> None:
        self.sources = sources
        self.outputs = outputs
        self.partitioner = partitioner
        self.object_processor = object_processor
        self.normalize_layout = normalize_layout
        self.activate = activate
        self.producer = producer

    def run(
        self, source_manifest_id: str, *, selected_page_indices: tuple[int, ...]
    ) -> tuple[str, ProcessingManifest]:
        source = self.sources.load(source_manifest_id)
        scope = ProcessingScope(
            source_manifest_id,
            source.manifest.source.sha256,
            len(source.manifest.pages),
            selected_page_indices,
        )
        pages: list[PageProcessingRecord] = []
        for page_index in scope.selected_page_indices:
            observed = source.manifest.pages[page_index]
            page = PageInput(
                source_manifest_id,
                scope.source_sha256,
                page_index,
                observed.width,
                observed.height,
                observed.svg,
                read_text_sidecar(self.sources, source, page_index),
            )
            canonical = self._canonical(page)
            partition_stage, partition = self._partition(page, canonical)
            raw_partition_stage = partition_stage if self.normalize_layout else None
            if partition is not None and self.normalize_layout:
                partition_stage, partition = self._normalize(page, partition_stage, partition)
            records = (
                tuple(self._object(page, item) for item in partition.objects)
                if partition is not None
                else ()
            )
            pages.append(
                PageProcessingRecord(
                    page_index, canonical, partition_stage, records, raw_partition_stage
                )
            )
        manifest = ProcessingManifest("processing-v1", scope, self.producer, tuple(pages))
        save = self.outputs.publish if self.activate else self.outputs.save_draft
        return save(manifest, sources=self.sources), manifest

    def _canonical(self, page: PageInput) -> StageOutcome:
        canonical = canonical_page(page)
        payload = TypeAdapter(CanonicalPage).dump_json(canonical)
        fingerprint = stage_fingerprint(
            "canonical", "canonical-source-v1", (sha256(payload).hexdigest(),)
        )
        cached = self.outputs.cached(fingerprint)
        if cached is not None:
            return cached
        artifact = self.outputs.assets.put(payload, media_type="application/json")
        outcome = StageOutcome(
            "canonical",
            fingerprint,
            StageState.SUCCEEDED,
            "canonical-source-v1",
            artifact,
        )
        self.outputs.cache(outcome)
        return outcome

    def _partition(
        self, page: PageInput, canonical: StageOutcome
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
        artifact = self.outputs.assets.put(
            TypeAdapter(PagePartition).dump_json(partition),
            media_type="application/json",
        )
        outcome = StageOutcome(
            "partition",
            fingerprint,
            StageState.SUCCEEDED,
            self.partitioner.fingerprint,
            artifact,
        )
        self.outputs.cache(outcome)
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
        ref = self.outputs.assets.put(
            TypeAdapter(PagePartition).dump_json(expected),
            media_type="application/json",
        )
        outcome = StageOutcome(
            "normalized_partition",
            fingerprint,
            StageState.SUCCEEDED,
            NORMALIZATION_VERSION,
            ref,
        )
        self.outputs.cache(outcome)
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
