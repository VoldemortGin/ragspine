"""A process-local demonstration API with no production fallback or model calls."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from enterprise_pdf_rag.adapters.aia_ingestion import AIA_OUTPUT
from enterprise_pdf_rag.adapters.answer_audit import open_audit_store
from enterprise_pdf_rag.adapters.document_catalog import scan_catalog
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.aia_review import create_aia_app
from enterprise_pdf_rag.adapters.http.documents import create_documents_app
from enterprise_pdf_rag.adapters.http.openai_demo import create_demo_router
from enterprise_pdf_rag.adapters.http.schemas import (
    ContextRequest,
    ContextResponse,
    DemoRequest,
    DemoResponse,
    HitSchema,
    SearchRequest,
    SearchResponse,
)
from enterprise_pdf_rag.adapters.hybrid_search import LocalRerankJudge
from enterprise_pdf_rag.adapters.processing_runtime import PROCESSING_OUTPUT
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.runtime import create_runtime
from enterprise_pdf_rag.figures.models import ExecutionMode, FailureCode, FigureError
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.common.evidence.providers.local_models import (
    LocalEmbeddingAdapter,
    LocalRerankAdapter,
)
from ragspine.common.evidence.providers.providers import (
    ProviderConfigurationError,
    load_llm_config,
    load_local_model_config,
)
from ragspine.common.evidence.settings import get_settings


def create_app(*, mode: ExecutionMode) -> FastAPI:
    runtime = create_runtime(mode=mode)
    app = FastAPI(title="Enterprise PDF RAG — offline figure slice", version="0.1.0.dev0")
    app.include_router(create_demo_router(runtime))

    @app.exception_handler(FigureError)
    async def figure_error_handler(_request: Request, error: FigureError) -> JSONResponse:
        status = 404 if error.code is FailureCode.MISSING_ARTIFACT else 409
        return JSONResponse(
            status_code=status,
            content={"error": {"code": error.code.value, "message": str(error)}},
        )

    @app.post("/v1/demo/ingest", response_model=DemoResponse, status_code=201)
    def demo(body: DemoRequest) -> DemoResponse:
        result = runtime.run_demo(query=body.query, snapshot_id=body.snapshot_id)
        return DemoResponse(
            bundle=result.bundle,
            hits=tuple(HitSchema.from_domain(hit) for hit in result.hits),
            context=result.context,
        )

    @app.post("/v1/search", response_model=SearchResponse)
    def search(body: SearchRequest) -> SearchResponse:
        hits = runtime.service.search(body.query, snapshot_id=body.snapshot_id, limit=body.limit)
        return SearchResponse(
            snapshot_id=body.snapshot_id,
            hits=tuple(HitSchema.from_domain(hit) for hit in hits),
        )

    @app.post("/v1/context", response_model=ContextResponse)
    def context(body: ContextRequest) -> ContextResponse:
        result = runtime.service.resolve(body.hit.to_domain(), snapshot_id=body.snapshot_id)
        return ContextResponse(context=result)

    return app


def create_configured_app() -> FastAPI:
    """Load the explicit API profile from external configuration and saved data."""
    configured = get_settings().execution_mode
    if configured == "unconfigured":
        raise ValueError(
            "Set APP_EXECUTION_MODE explicitly to aia-source-review, document-catalog or offline-demo"
        )
    if configured == "aia-source-review":
        processing = (
            ProcessingStore(PROCESSING_OUTPUT)
            if (PROCESSING_OUTPUT / "current-processing").is_file()
            else None
        )
        try:
            embedder = LocalEmbeddingAdapter(load_local_model_config("embedding"))
        except ProviderConfigurationError:
            embedder = None
        return create_aia_app(
            LocalDocumentStore(AIA_OUTPUT), processing=processing, embedder=embedder
        )
    if configured == "document-catalog":
        settings = get_settings()
        catalog = scan_catalog(settings.ingestion_root, legacy_roots=settings.legacy_document_roots)
        try:
            # Built once and shared by every mount; construction sends no request.
            shared_embedder = LocalEmbeddingAdapter(load_local_model_config("embedding"))
        except ProviderConfigurationError:
            shared_embedder = None
        try:
            # One bounded client for every chat request; construction sends nothing.
            llm = JsonCompletionClient(
                load_llm_config(),
                cache_dir=settings.ingestion_root / "model-cache",
                max_live_calls=settings.answer_max_live_calls,
                timeout=settings.answer_timeout_seconds,
                seed=settings.answer_seed,
            )
        except ProviderConfigurationError:
            llm = None
        try:
            reranker = LocalRerankJudge(LocalRerankAdapter(load_local_model_config("rerank")))
        except ProviderConfigurationError:
            reranker = None
        # The local answer journal: one row per answer, prompt verbatim (ADR-free local
        # file under the ingestion root). Disabled it writes nothing; failing to open it
        # is a warning, never a service that will not start.
        audit = (
            open_audit_store(settings.answer_audit_file) if settings.answer_audit_enabled else None
        )
        return create_documents_app(
            catalog,
            embedder=shared_embedder,
            llm=llm,
            reranker=reranker,
            verify_every_request=settings.verify_every_request,
            audit=audit,
        )
    return create_app(mode=ExecutionMode(configured))
