"""Read-only public schema drift check; changes require an explicit review."""

import json
from pathlib import Path

from pydantic import BaseModel

from enterprise_pdf_rag.adapters.http.chart_qa_schemas import (
    ChartQueryErrorResponse,
    ChartQueryRequest,
    ChartQueryResponse,
)
from enterprise_pdf_rag.adapters.http.chart_qa_v2_schemas import (
    DisplayedChartQueryRequest,
    DisplayedChartQueryResponse,
)
from enterprise_pdf_rag.adapters.http.document_schemas import (
    DocumentSnapshotResponse,
    PageTextResponse,
    SourceChatRequest,
)
from enterprise_pdf_rag.adapters.http.openai_schemas import (
    ChatRequest,
    CompletionChunk,
    CompletionResponse,
    ModelList,
)
from enterprise_pdf_rag.adapters.http.processing_review import (
    ProcessingContextRequest,
    ProcessingContextResponse,
    ProcessingSearchResponse,
)
from enterprise_pdf_rag.adapters.http.processing_schemas import (
    ProcessingSearchRequest,
    ProcessingSnapshotResponse,
    ProcessingStatusResponse,
)
from enterprise_pdf_rag.adapters.http.schemas import (
    ContextRequest,
    ContextResponse,
    DemoRequest,
    DemoResponse,
    ExtractionResponse,
    SearchRequest,
    SearchResponse,
)

ROOT = Path(__file__).resolve().parent.parent.parent
MODELS: tuple[type[BaseModel], ...] = (
    DemoRequest,
    DemoResponse,
    SearchRequest,
    SearchResponse,
    ContextRequest,
    ContextResponse,
    ExtractionResponse,
)


CONTRACTS: dict[str, tuple[type[BaseModel], ...]] = {
    "chart-qa-v1": (ChartQueryRequest, ChartQueryResponse, ChartQueryErrorResponse),
    "chart-qa-v2": (
        DisplayedChartQueryRequest,
        DisplayedChartQueryResponse,
        ChartQueryErrorResponse,
    ),
    "figure-api-v1": MODELS,
    "aia-source-review-v1": (
        SourceChatRequest,
        DocumentSnapshotResponse,
        PageTextResponse,
    ),
    "openai-demo-v1": (ChatRequest, ModelList, CompletionResponse, CompletionChunk),
    "aia-processing-v1": (
        ProcessingSnapshotResponse,
        ProcessingStatusResponse,
        ProcessingSearchRequest,
        ProcessingSearchResponse,
        ProcessingContextRequest,
        ProcessingContextResponse,
    ),
}


def main() -> int:
    for name, models in CONTRACTS.items():
        expected = json.loads(
            (ROOT / "docs" / "enterprise-pdf-rag" / "schemas" / f"{name}.json").read_text()
        )
        actual = {model.__name__: model.model_json_schema() for model in models}
        if expected != actual:
            print(
                f"Public {name} schema changed: review compatibility and the versioned schema before updating the contract."
            )
            return 1
        print(f"Public {name} schema verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
