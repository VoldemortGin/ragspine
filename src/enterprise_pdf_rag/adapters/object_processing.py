"""Persist source-bound object artifacts without confounding storage and qualification."""

from hashlib import sha256

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.source_objects import source_object_ir
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar
from enterprise_pdf_rag.figures.models import Verification
from enterprise_pdf_rag.processing.models import (
    LayoutObject,
    ObjectKind,
    ObjectProcessingRecord,
    PageInput,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.typed_ir import (
    LiteralQualification,
    ObjectDescription,
)


class ProcessingObjectAdapter:
    def __init__(self, sources: LocalDocumentStore, outputs: ProcessingStore) -> None:
        self.sources = sources
        self.outputs = outputs

    def process(self, page: PageInput, item: LayoutObject) -> ObjectProcessingRecord:
        native = self.sources.get(page.native_svg)
        crop = crop_native_svg(
            native.decode("utf-8"), width=page.width, height=page.height, bbox=item.bbox
        )
        stages = [
            self._save("svg", "native-svg-crop-v1", crop.encode(), page, item, "image/svg+xml")
        ]
        container = (
            item.kind is ObjectKind.GROUP
            and not item.source_span_ids
            and bool(item.child_object_ids)
        )
        observations = TextSidecar(
            "object-source-text-v1",
            page.source_sha256,
            page.page_index,
            tuple(
                span
                for span in page.text.spans
                if span.span_id in item.source_span_ids
                or (
                    container
                    and item.bbox[0] <= span.bbox[0] < span.bbox[2] <= item.bbox[2]
                    and item.bbox[1] <= span.bbox[1] < span.bbox[3] <= item.bbox[3]
                )
            ),
        )
        stages.append(
            self._save(
                "source_text",
                "group-region-occurrences-v1" if container else "source-occurrences-v1",
                TypeAdapter(TextSidecar).dump_json(observations),
                page,
                item,
            )
        )
        if item.kind not in (ObjectKind.TEXT, ObjectKind.LIST, ObjectKind.GROUP):
            raise ValueError("No semantic producer has been configured for this object type")
        source = source_object_ir(page, item)
        ir = self._save(
            "ir",
            "source-typed-ir-v1",
            TypeAdapter[object](type(source.ir)).dump_json(source.ir),
            page,
            item,
        )
        description = self._save(
            "description",
            source.description.producer,
            TypeAdapter(ObjectDescription).dump_json(source.description),
            page,
            item,
        )
        assert (
            ir.artifact is not None
            and description.artifact is not None
            and stages[0].artifact is not None
        )
        if source.description.verification is not Verification.VERIFIED:
            stages.extend(
                (
                    ir,
                    description,
                    StageOutcome(
                        "qualification",
                        sha256(
                            repr(
                                (
                                    "unverified-container-v1",
                                    page.source_manifest_id,
                                    item,
                                )
                            ).encode()
                        ).hexdigest(),
                        StageState.UNAVAILABLE,
                        "source-container-v1",
                        diagnostic="Container child references and regional transcription are retained; grouping and financial relations are unverified and are not indexed.",
                    ),
                )
            )
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        qualification = LiteralQualification(
            item.object_id,
            source.source,
            page.source_manifest_id,
            item.source_span_ids,
            ir.artifact,
            description.artifact,
            stages[0].artifact,
        )
        stages.extend(
            (
                ir,
                description,
                self._save(
                    "qualification",
                    qualification.scope,
                    TypeAdapter(LiteralQualification).dump_json(qualification),
                    page,
                    item,
                ),
            )
        )
        return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))

    def _save(
        self,
        stage: str,
        producer: str,
        payload: bytes,
        page: PageInput,
        item: LayoutObject,
        media_type: str = "application/json",
    ) -> StageOutcome:
        fingerprint = sha256(
            repr(
                (
                    "object-stage-v1",
                    stage,
                    producer,
                    page.source_manifest_id,
                    page.native_svg,
                    item,
                )
            ).encode()
        ).hexdigest()
        ref: AssetRef = self.outputs.assets.put(payload, media_type=media_type)
        outcome = StageOutcome(stage, fingerprint, StageState.SUCCEEDED, producer, ref)
        self.outputs.cache(outcome)
        return outcome
