"""Frozen legacy → canonical module map for the dissolved ``enterprise_pdf_rag`` (ADR 0022).

The single source of truth for the compatibility shim (``_shim.py``), its tests and the
import codemod (``scripts/enterprise_pdf_rag/rewrite_legacy_imports.py``). Generated once
from ``git ls-files src/enterprise_pdf_rag`` and the merge plan, then frozen: a migration
batch ``git mv``-s a module to the target named here and never edits an entry.

``PENDING`` holds the AIA sample lane, whose destination is decided with its move (the
lane leaves the wheel rather than joining ``ragspine``). Until then those modules stay at
their legacy paths and no alias exists for them.
"""

MOVES: dict[str, str] = {
    # B2 common/evidence
    "enterprise_pdf_rag.core.settings": "ragspine.common.evidence.settings",
    "enterprise_pdf_rag.core.logging": "ragspine.common.evidence.logging",
    # B2 common/evidence/providers
    "enterprise_pdf_rag.adapters.providers": "ragspine.common.evidence.providers.providers",
    "enterprise_pdf_rag.adapters.json_completion": "ragspine.common.evidence.providers.json_completion",
    "enterprise_pdf_rag.adapters.local_models": "ragspine.common.evidence.providers.local_models",
    "enterprise_pdf_rag.adapters.local_model_launcher": "ragspine.common.evidence.providers.local_model_launcher",
    "enterprise_pdf_rag.adapters.local_model_tunnel": "ragspine.common.evidence.providers.local_model_tunnel",
    # B3 extraction/evidence/document
    "enterprise_pdf_rag.documents.models": "ragspine.extraction.evidence.document.models",
    "enterprise_pdf_rag.documents.ports": "ragspine.extraction.evidence.document.ports",
    "enterprise_pdf_rag.documents.service": "ragspine.extraction.evidence.document.service",
    "enterprise_pdf_rag.documents.text_layer": "ragspine.extraction.evidence.document.text_layer",
    # B3 extraction/evidence/page
    "enterprise_pdf_rag.processing.models": "ragspine.extraction.evidence.page.models",
    "enterprise_pdf_rag.processing.service": "ragspine.extraction.evidence.page.service",
    "enterprise_pdf_rag.processing.ports": "ragspine.extraction.evidence.page.ports",
    "enterprise_pdf_rag.processing.geometry": "ragspine.extraction.evidence.page.geometry",
    "enterprise_pdf_rag.processing.column_regions": "ragspine.extraction.evidence.page.column_regions",
    # B3 extraction/evidence/metadata
    "enterprise_pdf_rag.processing.page_metadata": "ragspine.extraction.evidence.metadata.page_metadata",
    "enterprise_pdf_rag.processing.periods": "ragspine.extraction.evidence.metadata.periods",
    "enterprise_pdf_rag.processing.document_metadata": "ragspine.extraction.evidence.metadata.document_metadata",
    "enterprise_pdf_rag.processing.document_tree": "ragspine.extraction.evidence.metadata.document_tree",
    # B3 extraction/evidence/objects
    "enterprise_pdf_rag.processing.typed_ir": "ragspine.extraction.evidence.objects.typed_ir",
    # B3 extraction/evidence/objects/tables
    "enterprise_pdf_rag.processing.table_models": "ragspine.extraction.evidence.objects.tables.table_models",
    "enterprise_pdf_rag.processing.table_grid_proof": "ragspine.extraction.evidence.objects.tables.table_grid_proof",
    "enterprise_pdf_rag.processing.table_transcription": "ragspine.extraction.evidence.objects.tables.table_transcription",
    # B3 extraction/evidence/objects/diagrams
    "enterprise_pdf_rag.processing.diagram_models": "ragspine.extraction.evidence.objects.diagrams.diagram_models",
    "enterprise_pdf_rag.processing.diagram_description": "ragspine.extraction.evidence.objects.diagrams.diagram_description",
    # B3 extraction/evidence/objects/formulas
    "enterprise_pdf_rag.processing.formula_models": "ragspine.extraction.evidence.objects.formulas.formula_models",
    "enterprise_pdf_rag.processing.formula_rules": "ragspine.extraction.evidence.objects.formulas.formula_rules",
    # B3 extraction/evidence/figures
    "enterprise_pdf_rag.figures.models": "ragspine.extraction.evidence.figures.models",
    "enterprise_pdf_rag.figures.ports": "ragspine.extraction.evidence.figures.ports",
    "enterprise_pdf_rag.figures.service": "ragspine.extraction.evidence.figures.service",
    "enterprise_pdf_rag.figures.validation": "ragspine.extraction.evidence.figures.validation",
    "enterprise_pdf_rag.figures.source_label_match": "ragspine.extraction.evidence.figures.source_label_match",
    # B3 extraction/evidence/figures/chart_qa
    "enterprise_pdf_rag.figures.chart_qa.displayed_evidence": "ragspine.extraction.evidence.figures.chart_qa.displayed_evidence",
    "enterprise_pdf_rag.figures.chart_qa.displayed_models": "ragspine.extraction.evidence.figures.chart_qa.displayed_models",
    "enterprise_pdf_rag.figures.chart_qa.displayed_ports": "ragspine.extraction.evidence.figures.chart_qa.displayed_ports",
    "enterprise_pdf_rag.figures.chart_qa.displayed_service": "ragspine.extraction.evidence.figures.chart_qa.displayed_service",
    "enterprise_pdf_rag.figures.chart_qa.evidence": "ragspine.extraction.evidence.figures.chart_qa.evidence",
    "enterprise_pdf_rag.figures.chart_qa.models": "ragspine.extraction.evidence.figures.chart_qa.models",
    "enterprise_pdf_rag.figures.chart_qa.ports": "ragspine.extraction.evidence.figures.chart_qa.ports",
    "enterprise_pdf_rag.figures.chart_qa.service": "ragspine.extraction.evidence.figures.chart_qa.service",
    # B4 extraction/evidence/adapters/pdfspine
    "enterprise_pdf_rag.adapters.pdfspine_document": "ragspine.extraction.evidence.adapters.pdfspine.pdfspine_document",
    "enterprise_pdf_rag.adapters.pdfspine_svg": "ragspine.extraction.evidence.adapters.pdfspine.pdfspine_svg",
    "enterprise_pdf_rag.adapters.pdfspine_figure": "ragspine.extraction.evidence.adapters.pdfspine.pdfspine_figure",
    "enterprise_pdf_rag.adapters.pdfspine_formula": "ragspine.extraction.evidence.adapters.pdfspine.pdfspine_formula",
    "enterprise_pdf_rag.adapters.pdfspine_tables": "ragspine.extraction.evidence.adapters.pdfspine.pdfspine_tables",
    # B4 extraction/evidence/adapters/source_paint
    "enterprise_pdf_rag.adapters.source_paint": "ragspine.extraction.evidence.adapters.source_paint.source_paint",
    "enterprise_pdf_rag.adapters.source_paint_bar": "ragspine.extraction.evidence.adapters.source_paint.source_paint_bar",
    "enterprise_pdf_rag.adapters.source_profile": "ragspine.extraction.evidence.adapters.source_paint.source_profile",
    "enterprise_pdf_rag.adapters.stroke_visibility": "ragspine.extraction.evidence.adapters.source_paint.stroke_visibility",
    # B4 extraction/evidence/adapters/shapes
    "enterprise_pdf_rag.adapters.donut_geometry": "ragspine.extraction.evidence.adapters.shapes.donut_geometry",
    "enterprise_pdf_rag.adapters.bar_geometry": "ragspine.extraction.evidence.adapters.shapes.bar_geometry",
    "enterprise_pdf_rag.adapters.diagram_geometry": "ragspine.extraction.evidence.adapters.shapes.diagram_geometry",
    # B4 extraction/evidence/adapters/qualification
    "enterprise_pdf_rag.adapters.figure_label_qualification": "ragspine.extraction.evidence.adapters.qualification.figure_label_qualification",
    "enterprise_pdf_rag.adapters.donut_qualification": "ragspine.extraction.evidence.adapters.qualification.donut_qualification",
    "enterprise_pdf_rag.adapters.bar_qualification": "ragspine.extraction.evidence.adapters.qualification.bar_qualification",
    "enterprise_pdf_rag.adapters.diagram_qualification": "ragspine.extraction.evidence.adapters.qualification.diagram_qualification",
    "enterprise_pdf_rag.adapters.formula_qualification": "ragspine.extraction.evidence.adapters.qualification.formula_qualification",
    "enterprise_pdf_rag.adapters.literal_qualification": "ragspine.extraction.evidence.adapters.qualification.literal_qualification",
    # B4 extraction/evidence/adapters/semantics
    "enterprise_pdf_rag.adapters.figure_reasoning": "ragspine.extraction.evidence.adapters.semantics.figure_reasoning",
    "enterprise_pdf_rag.adapters.chart_semantics": "ragspine.extraction.evidence.adapters.semantics.chart_semantics",
    "enterprise_pdf_rag.adapters.chart_semantic_schemas": "ragspine.extraction.evidence.adapters.semantics.chart_semantic_schemas",
    "enterprise_pdf_rag.adapters.visual_semantics": "ragspine.extraction.evidence.adapters.semantics.visual_semantics",
    "enterprise_pdf_rag.adapters.visual_semantic_schemas": "ragspine.extraction.evidence.adapters.semantics.visual_semantic_schemas",
    "enterprise_pdf_rag.adapters.description_mapping": "ragspine.extraction.evidence.adapters.semantics.description_mapping",
    "enterprise_pdf_rag.adapters.description_normalization": "ragspine.extraction.evidence.adapters.semantics.description_normalization",
    # B4 extraction/evidence/adapters/chart_qa
    "enterprise_pdf_rag.adapters.chart_qa": "ragspine.extraction.evidence.adapters.chart_qa.chart_qa",
    "enterprise_pdf_rag.adapters.chart_qa_displayed": "ragspine.extraction.evidence.adapters.chart_qa.chart_qa_displayed",
    # B5 storage/evidence
    "enterprise_pdf_rag.adapters.document_store": "ragspine.storage.evidence.document_store",
    "enterprise_pdf_rag.adapters.processing_store": "ragspine.storage.evidence.processing_store",
    # B5 retrieval/evidence/index
    "enterprise_pdf_rag.processing.index_text": "ragspine.retrieval.evidence.index.index_text",
    # the one rename: a snapshot, not a second `retrieval.py` beside retrieval/lexical/
    "enterprise_pdf_rag.processing.retrieval": "ragspine.retrieval.evidence.index.snapshot",
    # B5 retrieval/evidence/adapters
    "enterprise_pdf_rag.adapters.hybrid_search": "ragspine.retrieval.evidence.adapters.hybrid_search",
    "enterprise_pdf_rag.adapters.tree_retrieval": "ragspine.retrieval.evidence.adapters.tree_retrieval",
    "enterprise_pdf_rag.adapters.processing_retrieval": "ragspine.retrieval.evidence.adapters.processing_retrieval",
    "enterprise_pdf_rag.adapters.document_catalog": "ragspine.retrieval.evidence.adapters.document_catalog",
    # B6 ingestion/evidence/pipeline
    "enterprise_pdf_rag.adapters.pdf_ingestion": "ragspine.ingestion.evidence.pipeline.pdf_ingestion",
    "enterprise_pdf_rag.adapters.processing_runtime": "ragspine.ingestion.evidence.pipeline.processing_runtime",
    # B6 ingestion/evidence/stages
    "enterprise_pdf_rag.adapters.page_partition": "ragspine.ingestion.evidence.stages.page_partition",
    "enterprise_pdf_rag.adapters.layout_normalization": "ragspine.ingestion.evidence.stages.layout_normalization",
    "enterprise_pdf_rag.adapters.page_metadata_extraction": "ragspine.ingestion.evidence.stages.page_metadata_extraction",
    "enterprise_pdf_rag.adapters.document_tree_extraction": "ragspine.ingestion.evidence.stages.document_tree_extraction",
    "enterprise_pdf_rag.adapters.object_processing": "ragspine.ingestion.evidence.stages.object_processing",
    "enterprise_pdf_rag.adapters.source_objects": "ragspine.ingestion.evidence.stages.source_objects",
    "enterprise_pdf_rag.adapters.semantic_objects": "ragspine.ingestion.evidence.stages.semantic_objects",
    # B6 ingestion/evidence/publication
    "enterprise_pdf_rag.adapters.draft_publication": "ragspine.ingestion.evidence.publication.draft_publication",
    "enterprise_pdf_rag.adapters.source_publication": "ragspine.ingestion.evidence.publication.source_publication",
    "enterprise_pdf_rag.adapters.bar_publication": "ragspine.ingestion.evidence.publication.bar_publication",
    "enterprise_pdf_rag.adapters.chart_publication": "ragspine.ingestion.evidence.publication.chart_publication",
    "enterprise_pdf_rag.adapters.diagram_publication": "ragspine.ingestion.evidence.publication.diagram_publication",
    "enterprise_pdf_rag.adapters.chart_member_validation": "ragspine.ingestion.evidence.publication.chart_member_validation",
    "enterprise_pdf_rag.adapters.chart_qa_promotion": "ragspine.ingestion.evidence.publication.chart_qa_promotion",
    "enterprise_pdf_rag.adapters.chart_qa_bar_promotion": "ragspine.ingestion.evidence.publication.chart_qa_bar_promotion",
    "enterprise_pdf_rag.adapters.processing_export": "ragspine.ingestion.evidence.publication.processing_export",
    "enterprise_pdf_rag.adapters.visual_requalification": "ragspine.ingestion.evidence.publication.visual_requalification",
    # B7 agent/evidence/answers
    "enterprise_pdf_rag.answers.member_filter": "ragspine.agent.evidence.answers.member_filter",
    "enterprise_pdf_rag.answers.models": "ragspine.agent.evidence.answers.models",
    "enterprise_pdf_rag.answers.page_window": "ragspine.agent.evidence.answers.page_window",
    "enterprise_pdf_rag.answers.ports": "ragspine.agent.evidence.answers.ports",
    "enterprise_pdf_rag.answers.prompt": "ragspine.agent.evidence.answers.prompt",
    "enterprise_pdf_rag.answers.query_filters": "ragspine.agent.evidence.answers.query_filters",
    "enterprise_pdf_rag.answers.query_mode": "ragspine.agent.evidence.answers.query_mode",
    "enterprise_pdf_rag.answers.verify": "ragspine.agent.evidence.answers.verify",
    # B7 agent/evidence/context
    "enterprise_pdf_rag.processing.context_builder": "ragspine.agent.evidence.context.context_builder",
    # B7 agent/evidence/adapters
    "enterprise_pdf_rag.adapters.answer_service": "ragspine.agent.evidence.adapters.answer_service",
    "enterprise_pdf_rag.adapters.answer_audit": "ragspine.agent.evidence.adapters.answer_audit",
    "enterprise_pdf_rag.adapters.query_translation": "ragspine.agent.evidence.adapters.query_translation",
    # B7 eval/evidence/chart_qa
    "enterprise_pdf_rag.adapters.chart_qa_evaluation": "ragspine.eval.evidence.chart_qa.chart_qa_evaluation",
    "enterprise_pdf_rag.adapters.chart_qa_capture": "ragspine.eval.evidence.chart_qa.chart_qa_capture",
    "enterprise_pdf_rag.adapters.chart_qa_v2_capture": "ragspine.eval.evidence.chart_qa.chart_qa_v2_capture",
    "enterprise_pdf_rag.adapters.chart_qa_v2_evaluation": "ragspine.eval.evidence.chart_qa.chart_qa_v2_evaluation",
    "enterprise_pdf_rag.adapters.chart_qa_v2_evaluation_models": "ragspine.eval.evidence.chart_qa.chart_qa_v2_evaluation_models",
    "enterprise_pdf_rag.adapters.chart_qa_v2_targets": "ragspine.eval.evidence.chart_qa.chart_qa_v2_targets",
    # B7 eval/evidence
    "enterprise_pdf_rag.adapters.nl_gold": "ragspine.eval.evidence.nl_gold",
    "enterprise_pdf_rag.adapters.retrieval_evaluations": "ragspine.eval.evidence.retrieval_evaluations",
    # B8 service/evidence/api
    "enterprise_pdf_rag.adapters.http.aia_review": "ragspine.service.evidence.api.aia_review",
    "enterprise_pdf_rag.adapters.http.app": "ragspine.service.evidence.api.app",
    "enterprise_pdf_rag.adapters.http.catalog_schemas": "ragspine.service.evidence.api.catalog_schemas",
    "enterprise_pdf_rag.adapters.http.chart_qa_schemas": "ragspine.service.evidence.api.chart_qa_schemas",
    "enterprise_pdf_rag.adapters.http.chart_qa_v2_schemas": "ragspine.service.evidence.api.chart_qa_v2_schemas",
    "enterprise_pdf_rag.adapters.http.chart_qa": "ragspine.service.evidence.api.chart_qa",
    "enterprise_pdf_rag.adapters.http.chat_schemas": "ragspine.service.evidence.api.chat_schemas",
    "enterprise_pdf_rag.adapters.http.chat": "ragspine.service.evidence.api.chat",
    "enterprise_pdf_rag.adapters.http.document_schemas": "ragspine.service.evidence.api.document_schemas",
    "enterprise_pdf_rag.adapters.http.documents": "ragspine.service.evidence.api.documents",
    "enterprise_pdf_rag.adapters.http.layout_schemas": "ragspine.service.evidence.api.layout_schemas",
    "enterprise_pdf_rag.adapters.http.openai_demo": "ragspine.service.evidence.api.openai_demo",
    "enterprise_pdf_rag.adapters.http.openai_schemas": "ragspine.service.evidence.api.openai_schemas",
    "enterprise_pdf_rag.adapters.http.processing_review": "ragspine.service.evidence.api.processing_review",
    "enterprise_pdf_rag.adapters.http.processing_schemas": "ragspine.service.evidence.api.processing_schemas",
    "enterprise_pdf_rag.adapters.http.schemas": "ragspine.service.evidence.api.schemas",
    "enterprise_pdf_rag.adapters.http.webui_gate": "ragspine.service.evidence.api.webui_gate",
    # B8 service/evidence/demo
    "enterprise_pdf_rag.adapters.runtime": "ragspine.service.evidence.demo.runtime",
    "enterprise_pdf_rag.adapters.memory": "ragspine.service.evidence.demo.memory",
    "enterprise_pdf_rag.adapters.offline": "ragspine.service.evidence.demo.offline",
    "enterprise_pdf_rag.adapters.demo_source": "ragspine.service.evidence.demo.demo_source",
    "enterprise_pdf_rag.adapters.qualification": "ragspine.service.evidence.demo.qualification",
    "enterprise_pdf_rag.adapters.review": "ragspine.service.evidence.demo.review",
    # B8 console-script module
    "enterprise_pdf_rag.cli": "ragspine.cli.evidence",
}

PACKAGES: frozenset[str] = frozenset(
    {
        "enterprise_pdf_rag.adapters",
        "enterprise_pdf_rag.adapters.http",
        "enterprise_pdf_rag.answers",
        "enterprise_pdf_rag.core",
        "enterprise_pdf_rag.documents",
        "enterprise_pdf_rag.figures",
        "enterprise_pdf_rag.figures.chart_qa",
        "enterprise_pdf_rag.processing",
    }
)

PENDING: frozenset[str] = frozenset(
    {
        "enterprise_pdf_rag.adapters.aia_candidates",
        "enterprise_pdf_rag.adapters.aia_ingestion",
        "enterprise_pdf_rag.adapters.aia_processing",
        "enterprise_pdf_rag.adapters.source_review_html",
        "enterprise_pdf_rag.documents.aia",
    }
)
