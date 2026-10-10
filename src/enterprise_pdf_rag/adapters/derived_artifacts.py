"""Derived object artifacts recorded without their bytes, recomputed on demand (ADR 0048).

The SVG crop (``native_crop``, and ``svg`` for every kind but a prepared chart), a chart's
structured figure SVG and both resvg renders (``model_render``) are pure functions of the
source snapshot's page SVG, its text sidecar and the object's layout geometry. A store
written with ``persist_derived_artifacts=false`` keeps their stage entries and digests, plus
one ``derived-artifacts/<sha256>.json`` record per digest, but not their bytes. A reader
re-derives them, and ``derived_artifact`` hands the result out only when it is exactly the
recorded digest; anything else is ``DerivedArtifactDrift``, never silently used.
"""

import json
import re
from collections.abc import Callable, Iterator
from hashlib import sha256

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure, prepare_figure
from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg
from enterprise_pdf_rag.adapters.visual_semantics import _prepare as prepare_visual
from ragspine.common.observability.trace import emit_trace
from ragspine.extraction.evidence.document.models import AssetRef
from ragspine.extraction.evidence.page.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    PagePartition,
    PageProcessingRecord,
    ProcessingScope,
    StageOutcome,
)

DERIVED_MEDIA_TYPES = frozenset({"image/svg+xml", "image/png"})
_MARKER_SCHEMA = "derived-artifact-v1"


class DerivedArtifactDrift(ValueError):
    """A recomputed derived artifact is not the recorded digest: tampering or version drift."""

    code = "derived_artifact_drift"


def _marker_name(digest: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("A derived artifact is recorded under its SHA-256 digest")
    return f"derived-artifacts/{digest}.json"


def _marker(ref: AssetRef) -> bytes:
    return json.dumps(
        {
            "schema_version": _MARKER_SCHEMA,
            "sha256": ref.sha256,
            "media_type": ref.media_type,
            "byte_length": ref.byte_length,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def recomputable(assets: LocalDocumentStore, ref: AssetRef) -> bool:
    """True when this store recorded ``ref`` as derived and recomputed on demand."""
    if ref.media_type not in DERIVED_MEDIA_TYPES:
        return False
    return assets.backend.record(_marker_name(ref.sha256)) == _marker(ref)


def mark_recomputable(assets: LocalDocumentStore, ref: AssetRef) -> None:
    """Record ``ref`` as derived and unwritten (idempotent; the same bytes every time)."""
    if not recomputable(assets, ref):
        assets.backend.put_record(_marker_name(ref.sha256), _marker(ref))


def derived_artifact(
    assets: LocalDocumentStore, ref: AssetRef, recompute: Callable[[], bytes]
) -> bytes:
    """The bytes of a derived artifact: stored ones read as before, else recomputed + checked.

    A recomputed payload that is not ``ref`` (length and SHA-256) is never used: it raises
    ``DerivedArtifactDrift`` and traces ``derived_artifact_drift`` (media type only).
    """
    if not recomputable(assets, ref):
        return assets.get(ref)
    payload = recompute()
    if (len(payload), sha256(payload).hexdigest()) != (ref.byte_length, ref.sha256):
        emit_trace(event="derived_artifact_drift", media_type=ref.media_type)
        raise DerivedArtifactDrift(
            "Recomputed derived artifact differs from its recorded digest "
            "(tampered store, or a changed SVG crop / renderer version)"
        )
    return payload


DERIVED_STAGES = frozenset({"native_crop", "svg", "model_render"})


def object_stage_bytes(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    page: PageProcessingRecord,
    stage: StageOutcome,
    object_id: str,
) -> bytes:
    """A stage's bytes: read as before, or — derived and unwritten — recomputed and checked."""
    artifact = stage.artifact
    if artifact is None:
        raise ValueError("Successful stage artifact is unavailable")
    if stage.stage not in DERIVED_STAGES:
        return assets.get(artifact)
    return derived_artifact(
        assets,
        artifact,
        lambda: recompute_object_stage(
            sources, assets, scope, page, object_id, stage.stage, artifact
        ),
    )


def recompute_object_stage(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    page: PageProcessingRecord,
    object_id: str,
    stage: str,
    artifact: AssetRef,
) -> bytes:
    """The recipe output equal to ``artifact``, else the stage's primary recipe output.

    A stage name can stand for two recipes (a chart's ``svg`` is its structured figure SVG,
    or the plain crop when the figure could not be prepared), so each is tried in order and
    the first one hitting the recorded digest wins; when none does, the primary one comes
    back and the caller's digest check refuses it.
    """
    source = sources.load(scope.source_manifest_id)
    page_record = source.manifest.pages[page.page_index]
    page_input = PageInput(
        scope.source_manifest_id,
        scope.source_sha256,
        page.page_index,
        page_record.width,
        page_record.height,
        page_record.svg,
        read_text_sidecar(sources, source, page.page_index),
    )
    if page.partition.artifact is None:
        raise ValueError("A derived object artifact needs its page's layout")
    partition = TypeAdapter(PagePartition).validate_json(assets.get(page.partition.artifact))
    item = next((obj for obj in partition.objects if obj.object_id == object_id), None)
    if item is None:
        raise ValueError("Derived object artifact names an object outside its page layout")
    native = sources.get(page_record.svg)
    primary: bytes | None = None
    for recipe in _recipes(stage, page_input, native, item):
        try:
            payload = recipe()
        except ValueError:
            continue
        if (len(payload), sha256(payload).hexdigest()) == (artifact.byte_length, artifact.sha256):
            return payload
        primary = payload if primary is None else primary
    if primary is None:
        raise ValueError(f"No recipe recomputes the derived stage {stage!r}")
    return primary


def _recipes(
    stage: str, page: PageInput, native: bytes, item: LayoutObject
) -> Iterator[Callable[[], bytes]]:
    def crop() -> bytes:
        return crop_native_svg(
            native.decode("utf-8"), width=page.width, height=page.height, bbox=item.bbox
        ).encode()

    def figure_svg() -> bytes:
        return _figure(page, native, item).svg.svg.encode()

    def figure_png() -> bytes:
        return _figure(page, native, item).rendered.png

    def visual_png() -> bytes:
        return prepare_visual(page=page, item=item, native_svg=native).model_png

    chart = item.kind is ObjectKind.CHART
    if stage == "native_crop":
        yield crop
    elif stage == "svg":
        if chart:
            yield figure_svg
        yield crop
    elif stage == "model_render":
        yield figure_png if chart else visual_png


def _figure(page: PageInput, native: bytes, item: LayoutObject) -> PreparedFigure:
    return prepare_figure(
        page=page,
        native_svg=native,
        bbox=item.bbox,
        region_id=item.extraction_region_id or item.object_id,
        context_span_ids=item.context_span_ids,
    )
