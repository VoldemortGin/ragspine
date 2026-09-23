"""Read-only first-twenty artifacts and snapshot-bound retrieval; no model inference."""

import re
from hashlib import sha256

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import Field

from enterprise_pdf_rag.adapters.chart_qa import StoredChartResolver
from enterprise_pdf_rag.adapters.chart_qa_displayed import StoredDisplayResolver
from enterprise_pdf_rag.adapters.chart_qa_evaluation import read_evaluation
from enterprise_pdf_rag.adapters.chart_qa_v2_evaluation import read_bar_evaluation
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.chart_qa import create_chart_qa_router
from enterprise_pdf_rag.adapters.http.processing_schemas import (
    ProcessingSearchRequest,
    ProcessingSnapshotResponse,
    ProcessingStatusResponse,
)
from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.adapters.processing_retrieval import (
    ProcessingRetrieval,
    resolve_processing_context,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.source_publication import validate_processing_source
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit, RetrievalContext
from ragspine.common.evidence.providers.providers import ProviderRequestError
from ragspine.extraction.evidence.figures.chart_qa.displayed_service import (
    DisplayedChartQAService,
)
from ragspine.extraction.evidence.figures.chart_qa.service import ChartQAService
from ragspine.extraction.evidence.figures.ports import EmbeddingPort
from ragspine.extraction.evidence.page.models import ProcessingManifest


class ProcessingSearchResponse(BoundaryModel):
    processing_id: str
    snapshot_id: str
    hits: tuple[PinnedRetrievalHit, ...]


class RetrievalHitInput(BoundaryModel):
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    member_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    score: float = Field(allow_inf_nan=False)


class ProcessingContextRequest(BoundaryModel):
    processing_id: str
    hit: RetrievalHitInput


class ProcessingContextResponse(BoundaryModel):
    processing_id: str
    context: RetrievalContext


class RetrievalExample(BoundaryModel):
    processing_id: str
    query: str
    results: ProcessingSearchResponse
    contexts: tuple[ProcessingContextResponse, ...]


def processing_status(processing_id: str, manifest: ProcessingManifest) -> ProcessingStatusResponse:
    objects = tuple(item for page in manifest.pages for item in page.objects)
    stages = (
        *(stage for item in objects for stage in item.stages),
        *(page.metadata for page in manifest.pages if page.metadata is not None),
    )
    return ProcessingStatusResponse(
        processing_id=processing_id,
        source_manifest_id=manifest.scope.source_manifest_id,
        source_page_count=manifest.scope.source_page_count,
        selected_physical_pages=manifest.scope.physical_pages,
        object_count=len(objects),
        ir_artifacts=sum(stage.stage == "ir" and stage.artifact is not None for stage in stages),
        description_artifacts=sum(
            stage.stage == "description" and stage.artifact is not None for stage in stages
        ),
        failed_stages=sum(stage.state == "failed" for stage in stages),
        unavailable_stages=sum(stage.state == "unavailable" for stage in stages),
        deferred_stages=sum(stage.state == "deferred" for stage in stages),
        qualified_claim_count=sum(item.qualified_claim_count for item in objects),
        retrieval_snapshot_id=None
        if manifest.retrieval is None
        else manifest.retrieval.snapshot_id,
    )


def processing_summary(processing_id: str, manifest: ProcessingManifest) -> str:
    state = processing_status(processing_id, manifest)
    pages = (
        "1-20"
        if state.selected_physical_pages == tuple(range(1, 21))
        else ", ".join(map(str, state.selected_physical_pages))
    )
    retrieval = (
        ""
        if manifest.retrieval is None
        else "\n\n已保存固定检索快照。有限图表标签资格不等于数值 QA 资格;原始 ChartIR 可审阅,未验证数值关系在检索投影中保持 unknown。排序质量另行验证。\n\n[查看真实本地检索、重排与证据回填运行记录(含失败历史)](http://127.0.0.1:8766/v1/processing/review/review.html#retrieval)"
    )
    return (
        f"已保存 {state.source_page_count} 页源资产;本次处理物理页 {pages}。\n\n识别 {state.object_count} 个对象;实际保存 {state.ir_artifacts} 份 typed IR 与 {state.description_artifacts} 份独立描述/逐字原文 projection。失败阶段 {state.failed_stages},unavailable 阶段 {state.unavailable_stages},deferred 阶段 {state.deferred_stages}。\n\n模型推断不等于验证。当前获准数值 claim:{state.qualified_claim_count};原文转录不证明金融关系。\n\n[打开前20页处理结果与各对象 SVG / IR / 描述 / 诊断](http://127.0.0.1:8766/v1/processing/review/review.html)"
        + retrieval
        + "\n\n已验证的查询仅通过结构化 `POST /v1/queries` 执行:v1 环形图查值/同口径百分点差;v2 柱状图只读原文显示值,不做跨期计算。当前聊天仍用于来源和产物审阅,不开放任意金融问答。"
    )


def create_processing_router(
    sources: LocalDocumentStore,
    outputs: ProcessingStore,
    processing_id: str,
    *,
    embedder: EmbeddingPort | None = None,
) -> APIRouter:
    router = APIRouter()
    pinned = outputs.load(processing_id)
    router.include_router(
        create_chart_qa_router(
            ChartQAService(StoredChartResolver(sources, outputs, processing_id=processing_id)),
            displayed_service=DisplayedChartQAService(
                StoredDisplayResolver(sources, outputs, processing_id=processing_id)
            ),
        )
    )
    root = (outputs.root / "runs" / processing_id).resolve()

    def checked() -> ProcessingManifest:
        try:
            manifest = outputs.load(processing_id)
            if manifest != pinned:
                raise ValueError("Immutable processing manifest changed")
            plan = (
                None
                if manifest.retrieval is None
                else outputs.load_retrieval(manifest.retrieval)[0]
            )
            validate_processing_source(
                sources=sources, artifacts=outputs.assets, manifest=manifest, plan=plan
            )
            return manifest
        except (ValueError, OSError):
            raise HTTPException(
                409, "Processing evidence is missing or inconsistent; no fallback"
            ) from None

    @router.get("/v1/processing/status", response_model=ProcessingStatusResponse)
    def status() -> ProcessingStatusResponse:
        return processing_status(processing_id, checked())

    @router.get("/v1/processing/manifest", response_model=ProcessingSnapshotResponse)
    def manifest_response() -> ProcessingSnapshotResponse:
        return ProcessingSnapshotResponse(snapshot_id=processing_id, manifest=checked())

    @router.get("/v1/processing/review/{relative:path}")
    def review(relative: str) -> Response:
        checked()
        if (
            re.fullmatch(
                r"(?:review\.html|manifest\.json|coverage\.json|retrieval-(?:example|validation)\.json|retrieval-evaluations/[0-9a-f]{64}/(?:retrieval-(?:example|validation)|evaluation)\.json|retrieval-controls/[0-9a-f]{64}/controls\.json|chart-qa-evaluations/[0-9a-f]{64}/(?:gold|observations|report)\.json|chart-qa-v2-evaluations/[0-9a-f]{64}/(?:gold|targets|observations|report)\.json|page-\d{3}/(?:[a-z_.-]+\.(?:html|json)|objects/object-[0-9a-f]{20}/[a-z_-]+\.(?:html|json|svg|png)))",
                relative,
            )
            is None
        ):
            raise HTTPException(404, "Unknown processing review artifact")
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "Processing review artifact is unavailable")
        content = target.read_bytes()
        if relative.startswith("chart-qa-v2-evaluations/"):
            try:
                if pinned.retrieval is None:
                    raise ValueError("Missing retrieval release")
                read_bar_evaluation(
                    target.parent,
                    processing_id=processing_id,
                    snapshot_id=pinned.retrieval.snapshot_id,
                )
            except (OSError, ValueError):
                raise HTTPException(
                    409,
                    "Displayed ChartQA evaluation evidence is missing or inconsistent",
                ) from None
        if relative.startswith("chart-qa-evaluations/"):
            try:
                read_evaluation(target.parent)
            except (OSError, ValueError):
                raise HTTPException(
                    409, "ChartQA evaluation evidence is missing or inconsistent"
                ) from None
        if (
            relative.startswith("retrieval-controls/")
            and sha256(content).hexdigest() != target.parent.name
        ):
            raise HTTPException(409, "Rerank service control evidence digest mismatch")
        content_type = {
            ".html": "text/html",
            ".json": "application/json",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }[target.suffix]
        return Response(
            content,
            media_type=content_type,
            headers={
                "Content-Security-Policy": "sandbox allow-popups; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/v1/processing/search", response_model=ProcessingSearchResponse)
    def search(body: ProcessingSearchRequest) -> ProcessingSearchResponse:
        manifest = checked()
        if body.processing_id != processing_id or manifest.retrieval is None:
            raise HTTPException(
                409,
                "The requested processing snapshot has no matching qualified retrieval release",
            )
        if embedder is None:
            raise HTTPException(503, "Local query embedding is not configured; no substitute")
        engine = ProcessingRetrieval(sources, outputs, embedder)
        try:
            hits = engine.search(manifest.retrieval, body.query, limit=body.limit)
        except ProviderRequestError:
            raise HTTPException(
                503, "Local query embedding failed; no retry or substitute"
            ) from None
        return ProcessingSearchResponse(
            processing_id=processing_id,
            snapshot_id=manifest.retrieval.snapshot_id,
            hits=hits,
        )

    @router.post("/v1/processing/context", response_model=ProcessingContextResponse)
    def context(body: ProcessingContextRequest) -> ProcessingContextResponse:
        manifest = checked()
        if body.processing_id != processing_id or manifest.retrieval is None:
            raise HTTPException(
                409,
                "The requested processing snapshot has no matching qualified retrieval release",
            )
        try:
            result = resolve_processing_context(
                sources,
                outputs,
                manifest.retrieval,
                PinnedRetrievalHit(body.hit.snapshot_id, body.hit.member_id, body.hit.score),
            )
        except (ValueError, OSError):
            raise HTTPException(
                409, "Hit or evidence does not belong to the fixed snapshot"
            ) from None
        return ProcessingContextResponse(processing_id=processing_id, context=result)

    return router
