"""Open WebUI transport for verified demo evidence, with no generative model."""

from collections.abc import Iterator
from time import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from enterprise_pdf_rag.adapters.http.openai_schemas import (
    DEMO_MODEL,
    DEMO_SNAPSHOT,
    ChatMessage,
    ChatRequest,
    CompletionChoice,
    CompletionChunk,
    CompletionDelta,
    CompletionResponse,
    ModelInfo,
    ModelList,
    StreamChoice,
)
from enterprise_pdf_rag.adapters.http.webui_gate import (
    DEMO_QUESTIONS,
    normalize_demo_question,
)
from enterprise_pdf_rag.adapters.runtime import Runtime
from enterprise_pdf_rag.figures.models import ReasoningView


def _render(view: ReasoningView) -> str:
    lines = [
        "**offline-demo | 内部创作的 PDF 图表示例。当前不提供公司财报或真实模型回答。**",
        "",
        "以下内容从检索命中的同一 snapshot 回取 ChartIR 与字段证据。历史聊天不作为事实来源。",
        "",
        "| Series | Year | Value | Unit |",
        "| --- | --- | --- | --- |",
    ]
    for point in view.chart_ir.points:
        lines.append(
            f"| {point.series.text} | {point.category.text} | {point.value.value} | {point.unit.text} |"
        )
    lines.extend(
        (
            "",
            f"来源: 合成演示 PDF 第 {view.svg.source.page_index + 1} 页。数值取自图中的明确标签。",
            "",
            "数值标签的来源位置 (PDF 左上角坐标, 单位 point):",
            "",
            "| Year | 标签坐标 |",
            "| --- | --- |",
        )
    )
    evidence = {field.field_path: field for field in view.evidence}
    for point in view.chart_ir.points:
        field = evidence[f"points.{point.point_id}.value"]
        for element in field.elements:
            lines.append(f"| {point.category.text} | `{element.anchor.bbox}` |")
    lines.extend(
        (
            "",
            "完整 ChartIR、SVG 元素和逐字段证据可通过本地 `/v1/search` 与 `/v1/context` 接口审阅。",
        )
    )
    return "\n".join(lines)


def completion_events(result: CompletionResponse) -> Iterator[str]:
    text = result.choices[0].message.content
    deltas = [CompletionDelta(role="assistant")]
    deltas.extend(
        CompletionDelta(content=text[start : start + 128]) for start in range(0, len(text), 128)
    )
    for delta in deltas:
        chunk = CompletionChunk(
            id=result.id,
            created=result.created,
            model=result.model,
            choices=(StreamChoice(delta=delta),),
        )
        yield "data: " + chunk.model_dump_json(exclude_none=True) + "\n\n"
    last = CompletionChunk(
        id=result.id,
        created=result.created,
        model=result.model,
        choices=(StreamChoice(delta=CompletionDelta(), finish_reason="stop"),),
    )
    yield "data: " + last.model_dump_json(exclude_none=True) + "\n\n"
    yield "data: [DONE]\n\n"


def create_demo_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()
    pinned = runtime.run_demo(query="Revenue", snapshot_id=DEMO_SNAPSHOT).bundle

    @router.get("/v1/models", response_model=ModelList)
    def models() -> ModelList:
        return ModelList(data=(ModelInfo(),))

    @router.post("/v1/chat/completions", response_model=CompletionResponse)
    def completion(body: ChatRequest) -> CompletionResponse | StreamingResponse:
        if body.model != DEMO_MODEL:
            raise HTTPException(404, "Only the explicitly named offline-demo model is available")
        if body.snapshot_id != pinned.snapshot_id:
            raise HTTPException(409, "The demo model is pinned to another snapshot")
        if body.messages[-1].role != "user":
            raise HTTPException(422, "The last message must be a user question")
        question = normalize_demo_question(body.messages[-1].content)
        if question not in DEMO_QUESTIONS:
            raise HTTPException(
                422,
                "This demo has no evidence for that request. Ask: Show demo chart evidence / 展示演示图表证据",
            )
        hits = runtime.service.search(question, snapshot_id=pinned.snapshot_id)
        hit = next(
            (candidate for candidate in hits if candidate.bundle_id == pinned.bundle_id),
            None,
        )
        if hit is None:
            raise HTTPException(409, "Pinned demo evidence is unavailable; no summary fallback")
        view = runtime.service.resolve(hit, snapshot_id=pinned.snapshot_id)
        result = CompletionResponse(
            id="chatcmpl-" + uuid4().hex,
            created=int(time()),
            choices=(
                CompletionChoice(message=ChatMessage(role="assistant", content=_render(view))),
            ),
        )
        if body.stream:
            # Resolve and qualify every dependency before sending SSE headers.
            return StreamingResponse(
                completion_events(result),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return result

    return router
