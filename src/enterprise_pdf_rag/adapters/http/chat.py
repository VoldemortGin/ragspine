"""OpenAI-compatible chat over mounted documents: one bounded model call, verified claims only.

Abstention is a 200 business result. Drifted or corrupt evidence is 409; a missing
provider (query embedder, answer model, reranker) is 503. No error carries a credential,
a provider response body or a store path.
"""

import re
from collections.abc import Iterator
from time import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from enterprise_pdf_rag.adapters.answer_service import (
    AmbiguousDocument,
    AnswerService,
    DependencyUnavailable,
    UnknownDocument,
)
from enterprise_pdf_rag.adapters.document_catalog import (
    CatalogEntry,
    MountedCatalog,
    QueryEmbeddingUnavailable,
)
from enterprise_pdf_rag.adapters.http.chat_schemas import (
    AnswerEnvelope,
    RagChatRequest,
    RagCompletionChunk,
    RagCompletionResponse,
)
from enterprise_pdf_rag.adapters.http.openai_demo import completion_events
from enterprise_pdf_rag.adapters.http.openai_schemas import (
    ChatMessage,
    CompletionChoice,
    ModelInfo,
    ModelList,
)
from enterprise_pdf_rag.answers.models import AnswerRequest, AnswerResult, AnswerStatus, ClaimKind
from enterprise_pdf_rag.answers.query_filters import (
    extract_years,
    shared_title_tokens,
    title_matches,
)
from ragspine.common.evidence.providers.providers import ProviderRequestError
from ragspine.extraction.evidence.figures.chart_qa.models import ChartQueryError, QueryFailure

MODEL_PREFIX = "enterprise-pdf-rag/"
_MODEL_REFERENCE = re.compile(r"^enterprise-pdf-rag/([0-9a-f]{12,64})$")
_LLM_UNCONFIGURED = (
    "Answer model is not configured (OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL); no substitute"
)
_EVIDENCE_CONFLICT = "Document evidence is missing or inconsistent; no fallback"
_DONE = "data: [DONE]\n\n"


def model_id(document_id: str) -> str:
    """The OpenAI model id naming one catalog document: a twelve-digit sha256 prefix."""
    return MODEL_PREFIX + document_id[:12]


def render_message(result: AnswerResult) -> str:
    """The assistant text: the verified answer with numbered citations, or the abstention."""
    if result.status is not AnswerStatus.ANSWERED or result.answer is None:
        reason = "unknown" if result.abstain_reason is None else result.abstain_reason.value
        detail = f": {result.abstain_detail}" if result.abstain_detail else ""
        return f"无法基于已验证证据回答 ({reason}){detail}"
    lines = [result.answer, "", "引用:"]
    for number, claim in enumerate(result.claims, start=1):
        if not claim.citations:
            continue
        citation = claim.citations[0]
        page = citation.page_index + 1
        if claim.kind is ClaimKind.CHART_VALUE:
            elements = ", ".join(f"#{element}" for element in citation.evidence_ids)
            lines.append(
                f"[{number}] p.{page} {citation.field_path} = {claim.text} (svg {elements})"
            )
        else:
            lines.append(f"[{number}] p.{page} {citation.field_path}: “{citation.quote}”")
    return "\n".join(lines)


def _route(mounted: MountedCatalog, question: str) -> str:
    """Pick one mounted document from the question's title words and years (ADR 0013).

    A document is a title candidate when the question names a word of its verified cover
    title that no other mounted title shares, and a year candidate when a year in the
    question is one it prints. Both signals present: their intersection. Exactly one
    survivor is selected; anything else is a 422 that lists the candidates by display name.
    """
    entries = [
        entry for entry in mounted.catalog.documents if entry.document_id in mounted.documents
    ]
    shared = shared_title_tokens(entry.display_title for entry in entries)
    by_title = [
        entry for entry in entries if title_matches(question, entry.display_title, shared=shared)
    ]
    years = set(extract_years(question))
    by_year = [entry for entry in entries if years and years.intersection(entry.years)]
    candidates: list[CatalogEntry]
    if by_title and years:
        candidates = [entry for entry in by_title if entry in by_year]
    else:
        candidates = by_title or by_year
    if len(candidates) == 1:
        return candidates[0].document_id
    names = "; ".join(f"{entry.display_name} ({entry.document_id[:12]})" for entry in entries)
    raise HTTPException(
        422,
        "Document selection required: several documents are mounted and the question "
        f"names {'none' if not candidates else 'more than one'} of them; name one with "
        f"`document` or a model id from /v1/models. Candidates: {names}",
    )


def _select(mounted: MountedCatalog, body: RagChatRequest) -> str | None:
    """Resolve the target document id; ``None`` leaves the choice to catalog uniqueness."""
    reference = body.document
    if reference is None:
        match = _MODEL_REFERENCE.match(body.model)
        if match is None:
            if len(mounted.documents) > 1:
                return _route(mounted, body.messages[-1].content)
            return None
        reference = match.group(1)
    entries = [
        entry for entry in mounted.catalog.documents if entry.document_id.startswith(reference)
    ]
    if not entries:
        raise HTTPException(404, "Unknown document")
    if len(entries) > 1:
        raise HTTPException(422, "Document reference is ambiguous; give more of its sha256")
    entry = entries[0]
    if entry.document_id not in mounted.documents:
        reason = mounted.failures.get(entry.document_id) or entry.reason or entry.retrieval_status
        raise HTTPException(409, f"Document is not mounted: {reason}")
    return entry.document_id


def _answer(service: AnswerService, request: AnswerRequest) -> AnswerResult:
    try:
        return service.answer(request)
    except UnknownDocument:
        raise HTTPException(404, "Unknown document") from None
    except AmbiguousDocument:
        raise HTTPException(
            422,
            "Document selection required: several documents are mounted; "
            "name one with `document` or a model id from /v1/models",
        ) from None
    except DependencyUnavailable as error:
        raise HTTPException(
            503, f"Answer dependency unavailable: {error.code}; no substitute"
        ) from None
    except QueryEmbeddingUnavailable:
        raise HTTPException(503, "Local query embedding is not configured; no substitute") from None
    except ProviderRequestError:
        raise HTTPException(503, "Local model request failed; no retry or substitute") from None
    except ChartQueryError as error:
        if error.code is QueryFailure.UNAVAILABLE_EVIDENCE:
            raise HTTPException(503, "Chart evidence is unavailable; no substitute") from None
        raise HTTPException(409, _EVIDENCE_CONFLICT) from None
    except (ValueError, OSError):
        raise HTTPException(409, _EVIDENCE_CONFLICT) from None


def _events(response: RagCompletionResponse) -> Iterator[str]:
    """Replay the finished answer as SSE, then the envelope as a trailer, then ``[DONE]``."""
    for event in completion_events(response):
        if event == _DONE:
            trailer = RagCompletionChunk(
                id=response.id,
                created=response.created,
                model=response.model,
                choices=(),
                enterprise_pdf_rag=response.enterprise_pdf_rag,
            )
            yield "data: " + trailer.model_dump_json() + "\n\n"
        yield event


def create_chat_router(mounted: MountedCatalog, service: AnswerService | None) -> APIRouter:
    """``/v1/models`` lists the mounted documents; ``/v1/chat/completions`` answers over one.

    ``service`` is ``None`` when no answer model is configured: chat is 503, nothing else.
    """
    router = APIRouter()

    @router.get("/v1/models", response_model=ModelList)
    def models() -> ModelList:
        return ModelList(
            data=tuple(
                ModelInfo(
                    id=model_id(document_id),
                    name=f"{document.entry.display_name} ({document_id[:12]})",
                    owned_by="enterprise-pdf-rag/document-catalog",
                )
                for document_id, document in mounted.documents.items()
            )
        )

    @router.post("/v1/chat/completions", response_model=RagCompletionResponse)
    def completion(body: RagChatRequest) -> RagCompletionResponse | StreamingResponse:
        if body.messages[-1].role != "user":
            raise HTTPException(422, "The last message must be a user question")
        document_id = _select(mounted, body)
        if service is None:
            raise HTTPException(503, _LLM_UNCONFIGURED)
        # Prior turns are data for the prompt; client system prompts are never forwarded.
        history = tuple(
            (message.role, message.content)
            for message in body.messages[:-1]
            if message.role != "system"
        )
        try:
            request = AnswerRequest(
                body.messages[-1].content,
                document_sha256=document_id,
                rerank=body.rerank,
                history=history,
                filters=None if body.filters is None else body.filters.to_domain(),
                page_window=body.page_window,
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        result = _answer(service, request)
        response = RagCompletionResponse(
            id="chatcmpl-" + uuid4().hex,
            created=int(time()),
            model=model_id(result.document_sha256),
            choices=(
                CompletionChoice(
                    message=ChatMessage(role="assistant", content=render_message(result))
                ),
            ),
            enterprise_pdf_rag=AnswerEnvelope.from_domain(result),
        )
        if body.stream:
            # Retrieval, the model call and verification all finished before any SSE byte.
            return StreamingResponse(
                _events(response),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return response

    return router
