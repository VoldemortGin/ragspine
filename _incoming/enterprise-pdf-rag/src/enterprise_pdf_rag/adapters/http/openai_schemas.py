"""A deliberately small, versioned OpenAI-compatible demo boundary."""

from typing import Literal

from pydantic import Field

from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel

DEMO_MODEL = "enterprise-pdf-rag-offline-demo-v1"
DEMO_SNAPSHOT = "webui-demo-v1"


class ChatMessage(BoundaryModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=16000)


class StreamOptions(BoundaryModel):
    include_usage: bool = False


class ChatRequest(BoundaryModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1, max_length=32)
    stream: bool = False
    stream_options: StreamOptions | None = None
    snapshot_id: str = DEMO_SNAPSHOT


class ModelInfo(BoundaryModel):
    id: str = DEMO_MODEL
    name: str = "Offline demo — authored PDF figure evidence only"
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "enterprise-pdf-rag/offline-demo"


class ModelList(BoundaryModel):
    object: Literal["list"] = "list"
    data: tuple[ModelInfo, ...]


class CompletionChoice(BoundaryModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Literal["stop"] = "stop"


class CompletionResponse(BoundaryModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str = DEMO_MODEL
    choices: tuple[CompletionChoice, ...]


class CompletionDelta(BoundaryModel):
    role: Literal["assistant"] | None = None
    content: str | None = None


class StreamChoice(BoundaryModel):
    index: int = 0
    delta: CompletionDelta
    finish_reason: Literal["stop"] | None = None


class CompletionChunk(BoundaryModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str = DEMO_MODEL
    choices: tuple[StreamChoice, ...]
    # No tokenizer or paid model ran; do not invent token-usage statistics.
    usage: None = None
