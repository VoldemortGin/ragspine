"""Per-document read-only API over pinned mounts; search needs the explicitly injected embedder."""

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from enterprise_pdf_rag.adapters.answer_service import AnswerService
from enterprise_pdf_rag.adapters.document_catalog import (
    CatalogEntry,
    DocumentCatalog,
    MountedCatalog,
    MountedDocument,
    QueryEmbeddingUnavailable,
    mount_catalog,
)
from enterprise_pdf_rag.adapters.http.catalog_schemas import (
    DocumentContextRequest,
    DocumentContextResponse,
    DocumentDetailResponse,
    DocumentListItem,
    DocumentListResponse,
    DocumentSearchRequest,
    DocumentSearchResponse,
)
from enterprise_pdf_rag.adapters.http.chat import create_chat_router
from enterprise_pdf_rag.adapters.http.processing_review import processing_status
from enterprise_pdf_rag.adapters.http.processing_schemas import (
    ProcessingSnapshotResponse,
)
from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient
from enterprise_pdf_rag.adapters.providers import ProviderRequestError
from enterprise_pdf_rag.figures.ports import EmbeddingPort
from enterprise_pdf_rag.processing.models import ProcessingManifest
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge

_EVIDENCE_CONFLICT = "Processing evidence is missing or inconsistent; no fallback"


def _list_item(mounted: MountedCatalog, entry: CatalogEntry) -> DocumentListItem:
    return DocumentListItem.from_entry(
        entry,
        mounted=entry.document_id in mounted.documents,
        mount_error=mounted.failures.get(entry.document_id),
    )


def create_documents_router(mounted: MountedCatalog) -> APIRouter:
    """Routes over one mounted catalog: unknown id 404, unmountable 409, no embedder 503."""
    router = APIRouter()
    catalog = mounted.catalog

    def entry_of(document_id: str) -> CatalogEntry:
        entry = catalog.entry(document_id)
        if entry is None:
            raise HTTPException(404, "Unknown document id")
        return entry

    def mount_of(document_id: str) -> MountedDocument:
        entry = entry_of(document_id)
        mount = mounted.documents.get(document_id)
        if mount is None:
            reason = mounted.failures.get(document_id) or entry.reason or entry.retrieval_status
            raise HTTPException(409, f"Document is not mounted: {reason}")
        return mount

    def checked(mount: MountedDocument) -> ProcessingManifest:
        try:
            return mount.manifest()
        except (ValueError, OSError):
            raise HTTPException(409, _EVIDENCE_CONFLICT) from None

    @router.get("/v1/documents", response_model=DocumentListResponse)
    def list_documents() -> DocumentListResponse:
        return DocumentListResponse(
            ingestion_root=catalog.ingestion_root,
            legacy_roots=catalog.legacy_roots,
            embedding_configured=mounted.embedding_fingerprint is not None,
            embedding_fingerprint=mounted.embedding_fingerprint,
            documents=tuple(_list_item(mounted, entry) for entry in catalog.documents),
            unpublished=catalog.unpublished,
        )

    @router.get("/v1/documents/{document_id}", response_model=DocumentDetailResponse)
    def detail(document_id: str) -> DocumentDetailResponse:
        entry = entry_of(document_id)
        mount = mounted.documents.get(document_id)
        return DocumentDetailResponse(
            document=_list_item(mounted, entry),
            status=None
            if mount is None
            else processing_status(mount.processing_id, checked(mount)),
        )

    @router.get("/v1/documents/{document_id}/manifest", response_model=ProcessingSnapshotResponse)
    def manifest(document_id: str) -> ProcessingSnapshotResponse:
        mount = mount_of(document_id)
        return ProcessingSnapshotResponse(snapshot_id=mount.processing_id, manifest=checked(mount))

    @router.post("/v1/documents/{document_id}/search", response_model=DocumentSearchResponse)
    def search(document_id: str, body: DocumentSearchRequest) -> DocumentSearchResponse:
        mount = mount_of(document_id)
        try:
            hits = mount.search(body.query, limit=body.limit)
        except QueryEmbeddingUnavailable:
            raise HTTPException(
                503, "Local query embedding is not configured; no substitute"
            ) from None
        except ProviderRequestError:
            raise HTTPException(
                503, "Local query embedding failed; no retry or substitute"
            ) from None
        except (ValueError, OSError):
            raise HTTPException(409, _EVIDENCE_CONFLICT) from None
        return DocumentSearchResponse(
            document_id=mount.document_id,
            processing_id=mount.processing_id,
            snapshot_id=mount.retrieval_snapshot_id,
            hits=hits,
        )

    @router.post("/v1/documents/{document_id}/context", response_model=DocumentContextResponse)
    def context(document_id: str, body: DocumentContextRequest) -> DocumentContextResponse:
        mount = mount_of(document_id)
        try:
            result = mount.resolve(
                PinnedRetrievalHit(body.hit.snapshot_id, body.hit.member_id, body.hit.score)
            )
        except (ValueError, OSError):
            raise HTTPException(
                409, "Hit or evidence does not belong to the pinned snapshot"
            ) from None
        return DocumentContextResponse(
            document_id=mount.document_id, processing_id=mount.processing_id, context=result
        )

    return router


def create_documents_app(
    catalog: DocumentCatalog,
    *,
    embedder: EmbeddingPort | None,
    llm: JsonCompletionClient | None = None,
    reranker: ListwiseJudge | None = None,
    verify_every_request: bool = False,
) -> FastAPI:
    """Mount every ready entry once with the shared embedder; ``None`` serves evidence only.

    Chat answers through the bounded ``llm`` shared by every request; without one the chat
    routes are 503. ``reranker`` only serves requests that explicitly ask for reranking.
    ``verify_every_request`` makes each request repeat the whole mount-time verification.
    """
    mounted = mount_catalog(catalog, embedder=embedder, verify_every_request=verify_every_request)
    app = FastAPI(title="Enterprise PDF RAG — document catalog", version="0.1.0.dev0")
    app.include_router(create_documents_router(mounted))
    service = None if llm is None else AnswerService(mounted.documents, llm, reranker=reranker)
    app.include_router(create_chat_router(mounted, service))

    @app.exception_handler(ValueError)
    @app.exception_handler(OSError)
    async def evidence_error(_request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": "Document evidence is unavailable or inconsistent; no fallback"},
        )

    return app
