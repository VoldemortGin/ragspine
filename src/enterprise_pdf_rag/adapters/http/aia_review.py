"""Read-only business profile for the persisted selected source; no chart QA or model calls."""

from html import escape
from time import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from enterprise_pdf_rag.adapters.aia_ingestion import (
    read_region_sidecar,
    read_text_sidecar,
    render_source_page,
    render_source_review,
    source_text_json,
)
from enterprise_pdf_rag.adapters.document_store import (
    LocalDocumentStore,
    manifest_assets,
)
from enterprise_pdf_rag.adapters.http.document_schemas import (
    DocumentSnapshotResponse,
    PageTextResponse,
    SourceChatRequest,
)
from enterprise_pdf_rag.adapters.http.openai_demo import completion_events
from enterprise_pdf_rag.adapters.http.openai_schemas import (
    ChatMessage,
    CompletionChoice,
    CompletionResponse,
    ModelInfo,
    ModelList,
)
from enterprise_pdf_rag.adapters.http.processing_review import (
    create_processing_router,
    processing_summary,
)
from enterprise_pdf_rag.adapters.http.webui_gate import (
    AIA_REVIEW_MODEL,
    source_review_page,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.source_publication import validate_processing_source
from enterprise_pdf_rag.documents.aia import AIA_SPEC
from enterprise_pdf_rag.documents.models import DocumentSnapshot, DocumentSpec
from enterprise_pdf_rag.figures.ports import EmbeddingPort


def _ensure_source(snapshot: DocumentSnapshot, spec: DocumentSpec) -> None:
    manifest = snapshot.manifest
    if (
        manifest.schema_version != "source-ingestion-v1"
        or manifest.source.sha256 != spec.sha256
        or manifest.filename != spec.filename
        or len(manifest.pages) != spec.page_count
        or tuple(page.page_index for page in manifest.pages)
        != tuple(range(spec.page_count))
        or manifest.region.page_index != spec.focus_page_index
        or manifest.region.bbox != spec.focus_bbox
        or manifest.region.native_svg != manifest.pages[spec.focus_page_index].svg
    ):
        raise ValueError(
            "Persisted manifest is not the selected source; no demo fallback"
        )


def create_aia_app(
    store: LocalDocumentStore,
    *,
    spec: DocumentSpec = AIA_SPEC,
    processing: ProcessingStore | None = None,
    embedder: EmbeddingPort | None = None,
) -> FastAPI:
    snapshot = store.load_current()
    _ensure_source(snapshot, spec)
    read_region_sidecar(store, snapshot)
    manifest = snapshot.manifest
    references = {ref.sha256: ref for ref in manifest_assets(manifest)}
    app = FastAPI(
        title="AIA 2026 interim source review — semantics pending", version="0.1.0.dev0"
    )
    processed = None if processing is None else processing.load_current()
    if processed is not None and processing is not None:
        if processed[1].scope.source_manifest_id != snapshot.manifest_id:
            raise ValueError("Processing belongs to another source snapshot")
        app.include_router(
            create_processing_router(store, processing, processed[0], embedder=embedder)
        )

    @app.exception_handler(ValueError)
    @app.exception_handler(OSError)
    async def source_error(_request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": "Source evidence is unavailable or inconsistent; no fallback"
            },
        )

    @app.get("/v1/models", response_model=ModelList)
    def models() -> ModelList:
        return ModelList(
            data=(
                ModelInfo(
                    id=AIA_REVIEW_MODEL,
                    name="AIA 2026 中期业绩 — 原文审阅 / 语义待验证",
                    owned_by="enterprise-pdf-rag/source-review",
                ),
            )
        )

    @app.get("/v1/aia/manifest", response_model=DocumentSnapshotResponse)
    def get_manifest() -> DocumentSnapshotResponse:
        # Pin this process to one immutable manifest; reads still verify live bytes.
        store.load(snapshot.manifest_id)
        return DocumentSnapshotResponse(
            manifest_id=snapshot.manifest_id, manifest=manifest
        )

    @app.get("/v1/aia/pages/{page_number}/text", response_model=PageTextResponse)
    def page_text(page_number: int) -> PageTextResponse:
        if not 1 <= page_number <= len(manifest.pages):
            raise HTTPException(404, "Source page does not exist")
        store.load(snapshot.manifest_id)
        return PageTextResponse(
            sidecar=read_text_sidecar(store, snapshot, page_number - 1)
        )

    @app.get("/v1/aia/assets/{digest}")
    def asset(digest: str) -> Response:
        ref = references.get(digest)
        if ref is None:
            raise HTTPException(
                404, "Asset does not belong to the pinned source manifest"
            )
        return Response(
            content=store.get(ref),
            media_type=ref.media_type,
            headers={
                "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/v1/aia/source.pdf")
    def source_pdf() -> Response:
        return asset(manifest.source.sha256)

    @app.get("/v1/aia/text.json")
    def source_text() -> Response:
        store.load(snapshot.manifest_id)
        return Response(
            content=source_text_json(store, snapshot), media_type="application/json"
        )

    @app.get("/v1/aia/pages/page-{page_number}.html", response_class=HTMLResponse)
    def page_review(page_number: int) -> str:
        if not 1 <= page_number <= len(manifest.pages):
            raise HTTPException(404, "Source page does not exist")
        store.load(snapshot.manifest_id)
        return render_source_page(store, snapshot, page_number - 1)

    @app.get("/v1/aia/review", response_class=HTMLResponse)
    @app.get("/v1/aia/review.html", response_class=HTMLResponse)
    def review() -> str:
        store.load(snapshot.manifest_id)
        return render_source_review(store, snapshot)

    @app.post("/v1/chat/completions", response_model=CompletionResponse)
    def completion(body: SourceChatRequest) -> CompletionResponse | StreamingResponse:
        if body.model != AIA_REVIEW_MODEL:
            raise HTTPException(
                404, "Only the selected AIA source-review model is available"
            )
        if body.snapshot_id is not None and body.snapshot_id != snapshot.manifest_id:
            raise HTTPException(
                409, "Requested source snapshot differs from the pinned manifest"
            )
        if body.messages[-1].role != "user":
            raise HTTPException(422, "The last message must be a source-review request")
        page_number = source_review_page(body.messages[-1].content)
        if page_number is None:
            raise HTTPException(
                422,
                "Only source review is available; ask 查看当前文件 or 查看第25页. Financial QA and ChartIR are pending",
            )
        default_processing = page_number == 0 and processed is not None
        if page_number == 0:
            page_number = (
                manifest.region.page_index + 1
                if processed is None
                else processed[1].scope.physical_pages[0]
            )
        if not 1 <= page_number <= len(manifest.pages):
            raise HTTPException(404, "Source page does not exist")
        store.load(snapshot.manifest_id)
        page = manifest.pages[page_number - 1]
        store.get(page.svg)
        text = read_text_sidecar(store, snapshot, page_number - 1)
        lines = [
            f"**当前文件: {manifest.filename}**",
            "",
            f"已保存原始 PDF 和全部 {len(manifest.pages)} 页的来源资产。本回答固定到同一份已保存的原文件。",
            "",
            "**原文审阅; 图表语义 pending。** 以下是 pdfspine 提取的原文观测,未经 LLM 改写,不是 embedding 描述或财务问答。",
            "",
            f"PDF 第 {page_number} 页原文片段 ({min(40, len(text.spans))}/{len(text.spans)} 个文本span):",
            "",
        ]
        for span in text.spans[:40]:
            lines.append(
                f"> {escape(span.text)}  \n> 来源: 第 {page_number} 页, bbox `{span.bbox}`"
            )
            lines.append("")
        lines.extend(
            (
                "[打开来源审阅与真实图表 SVG](http://127.0.0.1:8766/v1/aia/review)",
                "",
                "缺口: ChartIR、LLM 描述、图表数值/年份/系列关系的源级验证尚未完成; 未运行 embedding/rerank,不依据片段推导财务结论。",
            )
        )
        if processed is not None and processing is not None:
            current = processing.load(processed[0])
            plan = (
                None
                if current.retrieval is None
                else processing.load_retrieval(current.retrieval)[0]
            )
            validate_processing_source(
                sources=store, artifacts=processing.assets, manifest=current, plan=plan
            )
            summary = processing_summary(processed[0], current)
            if default_processing:
                lines = [f"**当前文件: {manifest.filename}**", "", summary]
            else:
                lines = lines[:-2]
                lines.extend(("", summary))
        result = CompletionResponse(
            id="chatcmpl-" + uuid4().hex,
            created=int(time()),
            model=AIA_REVIEW_MODEL,
            choices=(
                CompletionChoice(
                    message=ChatMessage(role="assistant", content="\n".join(lines))
                ),
            ),
        )
        if body.stream:
            return StreamingResponse(
                completion_events(result),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return result

    return app
