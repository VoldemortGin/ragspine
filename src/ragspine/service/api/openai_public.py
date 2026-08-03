"""OpenAI Chat Completions API 形状克隆：现有 openai SDK / 客户端零改动直连本服务。

生态里事实上的互通标准是 OpenAI 的 `/v1/chat/completions`，而非任何一家 RAG 框架的私有
Python API（LightRAG 亦是靠 Ollama 兼容路由获得客户端生态）。故本兼容层对齐 OpenAI 官方形状，
让 Open WebUI / Cherry Playground / LangChain / LlamaIndex / Dify 的 OpenAI-compatible
provider 等客户端把 RAGSpine 当作一个模型直接接上。

对外形状以 OpenAI 官方为准：
- POST /v1/chat/completions：blocking -> {id, object:"chat.completion", created, model,
  choices[{index, message{role,content}, finish_reason}], usage}；stream=true -> SSE
  `data: {...}\\n\\n` 的 `chat.completion.chunk` 序列（首帧带 delta.role，末帧带
  finish_reason），以 `data: [DONE]` 收尾。
- GET /v1/models：{object:"list", data:[{id, object:"model", created, owned_by}]}。
- 错误体 {error:{message, type, param, code}}：400 invalid_request_error / 500 server_error。

**溯源不因套形状而丢失**：`route` 与 `sources` 走非标准的顶层 `ragspine` 扩展字段带出
（流式则挂在末帧）。OpenAI 客户端一律忽略未知字段，故兼容性不受影响，而家族「答案可溯源」
的不变量得以保留——绝不为了迎合别人的签名而把来源丢掉。

**反编造纪律与 /v1/ask/stream 同款**：整条守卫链在流打开【之前】跑到完成，生成器只回放已算好
的字符串，零 provider/store 访问；守卫阶段任何失败 → 正常 JSON 错误，绝不半开流。

messages 映射：最后一条 role=="user" 的消息为 question，其之前的 user/assistant 轮次为
history（与 answer_question(history=) 语义一致——只作生成上下文，绝不产生新证据）。system
消息被【刻意忽略】：本服务的系统提示由受控 profile 决定，不接受客户端改写（防提示注入）。

本文件自包含（schemas 不进 schemas.py，路由不进 routes.py）；app.py 仅 include_router。
"""

import json
import time
import uuid
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ragspine.agent.agent import answer_question
from ragspine.agent.decompose import make_decomposer
from ragspine.agent.llm_provider import LLMProvider, iter_text_chunks
from ragspine.agent.query_transform import make_adaptive_decomposer
from ragspine.common.observability import emit_trace, new_request_id
from ragspine.service.api.dependencies import get_config, get_faq_cache, get_provider
from ragspine.service.config import ServiceConfig, open_fact_store, open_narrative_retriever
from ragspine.service.faq.faq_cache import FAQCache

router = APIRouter()

ConfigDep = Annotated[ServiceConfig, Depends(get_config)]
ProviderDep = Annotated[LLMProvider, Depends(get_provider)]
FAQCacheDep = Annotated[FAQCache, Depends(get_faq_cache)]

# 对外模型名：客户端 model 字段原样回显，但服务端只有这一个逻辑模型。
MODEL_ID = "ragspine"

_ROLE_USER = "user"
_ROLE_ASSISTANT = "assistant"


class ChatMessage(BaseModel):
    role: str
    content: str = ""


class ChatCompletionRequest(BaseModel):
    """OpenAI 请求体子集：仅取本服务真正会用到的字段，其余（temperature / top_p / n /
    max_tokens 等）容忍并忽略——RAGSpine 的生成参数由服务端受控配置决定，不由客户端改写。"""

    model: str = MODEL_ID
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False

    model_config = {"extra": "ignore"}


def _error(status: int, message: str, *, type_: str, param: str | None = None) -> JSONResponse:
    """OpenAI 官方错误体形状。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "param": param, "code": None}},
    )


def _split_messages(messages: list[ChatMessage]) -> tuple[str, list[tuple[str, str]]]:
    """拆成 (question, history)：最后一条 user 为问题，其前的 user/assistant 轮次为历史。

    system 消息一律丢弃（服务端系统提示受控，不接受客户端改写）。
    """
    turns = [m for m in messages if m.role in {_ROLE_USER, _ROLE_ASSISTANT}]
    last_user = next(
        (index for index in range(len(turns) - 1, -1, -1) if turns[index].role == _ROLE_USER),
        None,
    )
    if last_user is None:
        return "", []
    question = turns[last_user].content
    history = [(turn.role, turn.content) for turn in turns[:last_user]]
    return question, history


def _answer(
    question: str,
    history: list[tuple[str, str]],
    config: ServiceConfig,
    provider: LLMProvider,
    faq_cache: FAQCache,
    request_id: str,
) -> tuple[str, str, list[dict[str, object]]]:
    """跑完整守卫链，返回 (answer, route, sources)。与 /v1/ask 同一条链，不另写逻辑。"""
    hit = faq_cache.lookup(question, reference_date=None)
    if hit is not None:
        emit_trace(
            request_id=request_id,
            cache_hit=True,
            faq_id=hit.item_id,
            faq_version=hit.version,
        )
        sources: list[dict[str, object]] = [{"doc": hit.source}] if hit.source else []
        return hit.answer, "faq", sources

    with (
        open_fact_store(config) as store,
        open_narrative_retriever(config, provider) as retriever,
    ):
        if config.adaptive != "none":
            decomposer = make_adaptive_decomposer(config.adaptive, provider=provider)
        else:
            decomposer = make_decomposer(config.query_decompose, provider=provider)
        result = answer_question(
            question,
            store,
            provider,
            reference_date=None,
            narrative_retriever=retriever,
            decomposer=decomposer,
            history=history or None,
        )
    emit_trace(request_id=request_id, cache_hit=False, route=result.route)
    return result.answer, result.route, list(result.sources)


def _usage(question: str, answer: str) -> dict[str, int]:
    """OpenAI 客户端普遍要求 usage 存在。RAGSpine 不按 token 计费，故给出确定性的
    字符数近似——刻意【不】伪造成真实 tokenizer 计数，字段语义在文档中标注为近似值。"""
    prompt_tokens = len(question)
    completion_tokens = len(answer)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _extension(request_id: str, route: str, sources: list[dict[str, object]]) -> dict[str, Any]:
    return {"request_id": request_id, "route": route, "sources": sources}


def _sse_frame(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/v1/chat/completions", response_model=None)
def chat_completions(
    req: ChatCompletionRequest,
    config: ConfigDep,
    provider: ProviderDep,
    faq_cache: FAQCacheDep,
) -> JSONResponse | StreamingResponse:
    request_id = new_request_id()
    question, history = _split_messages(req.messages)
    if not question.strip():
        return _error(
            400,
            "messages must contain at least one user message with content",
            type_="invalid_request_error",
            param="messages",
        )

    try:
        answer, route, sources = _answer(question, history, config, provider, faq_cache, request_id)
    except Exception:  # 防御性兜底：绝不泄露 traceback，也绝不半开流
        return _error(500, "internal error", type_="server_error")

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    extension = _extension(request_id, route, sources)

    if not req.stream:
        return JSONResponse(
            content={
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": req.model or MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": _ROLE_ASSISTANT, "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": _usage(question, answer),
                "ragspine": extension,
            }
        )

    def gen() -> Iterator[str]:
        # 仅回放已算好的守卫值，零 provider/store 访问（同 /v1/ask/stream 纪律）。
        base = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": req.model or MODEL_ID,
        }
        yield _sse_frame(
            {
                **base,
                "choices": [
                    {"index": 0, "delta": {"role": _ROLE_ASSISTANT}, "finish_reason": None}
                ],
            }
        )
        for chunk in iter_text_chunks(answer):
            yield _sse_frame(
                {
                    **base,
                    "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                }
            )
        yield _sse_frame(
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": _usage(question, answer),
                "ragspine": extension,
            }
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/v1/models", response_model=None)
def list_models() -> JSONResponse:
    """OpenAI 模型列表：本服务只暴露一个逻辑模型。"""
    return JSONResponse(
        content={
            "object": "list",
            "data": [
                {
                    "id": MODEL_ID,
                    "object": "model",
                    "created": 0,
                    "owned_by": MODEL_ID,
                }
            ],
        }
    )
