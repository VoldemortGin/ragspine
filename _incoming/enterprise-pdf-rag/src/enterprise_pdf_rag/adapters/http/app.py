"""A process-local demonstration API with no production fallback or model calls."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from enterprise_pdf_rag.adapters.aia_ingestion import AIA_OUTPUT
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.aia_review import create_aia_app
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
from enterprise_pdf_rag.adapters.local_models import LocalEmbeddingAdapter
from enterprise_pdf_rag.adapters.processing_runtime import PROCESSING_OUTPUT
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.providers import (
    ProviderConfigurationError,
    load_local_model_config,
)
from enterprise_pdf_rag.adapters.runtime import create_runtime
from enterprise_pdf_rag.core.settings import get_settings
from enterprise_pdf_rag.figures.models import ExecutionMode, FailureCode, FigureError


def create_app(*, mode: ExecutionMode) -> FastAPI:
    runtime = create_runtime(mode=mode)
    app = FastAPI(
        title="Enterprise PDF RAG — offline figure slice", version="0.1.0.dev0"
    )
    app.include_router(create_demo_router(runtime))

    @app.exception_handler(FigureError)
    async def figure_error_handler(
        _request: Request, error: FigureError
    ) -> JSONResponse:
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
        hits = runtime.service.search(
            body.query, snapshot_id=body.snapshot_id, limit=body.limit
        )
        return SearchResponse(
            snapshot_id=body.snapshot_id,
            hits=tuple(HitSchema.from_domain(hit) for hit in hits),
        )

    @app.post("/v1/context", response_model=ContextResponse)
    def context(body: ContextRequest) -> ContextResponse:
        result = runtime.service.resolve(
            body.hit.to_domain(), snapshot_id=body.snapshot_id
        )
        return ContextResponse(context=result)

    return app


def create_configured_app() -> FastAPI:
    """Load the explicit API profile from external configuration and saved data."""
    configured = get_settings().execution_mode
    if configured == "unconfigured":
        raise ValueError(
            "Set APP_EXECUTION_MODE explicitly to aia-source-review or offline-demo"
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
    return create_app(mode=ExecutionMode(configured))
