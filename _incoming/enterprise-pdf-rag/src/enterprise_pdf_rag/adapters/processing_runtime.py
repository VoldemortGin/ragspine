"""Composition for explicitly budgeted first-twenty-page source processing."""

from typing import Literal

from enterprise_pdf_rag.adapters.aia_ingestion import AIA_OUTPUT
from enterprise_pdf_rag.adapters.aia_processing import ProcessingPipeline
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient
from enterprise_pdf_rag.adapters.page_partition import ModelPagePartitioner
from enterprise_pdf_rag.adapters.processing_export import export_processing_review
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.providers import load_llm_config
from enterprise_pdf_rag.adapters.semantic_objects import SemanticObjectAdapter

PROCESSING_OUTPUT = AIA_OUTPUT / "pages-001-020"


class ProcessingRunSummary(BoundaryModel):
    processing_id: str
    source_page_count: int
    selected_physical_pages: tuple[int, ...]
    layout_succeeded_pages: int
    object_count: int
    semantic_status: str
    review_path: str
    live_call_count: int = 0


def process_aia_layout(
    *,
    physical_pages: tuple[int, ...],
    max_live_calls: int,
    timeout: float = 180.0,
    retry_failed: bool = False,
) -> ProcessingRunSummary:
    if (
        not physical_pages
        or len(set(physical_pages)) != len(physical_pages)
        or any(not 1 <= page <= 20 for page in physical_pages)
    ):
        raise ValueError("Layout scope must select distinct physical pages 1-20")
    if not 0 <= max_live_calls <= len(physical_pages):
        raise ValueError(
            "Layout call budget cannot exceed the number of selected pages"
        )
    sources = LocalDocumentStore(AIA_OUTPUT)
    source = sources.load_current()
    outputs = ProcessingStore(PROCESSING_OUTPUT)
    client = JsonCompletionClient(
        load_llm_config(),
        cache_dir=PROCESSING_OUTPUT / "model-cache",
        max_live_calls=max_live_calls,
        timeout=timeout,
        retry_failed=retry_failed,
    )
    pipeline = ProcessingPipeline(
        sources, outputs, ModelPagePartitioner(client, sources), None
    )
    snapshot_id, manifest = pipeline.run(
        source.manifest_id,
        selected_page_indices=tuple(sorted(page - 1 for page in physical_pages)),
    )
    review = export_processing_review(sources, outputs, snapshot_id)
    return ProcessingRunSummary(
        processing_id=snapshot_id,
        source_page_count=manifest.scope.source_page_count,
        selected_physical_pages=manifest.scope.physical_pages,
        layout_succeeded_pages=sum(
            page.partition.state == "succeeded" for page in manifest.pages
        ),
        object_count=sum(len(page.objects) for page in manifest.pages),
        semantic_status="deferred: layout review only; no object semantic processing or embedding has run",
        review_path=str(review),
        live_call_count=client.live_call_count,
    )


def process_aia_semantics(
    *,
    physical_pages: tuple[int, ...],
    max_live_calls: int,
    timeout: float = 180.0,
    retry_failed: bool = False,
    qualification_policy: Literal["none", "source-labels-only", "donut"] = "none",
    description_corrections: tuple[str, ...] = (),
    chart_corrections: tuple[str, ...] = (),
) -> ProcessingRunSummary:
    if (
        not physical_pages
        or len(set(physical_pages)) != len(physical_pages)
        or any(not 1 <= page <= 20 for page in physical_pages)
    ):
        raise ValueError("Semantic scope must select distinct physical pages 1-20")
    if not 0 <= max_live_calls <= 200:
        raise ValueError("Semantic call budget must be between 0 and 200")
    sources = LocalDocumentStore(AIA_OUTPUT)
    source = sources.load_current()
    outputs = ProcessingStore(PROCESSING_OUTPUT)
    config = load_llm_config()
    layout_client = JsonCompletionClient(
        config,
        cache_dir=PROCESSING_OUTPUT / "model-cache",
        max_live_calls=0,
        timeout=timeout,
    )
    client = JsonCompletionClient(
        config,
        cache_dir=PROCESSING_OUTPUT / "model-cache",
        max_live_calls=max_live_calls,
        timeout=timeout,
        retry_failed=retry_failed,
    )
    pipeline = ProcessingPipeline(
        sources,
        outputs,
        ModelPagePartitioner(layout_client, sources),
        SemanticObjectAdapter(
            sources,
            outputs,
            client,
            qualification_policy=qualification_policy,
            description_corrections=description_corrections,
            chart_corrections=chart_corrections,
        ),
    )
    snapshot_id, manifest = pipeline.run(
        source.manifest_id,
        selected_page_indices=tuple(sorted(page - 1 for page in physical_pages)),
    )
    review = export_processing_review(sources, outputs, snapshot_id)
    return ProcessingRunSummary(
        processing_id=snapshot_id,
        source_page_count=manifest.scope.source_page_count,
        selected_physical_pages=manifest.scope.physical_pages,
        layout_succeeded_pages=sum(
            page.partition.state == "succeeded" for page in manifest.pages
        ),
        object_count=sum(len(page.objects) for page in manifest.pages),
        semantic_status="attempted: inspect each actual IR/description and concrete diagnostic; inferred output is not qualified or indexed",
        review_path=str(review),
        live_call_count=client.live_call_count,
    )


class RetrievalRunSummary(BoundaryModel):
    processing_id: str
    retrieval_snapshot_id: str
    member_count: int
    embedding_dimensions: tuple[int, ...]
    returned_contexts: int
    artifact_path: str
    validation_path: str


class RetrievalDecision(BoundaryModel):
    candidate_index: int
    member_id: str
    object_id: str
    physical_page: int
    scope: str
    rerank_score: float
    cosine_score: float
    financial_qa_allowed: bool
    financial_guard_reason: str
    object_review: str


class RetrievalCandidate(BoundaryModel):
    candidate_index: int
    member_id: str
    object_id: str
    physical_page: int
    cosine_score: float
    description: str


class RetrievalValidation(BoundaryModel):
    processing_id: str
    snapshot_id: str
    embedding_model: str
    embedding_fingerprint: str
    embedding_dimensions: tuple[int, ...]
    rerank_model: str
    rerank_configuration_id: str
    ranking_status: str
    ranking_diagnostics: tuple[str, ...]
    indexed_descriptions: int
    candidates: tuple[RetrievalCandidate, ...]
    decisions: tuple[RetrievalDecision, ...]


def index_aia_processing(
    *,
    processing_id: str,
    query: str,
    limit: int = 5,
    rerank_configuration_id: str = "unrecorded",
) -> RetrievalRunSummary:
    """Explicit real local embedding/rerank run over source-qualified projections."""
    from dataclasses import replace
    from hashlib import sha256

    from enterprise_pdf_rag.adapters.http.processing_review import (
        ProcessingContextResponse,
        ProcessingSearchResponse,
        RetrievalExample,
    )
    from enterprise_pdf_rag.adapters.local_models import (
        LocalEmbeddingAdapter,
        LocalRerankAdapter,
    )
    from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
    from enterprise_pdf_rag.adapters.providers import load_local_model_config
    from enterprise_pdf_rag.adapters.retrieval_evaluations import save_evaluation
    from enterprise_pdf_rag.adapters.source_publication import (
        validate_processing_source,
    )
    from enterprise_pdf_rag.processing.retrieval import require_financial_qualification

    if not query.strip() or not 1 <= limit <= 20:
        raise ValueError("A nonempty query and a bounded context limit are required")
    sources = LocalDocumentStore(AIA_OUTPUT)
    outputs = ProcessingStore(PROCESSING_OUTPUT)
    manifest = outputs.load(processing_id)
    validate_processing_source(
        sources=sources, artifacts=outputs.assets, manifest=manifest, plan=None
    )
    embedding_config = load_local_model_config("embedding")
    rerank_config = load_local_model_config("rerank")
    embedding = LocalEmbeddingAdapter(embedding_config)
    reranker = LocalRerankAdapter(rerank_config)
    retrieval = ProcessingRetrieval(sources, outputs, embedding)
    publication = retrieval.build(
        manifest.scope,
        tuple(
            (page.page_index, item) for page in manifest.pages for item in page.objects
        ),
    )
    published = replace(manifest, retrieval=publication)
    result_id = outputs.publish(published, sources=sources)
    export_processing_review(sources, outputs, result_id)
    plan, _ = outputs.load_retrieval(publication)
    hits = retrieval.search(publication, query, limit=min(100, max(limit, limit * 3)))
    if hits:
        candidates = tuple(retrieval.resolve(publication, hit) for hit in hits)
        order = reranker.rerank(
            query,
            tuple(context.description.text for context in candidates),
            limit=min(limit, len(candidates)),
        )
        selected = tuple(hits[result.index] for result in order)
        contexts = tuple(candidates[result.index] for result in order)
    else:
        selected = ()
        contexts = ()
        order = ()
        candidates = ()
    artifact = RetrievalExample(
        processing_id=result_id,
        query=query,
        results=ProcessingSearchResponse(
            processing_id=result_id, snapshot_id=publication.snapshot_id, hits=selected
        ),
        contexts=tuple(
            ProcessingContextResponse(processing_id=result_id, context=context)
            for context in contexts
        ),
    )
    decisions: list[RetrievalDecision] = []
    for context, result in zip(contexts, order, strict=True):
        allowed = True
        reason = "All required field relationships are independently qualified"
        try:
            require_financial_qualification(context)
        except ValueError as error:
            allowed = False
            reason = str(error)
        directory = (
            "object-" + sha256(context.member.object_id.encode()).hexdigest()[:20]
        )
        decisions.append(
            RetrievalDecision(
                candidate_index=result.index,
                member_id=context.member.member_id,
                object_id=context.member.object_id,
                physical_page=context.member.page_index + 1,
                scope=context.scope,
                rerank_score=result.relevance_score,
                cosine_score=hits[result.index].score,
                financial_qa_allowed=allowed,
                financial_guard_reason=reason,
                object_review=f"../../page-{context.member.page_index + 1:03d}/objects/{directory}/review.html",
            )
        )
    validation = RetrievalValidation(
        processing_id=result_id,
        snapshot_id=publication.snapshot_id,
        embedding_model=embedding_config.model,
        embedding_fingerprint=embedding.fingerprint,
        embedding_dimensions=tuple(
            sorted({member.embedding_dimensions for member in plan.members})
        ),
        rerank_model=rerank_config.model,
        rerank_configuration_id=rerank_configuration_id,
        ranking_status="not_qualified",
        ranking_diagnostics=(
            "This records actual provider scores; ranking quality requires separate positive/negative controls.",
            *(
                ("All returned scores are tied; this is not a validated ranking.",)
                if len(order) > 1
                and len({result.relevance_score for result in order}) == 1
                else ()
            ),
        ),
        indexed_descriptions=len(plan.members),
        candidates=tuple(
            RetrievalCandidate(
                candidate_index=index,
                member_id=context.member.member_id,
                object_id=context.member.object_id,
                physical_page=context.member.page_index + 1,
                cosine_score=hits[index].score,
                description=context.description.text,
            )
            for index, context in enumerate(candidates)
        ),
        decisions=tuple(decisions),
    )
    target, validation_path = save_evaluation(
        outputs,
        result_id,
        artifact.model_dump_json(indent=2).encode(),
        validation.model_dump_json(indent=2).encode(),
    )
    export_processing_review(sources, outputs, result_id)
    return RetrievalRunSummary(
        processing_id=result_id,
        retrieval_snapshot_id=publication.snapshot_id,
        member_count=len(plan.members),
        embedding_dimensions=tuple(
            sorted({member.embedding_dimensions for member in plan.members})
        ),
        returned_contexts=len(contexts),
        artifact_path=str(target),
        validation_path=str(validation_path),
    )
