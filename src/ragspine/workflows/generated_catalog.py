"""Build reviewed workflow descriptors into safe, deterministic Dify templates.

Descriptor sources are attribution-only references.  This module never reads,
downloads, or copies an upstream workflow, prompt, credential, or executable
configuration into the generated Dify document.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, cast

from ragspine.workflows.errors import WorkflowCatalogError
from ragspine.workflows.formats import parse_workflow
from ragspine.workflows.model import (
    WorkflowCompatibility,
    WorkflowRequirement,
    WorkflowSource,
    WorkflowTemplate,
)
from ragspine.workflows.planner import (
    DIFY_DSL_VERSION,
    WorkflowBlueprint,
    WorkflowInput,
    blueprint_system_prompt,
    make_blueprint,
    render_blueprint,
)
from ragspine.workflows.source_policy import validate_source_reference

WorkflowArchetype = Literal[
    "analysis",
    "extraction",
    "routing",
    "synthesis",
    "transformation",
]


@dataclass(frozen=True)
class _WorkflowSpec:
    archetype: str
    inputs: tuple[WorkflowInput, ...]
    output_name: str
    guidance: str
    output_constraint: str


DESCRIPTOR_SCHEMA_VERSION = 1
MAX_DESCRIPTOR_CATALOG_BYTES = 4 * 1024 * 1024
MAX_GENERATED_TEMPLATES = 1000
SUPPORTED_ARCHETYPES = frozenset(
    {"analysis", "extraction", "routing", "synthesis", "transformation"}
)
_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_OPENAI_PROVIDER = "langgenius/openai/openai"
_ARCHETYPE_SPECS: dict[str, _WorkflowSpec] = {
    "analysis": _WorkflowSpec(
        archetype="analysis",
        inputs=(
            WorkflowInput(variable="evidence", label="Evidence", kind="paragraph"),
            WorkflowInput(variable="question", label="Question", kind="text-input"),
        ),
        output_name="analysis",
        guidance=(
            "Analyze the evidence against the question, distinguish facts from assumptions, "
            "and make material risks explicit."
        ),
        output_constraint=(
            "Return findings, supporting evidence, assumptions, and uncertainty in clearly "
            "labeled sections."
        ),
    ),
    "extraction": _WorkflowSpec(
        archetype="extraction",
        inputs=(
            WorkflowInput(variable="source", label="Source", kind="paragraph"),
            WorkflowInput(variable="fields", label="Requested fields", kind="paragraph"),
        ),
        output_name="extracted_data",
        guidance=(
            "Extract only requested information explicitly supported by the source, preserve "
            "source wording where useful, and mark missing fields clearly."
        ),
        output_constraint=(
            "Return one valid JSON object using the requested field names; use null for an "
            "unsupported or missing value."
        ),
    ),
    "routing": _WorkflowSpec(
        archetype="routing",
        inputs=(
            WorkflowInput(variable="request", label="Request", kind="paragraph"),
            WorkflowInput(variable="policy", label="Routing policy", kind="paragraph"),
        ),
        output_name="routing_decision",
        guidance=(
            "Classify and prioritize the request using only the supplied policy, then recommend "
            "a route without executing external actions."
        ),
        output_constraint=(
            "Return a JSON object with route, priority, rationale, and missing_information."
        ),
    ),
    "synthesis": _WorkflowSpec(
        archetype="synthesis",
        inputs=(
            WorkflowInput(variable="materials", label="Materials", kind="paragraph"),
            WorkflowInput(variable="audience", label="Audience", kind="text-input"),
        ),
        output_name="synthesis",
        guidance=(
            "Synthesize the materials for the stated audience without inventing facts or "
            "silently resolving contradictions."
        ),
        output_constraint=(
            "Return an audience-ready brief that separates established facts, contradictions, "
            "and open questions."
        ),
    ),
    "transformation": _WorkflowSpec(
        archetype="transformation",
        inputs=(
            WorkflowInput(variable="content", label="Content", kind="paragraph"),
            WorkflowInput(variable="instruction", label="Instruction", kind="paragraph"),
        ),
        output_name="transformed_content",
        guidance=(
            "Apply the instruction to the content while preserving factual meaning, important "
            "constraints, and traceable uncertainty."
        ),
        output_constraint=(
            "Return the transformed content followed by concise change notes; identify any "
            "instruction that could not be satisfied safely."
        ),
    ),
}

_USE_CASE_SPECS: dict[str, _WorkflowSpec] = {
    "alerting": _WorkflowSpec(
        archetype="routing",
        inputs=(
            WorkflowInput(variable="events", label="Observed events", kind="paragraph"),
            WorkflowInput(variable="alert_policy", label="Alert policy", kind="paragraph"),
        ),
        output_name="alert_decisions",
        guidance=(
            "Evaluate only the supplied events against the alert policy. Assign severity without "
            "sending notifications or calling external systems."
        ),
        output_constraint=(
            "Return a JSON array of alert decisions with event, severity, evidence, rationale, "
            "and recommended_recipient; use an empty array when nothing qualifies."
        ),
    ),
    "analysis": _WorkflowSpec(
        archetype="analysis",
        inputs=(
            WorkflowInput(variable="evidence", label="Evidence", kind="paragraph"),
            WorkflowInput(
                variable="analysis_question",
                label="Analysis question",
                kind="text-input",
            ),
        ),
        output_name="evidence_analysis",
        guidance=(
            "Analyze the evidence against the stated question, separating observations, "
            "interpretations, and unsupported assumptions."
        ),
        output_constraint=(
            "Return findings, cited evidence excerpts, risks, assumptions, and confidence in "
            "clearly labeled sections."
        ),
    ),
    "classification": _WorkflowSpec(
        archetype="routing",
        inputs=(
            WorkflowInput(
                variable="item_to_classify",
                label="Item to classify",
                kind="paragraph",
            ),
            WorkflowInput(variable="taxonomy", label="Allowed taxonomy", kind="paragraph"),
            WorkflowInput(
                variable="decision_rules",
                label="Decision rules",
                kind="paragraph",
            ),
        ),
        output_name="classification_result",
        guidance=(
            "Classify the item using only the allowed taxonomy and decision rules. Do not invent "
            "a label when the evidence is insufficient."
        ),
        output_constraint=(
            "Return one JSON object with label, confidence, rationale, matched_rules, and "
            "missing_information."
        ),
    ),
    "compliance-review": _WorkflowSpec(
        archetype="analysis",
        inputs=(
            WorkflowInput(variable="material", label="Material to review", kind="paragraph"),
            WorkflowInput(
                variable="policy_criteria",
                label="Policy criteria",
                kind="paragraph",
            ),
        ),
        output_name="compliance_assessment",
        guidance=(
            "Compare the supplied material with each explicit policy criterion. Treat this as "
            "screening support, not legal or regulatory certification."
        ),
        output_constraint=(
            "Return a JSON object containing per-criterion status, supporting evidence, gaps, "
            "overall_status, and required_human_review."
        ),
    ),
    "content-creation": _WorkflowSpec(
        archetype="transformation",
        inputs=(
            WorkflowInput(variable="content_brief", label="Content brief", kind="paragraph"),
            WorkflowInput(variable="source_facts", label="Source facts", kind="paragraph"),
            WorkflowInput(variable="audience", label="Audience", kind="text-input"),
        ),
        output_name="content_draft",
        guidance=(
            "Develop an original draft from the brief for the stated audience, using source facts "
            "as the factual boundary."
        ),
        output_constraint=(
            "Return the draft followed by a fact-use checklist and unresolved questions; do not "
            "add unsupported claims."
        ),
    ),
    "customer-support": _WorkflowSpec(
        archetype="routing",
        inputs=(
            WorkflowInput(
                variable="support_request",
                label="Support request",
                kind="paragraph",
            ),
            WorkflowInput(variable="support_policy", label="Support policy", kind="paragraph"),
            WorkflowInput(variable="known_context", label="Known context", kind="paragraph"),
        ),
        output_name="support_resolution",
        guidance=(
            "Triage the support request using the supplied policy and context. Draft a bounded "
            "response but do not send it or claim actions were completed."
        ),
        output_constraint=(
            "Return a JSON object with category, priority, route, draft_response, evidence, and "
            "missing_information."
        ),
    ),
    "data-enrichment": _WorkflowSpec(
        archetype="extraction",
        inputs=(
            WorkflowInput(variable="records", label="Records", kind="paragraph"),
            WorkflowInput(
                variable="enrichment_schema",
                label="Enrichment schema",
                kind="paragraph",
            ),
            WorkflowInput(variable="reference_data", label="Reference data", kind="paragraph"),
        ),
        output_name="enriched_records",
        guidance=(
            "Add only fields supported by the supplied reference data while preserving original "
            "record identifiers and values."
        ),
        output_constraint=(
            "Return a valid JSON array matching the enrichment schema, with null plus an "
            "enrichment_note for every unsupported field."
        ),
    ),
    "data-synchronization": _WorkflowSpec(
        archetype="transformation",
        inputs=(
            WorkflowInput(variable="source_records", label="Source records", kind="paragraph"),
            WorkflowInput(variable="target_schema", label="Target schema", kind="paragraph"),
            WorkflowInput(variable="sync_rules", label="Synchronization rules", kind="paragraph"),
        ),
        output_name="sync_payload",
        guidance=(
            "Normalize the supplied records for the target schema and synchronization rules. "
            "Prepare data only; never write to an external system."
        ),
        output_constraint=(
            "Return a JSON object with ready_records, rejected_records, validation_errors, and "
            "proposed_operations."
        ),
    ),
    "document-processing": _WorkflowSpec(
        archetype="extraction",
        inputs=(
            WorkflowInput(variable="document_text", label="Document text", kind="paragraph"),
            WorkflowInput(
                variable="processing_requirements",
                label="Processing requirements",
                kind="paragraph",
            ),
        ),
        output_name="document_record",
        guidance=(
            "Organize explicit document content according to the processing requirements while "
            "preserving provenance cues such as headings or page labels when supplied."
        ),
        output_constraint=(
            "Return one valid JSON object with requested fields, source_cues, missing_fields, and "
            "processing_warnings."
        ),
    ),
    "execution-planning": _WorkflowSpec(
        archetype="routing",
        inputs=(
            WorkflowInput(variable="objective", label="Objective", kind="paragraph"),
            WorkflowInput(
                variable="available_capabilities",
                label="Available capabilities",
                kind="paragraph",
            ),
            WorkflowInput(
                variable="safety_constraints",
                label="Safety constraints",
                kind="paragraph",
            ),
        ),
        output_name="execution_plan",
        guidance=(
            "Plan a bounded sequence using only the listed capabilities and safety constraints. "
            "Do not execute tools, trigger webhooks, or claim completion."
        ),
        output_constraint=(
            "Return a JSON object with ordered_steps, prerequisites, approval_points, stop_conditions, "
            "and unresolved_inputs."
        ),
    ),
    "extraction": _WorkflowSpec(
        archetype="extraction",
        inputs=(
            WorkflowInput(
                variable="source_document",
                label="Source document",
                kind="paragraph",
            ),
            WorkflowInput(variable="field_schema", label="Field schema", kind="paragraph"),
        ),
        output_name="extracted_fields",
        guidance=(
            "Extract only fields explicitly supported by the source document and follow the "
            "requested field schema exactly."
        ),
        output_constraint=(
            "Return one valid JSON object using the requested field names, with null for missing "
            "values and a _source_evidence object."
        ),
    ),
    "general-assistance": _WorkflowSpec(
        archetype="synthesis",
        inputs=(
            WorkflowInput(variable="materials", label="Materials", kind="paragraph"),
            WorkflowInput(
                variable="requested_outcome",
                label="Requested outcome",
                kind="text-input",
            ),
        ),
        output_name="organized_result",
        guidance=(
            "Organize the supplied materials toward the requested outcome without assuming a "
            "specialized domain process that was not provided."
        ),
        output_constraint=(
            "Return a useful structured result, followed by assumptions, missing information, "
            "and suggested human checks."
        ),
    ),
    "invoice-processing": _WorkflowSpec(
        archetype="extraction",
        inputs=(
            WorkflowInput(variable="invoice_text", label="Invoice text", kind="paragraph"),
            WorkflowInput(
                variable="validation_rules",
                label="Validation rules",
                kind="paragraph",
            ),
        ),
        output_name="invoice_review",
        guidance=(
            "Extract explicit invoice fields and evaluate them only against the supplied validation "
            "rules. Never infer a missing amount, tax, date, or vendor identifier."
        ),
        output_constraint=(
            "Return a JSON object with invoice_fields, validation_results, missing_fields, "
            "discrepancies, and review_required."
        ),
    ),
    "knowledge-retrieval": _WorkflowSpec(
        archetype="synthesis",
        inputs=(
            WorkflowInput(
                variable="knowledge_materials",
                label="Knowledge materials",
                kind="paragraph",
            ),
            WorkflowInput(
                variable="information_need",
                label="Information need",
                kind="text-input",
            ),
        ),
        output_name="knowledge_answer",
        guidance=(
            "Find and synthesize only passages relevant to the information need within the supplied "
            "knowledge materials."
        ),
        output_constraint=(
            "Return a concise answer with supporting excerpts or source labels, conflicts, and an "
            "explicit not_found result when support is absent."
        ),
    ),
    "lead-generation": _WorkflowSpec(
        archetype="extraction",
        inputs=(
            WorkflowInput(variable="prospect_records", label="Prospect records", kind="paragraph"),
            WorkflowInput(
                variable="qualification_criteria",
                label="Qualification criteria",
                kind="paragraph",
            ),
        ),
        output_name="qualified_leads",
        guidance=(
            "Evaluate only supplied prospect records against explicit qualification criteria. Do "
            "not guess contact details or enrich personal data from outside sources."
        ),
        output_constraint=(
            "Return a JSON array with record_id, qualification, matched_criteria, evidence, and "
            "missing_information."
        ),
    ),
    "monitoring": _WorkflowSpec(
        archetype="analysis",
        inputs=(
            WorkflowInput(variable="observations", label="Observations", kind="paragraph"),
            WorkflowInput(variable="baseline", label="Baseline", kind="paragraph"),
            WorkflowInput(
                variable="monitoring_rules",
                label="Monitoring rules",
                kind="paragraph",
            ),
        ),
        output_name="signal_assessment",
        guidance=(
            "Analyze this supplied observation snapshot against the baseline and monitoring rules. "
            "Do not imply continuous monitoring or access to live systems."
        ),
        output_constraint=(
            "Return a JSON object with detected_signals, evidence, magnitude, confidence, and "
            "recommended_follow_up."
        ),
    ),
    "onboarding": _WorkflowSpec(
        archetype="transformation",
        inputs=(
            WorkflowInput(variable="procedures", label="Procedures", kind="paragraph"),
            WorkflowInput(variable="learner_profile", label="Learner profile", kind="paragraph"),
            WorkflowInput(variable="scope", label="Onboarding scope", kind="text-input"),
        ),
        output_name="onboarding_guide",
        guidance=(
            "Turn supplied procedures into a clear onboarding path for the learner profile and "
            "requested scope without adding nonexistent policies."
        ),
        output_constraint=(
            "Return sequenced learning steps, prerequisites, checks for understanding, references, "
            "and unresolved questions."
        ),
    ),
    "outreach": _WorkflowSpec(
        archetype="transformation",
        inputs=(
            WorkflowInput(
                variable="recipient_context",
                label="Recipient context",
                kind="paragraph",
            ),
            WorkflowInput(variable="offer_context", label="Offer context", kind="paragraph"),
            WorkflowInput(
                variable="tone_constraints",
                label="Tone and constraints",
                kind="paragraph",
            ),
        ),
        output_name="outreach_draft",
        guidance=(
            "Draft contextual outreach using only supplied recipient and offer context. Do not send "
            "messages or invent personal details."
        ),
        output_constraint=(
            "Return a subject, message draft, personalization evidence, claims checklist, and "
            "missing_information."
        ),
    ),
    "question-answering": _WorkflowSpec(
        archetype="synthesis",
        inputs=(
            WorkflowInput(
                variable="reference_material",
                label="Reference material",
                kind="paragraph",
            ),
            WorkflowInput(variable="question", label="Question", kind="text-input"),
        ),
        output_name="grounded_answer",
        guidance=(
            "Answer the question strictly from the supplied reference material and distinguish a "
            "supported answer from a plausible guess."
        ),
        output_constraint=(
            "Return the answer, supporting excerpts or source labels, uncertainty, and an explicit "
            "not_found statement when evidence is insufficient."
        ),
    ),
    "recommendation": _WorkflowSpec(
        archetype="synthesis",
        inputs=(
            WorkflowInput(variable="options", label="Options", kind="paragraph"),
            WorkflowInput(
                variable="decision_criteria",
                label="Decision criteria",
                kind="paragraph",
            ),
            WorkflowInput(variable="context", label="Decision context", kind="paragraph"),
        ),
        output_name="recommendation_memo",
        guidance=(
            "Compare only the supplied options against the explicit criteria and context. Make "
            "trade-offs and missing evidence visible."
        ),
        output_constraint=(
            "Return a recommendation, option comparison, criterion-by-criterion rationale, "
            "assumptions, risks, and conditions that would change the choice."
        ),
    ),
    "report-generation": _WorkflowSpec(
        archetype="synthesis",
        inputs=(
            WorkflowInput(variable="findings", label="Findings", kind="paragraph"),
            WorkflowInput(
                variable="report_audience",
                label="Report audience",
                kind="text-input",
            ),
            WorkflowInput(
                variable="report_structure",
                label="Report structure",
                kind="paragraph",
            ),
        ),
        output_name="structured_report",
        guidance=(
            "Assemble supplied findings into the requested report structure for the stated audience "
            "without silently resolving contradictions."
        ),
        output_constraint=(
            "Return a structured report with executive summary, evidence-backed sections, "
            "contradictions, limitations, and open questions."
        ),
    ),
    "research": _WorkflowSpec(
        archetype="analysis",
        inputs=(
            WorkflowInput(
                variable="research_evidence",
                label="Research evidence",
                kind="paragraph",
            ),
            WorkflowInput(
                variable="research_question",
                label="Research question",
                kind="text-input",
            ),
        ),
        output_name="research_brief",
        guidance=(
            "Develop a focused research synthesis from the supplied evidence only, comparing "
            "sources and surfacing uncertainty or evidence gaps."
        ),
        output_constraint=(
            "Return a research brief with answer, evidence map, competing explanations, limitations, "
            "and next research questions."
        ),
    ),
    "routing": _WorkflowSpec(
        archetype="routing",
        inputs=(
            WorkflowInput(variable="request", label="Request", kind="paragraph"),
            WorkflowInput(variable="routing_policy", label="Routing policy", kind="paragraph"),
            WorkflowInput(variable="available_routes", label="Available routes", kind="paragraph"),
        ),
        output_name="routing_decision",
        guidance=(
            "Route the request only among the supplied destinations according to the routing policy. "
            "Recommend a route without performing the handoff."
        ),
        output_constraint=(
            "Return a JSON object with route, priority, matched_rules, rationale, and "
            "missing_information."
        ),
    ),
    "scheduling": _WorkflowSpec(
        archetype="routing",
        inputs=(
            WorkflowInput(
                variable="scheduling_request",
                label="Scheduling request",
                kind="paragraph",
            ),
            WorkflowInput(variable="availability", label="Availability", kind="paragraph"),
            WorkflowInput(variable="constraints", label="Constraints", kind="paragraph"),
        ),
        output_name="schedule_proposal",
        guidance=(
            "Propose a schedule from the supplied availability and constraints. Do not book, edit, "
            "or claim access to any calendar."
        ),
        output_constraint=(
            "Return a JSON object with proposed_slots, conflicts, assumptions, timezone, and "
            "confirmation_needed."
        ),
    ),
    "social-publishing": _WorkflowSpec(
        archetype="transformation",
        inputs=(
            WorkflowInput(variable="source_content", label="Source content", kind="paragraph"),
            WorkflowInput(
                variable="channel_constraints",
                label="Channel constraints",
                kind="paragraph",
            ),
            WorkflowInput(variable="audience", label="Audience", kind="text-input"),
        ),
        output_name="social_post_package",
        guidance=(
            "Adapt supplied content for the stated audience and channel constraints. Prepare drafts "
            "only; never publish or imply a post was published."
        ),
        output_constraint=(
            "Return channel-ready draft text, variants, factual claims checklist, accessibility notes, "
            "and required_human_review."
        ),
    ),
    "summarization": _WorkflowSpec(
        archetype="synthesis",
        inputs=(
            WorkflowInput(
                variable="source_material",
                label="Source material",
                kind="paragraph",
            ),
            WorkflowInput(
                variable="summary_purpose",
                label="Summary purpose",
                kind="text-input",
            ),
        ),
        output_name="evidence_summary",
        guidance=(
            "Summarize the supplied material for the stated purpose, preserving material caveats, "
            "disagreements, and the boundary of the evidence."
        ),
        output_constraint=(
            "Return key points, supporting evidence cues, decisions or actions explicitly present, "
            "and omitted or uncertain items."
        ),
    ),
    "translation": _WorkflowSpec(
        archetype="transformation",
        inputs=(
            WorkflowInput(variable="source_text", label="Source text", kind="paragraph"),
            WorkflowInput(
                variable="target_language",
                label="Target language",
                kind="text-input",
            ),
            WorkflowInput(variable="terminology", label="Required terminology", kind="paragraph"),
        ),
        output_name="translated_text",
        guidance=(
            "Translate the source text into the target language while preserving facts, structure, "
            "and supplied terminology."
        ),
        output_constraint=(
            "Return the translation followed by terminology notes, ambiguous source phrases, and "
            "any content intentionally left untranslated."
        ),
    ),
}


@dataclass(frozen=True)
class WorkflowDescriptor:
    """A reviewed Spine-authored use case plus optional source reference."""

    id: str
    name: str
    description: str
    categories: tuple[str, ...]
    tags: tuple[str, ...]
    intents: tuple[str, ...]
    examples: tuple[str, ...]
    archetype: str
    goal: str
    source: WorkflowSource | None = None


def load_workflow_descriptors(
    source: str | bytes,
    *,
    expected_count: int | None = None,
) -> tuple[WorkflowDescriptor, ...]:
    """Load the bounded JSON-only descriptor schema used for reviewed entries."""

    document = _parse_descriptor_json(source)
    if set(document) != {"schema_version", "templates"}:
        raise WorkflowCatalogError("workflow descriptor catalog 含不允许字段（不接受 YAML/config）")
    if document.get("schema_version") != DESCRIPTOR_SCHEMA_VERSION:
        raise WorkflowCatalogError(
            f"不支持 workflow descriptor schema_version: {document.get('schema_version')!r}"
        )
    raw_templates = document.get("templates")
    if not isinstance(raw_templates, list):
        raise WorkflowCatalogError("workflow descriptor templates 必须是 list")

    descriptors = tuple(
        _parse_descriptor(_mapping(item, f"templates[{index}]"), index=index)
        for index, item in enumerate(raw_templates)
    )
    _validate_descriptor_set(descriptors, expected_count=expected_count)
    return descriptors


def build_workflow_templates(
    descriptors: tuple[WorkflowDescriptor, ...],
    *,
    expected_count: int | None = None,
) -> tuple[WorkflowTemplate, ...]:
    """Build independent Dify 0.6 templates without consuming upstream config."""

    resolved = tuple(descriptors)
    _validate_descriptor_set(resolved, expected_count=expected_count)
    resolved_specs = tuple(
        (descriptor, *_resolve_workflow_spec(descriptor)) for descriptor in resolved
    )
    unique_specs = {spec_key: spec for _, spec_key, spec in resolved_specs}
    base_documents = {
        spec_key: parse_workflow(
            render_blueprint(
                WorkflowBlueprint(
                    name="Generated workflow",
                    goal="Process supplied input safely.",
                    output_name=spec.output_name,
                    inputs=spec.inputs,
                )
            ),
            format="yaml",
        )
        for spec_key, spec in unique_specs.items()
    }
    templates = tuple(
        _build_template(
            descriptor,
            spec_key=spec_key,
            spec=spec,
            base_document=base_documents[spec_key],
        )
        for descriptor, spec_key, spec in resolved_specs
    )
    return templates


def _resolve_workflow_spec(descriptor: WorkflowDescriptor) -> tuple[str, _WorkflowSpec]:
    use_cases = tuple(
        category.removeprefix("use-case:")
        for category in descriptor.categories
        if category.startswith("use-case:")
    )
    if len(use_cases) > 1:
        raise WorkflowCatalogError("generated descriptor 只能含一个 use-case 分类")
    if use_cases:
        use_case = use_cases[0]
        spec = _USE_CASE_SPECS.get(use_case)
        if spec is not None:
            if spec.archetype != descriptor.archetype:
                raise WorkflowCatalogError("generated descriptor use-case/archetype 不一致")
            return f"use-case:{use_case}", spec
    return f"archetype:{descriptor.archetype}", _ARCHETYPE_SPECS[descriptor.archetype]


def _build_template(
    descriptor: WorkflowDescriptor,
    *,
    spec_key: str,
    spec: _WorkflowSpec,
    base_document: dict[str, object],
) -> WorkflowTemplate:
    # Import lazily to avoid a module cycle when the built-in catalog consumes
    # the reviewed descriptor resource.  Scan before rendering: no credential-
    # shaped descriptor value may ever enter even a transient YAML string.
    from ragspine.workflows.catalog import _reject_secrets, _validate_workflow

    _reject_secrets(
        {
            "id": descriptor.id,
            "name": descriptor.name,
            "description": descriptor.description,
            "categories": list(descriptor.categories),
            "tags": list(descriptor.tags),
            "intents": list(descriptor.intents),
            "examples": list(descriptor.examples),
            "goal": descriptor.goal,
            "source": None if descriptor.source is None else descriptor.source.__dict__,
        },
        template_id=descriptor.id,
    )
    normalized_goal = make_blueprint(descriptor.goal).goal
    contract_label = spec_key.replace(":", " contract: ", 1)
    blueprint = WorkflowBlueprint(
        name=descriptor.name,
        goal=(
            f"{contract_label.capitalize()}. {spec.guidance} "
            f"Output requirement: {spec.output_constraint} "
            f"Specific use case: {normalized_goal}"
        ),
        output_name=spec.output_name,
        inputs=spec.inputs,
    )
    yaml_text = render_blueprint(blueprint)
    document = deepcopy(base_document)
    _apply_blueprint(document, blueprint)
    _validate_workflow(descriptor.id, document, runnable=True)
    return WorkflowTemplate(
        id=descriptor.id,
        name=descriptor.name,
        description=descriptor.description,
        categories=descriptor.categories,
        tags=descriptor.tags,
        intents=descriptor.intents,
        examples=descriptor.examples,
        compatibility=WorkflowCompatibility(
            format="dify",
            dsl_version=DIFY_DSL_VERSION,
            status="runnable",
        ),
        requirements=(
            WorkflowRequirement(kind="llm_provider", name=_OPENAI_PROVIDER, required=True),
        ),
        source=descriptor.source,
        workflow=document,
        yaml=yaml_text,
        sha256=hashlib.sha256(yaml_text.encode("utf-8")).hexdigest(),
    )


def _apply_blueprint(document: dict[str, object], blueprint: WorkflowBlueprint) -> None:
    """Mirror the two dynamic values in planner.render_blueprint without reparsing YAML."""

    app = _mapping(document.get("app"), "app")
    app["name"] = blueprint.name
    workflow = _mapping(document.get("workflow"), "workflow")
    graph = _mapping(workflow.get("graph"), "workflow.graph")
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        raise WorkflowCatalogError("generated archetype graph.nodes 必须是 list")
    llm_data: dict[str, Any] | None = None
    for raw_node in nodes:
        node = _mapping(raw_node, "workflow.graph.nodes[]")
        if node.get("id") == "llm_1":
            llm_data = _mapping(node.get("data"), "llm_1.data")
            break
    if llm_data is None:
        raise WorkflowCatalogError("generated archetype 缺少 llm_1")
    prompts = llm_data.get("prompt_template")
    if not isinstance(prompts, list):
        raise WorkflowCatalogError("generated archetype prompt_template 必须是 list")
    for raw_prompt in prompts:
        prompt = _mapping(raw_prompt, "llm_1.prompt_template[]")
        if prompt.get("role") == "system":
            prompt["text"] = blueprint_system_prompt(blueprint)
            return
    raise WorkflowCatalogError("generated archetype 缺少 system prompt")


def _parse_descriptor(data: dict[str, Any], *, index: int) -> WorkflowDescriptor:
    label = f"templates[{index}]"
    expected_keys = {
        "id",
        "name",
        "description",
        "categories",
        "tags",
        "intents",
        "examples",
        "archetype",
        "goal",
        "source",
    }
    unexpected = set(data).difference(expected_keys)
    if unexpected:
        raise WorkflowCatalogError(f"{label} 含不允许字段（descriptor 不接受 YAML/config）")
    raw_source = data.get("source")
    source = None
    if raw_source is not None:
        source = _parse_source(_mapping(raw_source, f"{label}.source"))
    return WorkflowDescriptor(
        id=_string(data.get("id"), f"{label}.id"),
        name=_string(data.get("name"), f"{label}.name"),
        description=_string(data.get("description"), f"{label}.description"),
        categories=_string_tuple(data.get("categories"), f"{label}.categories"),
        tags=_string_tuple(data.get("tags"), f"{label}.tags"),
        intents=_string_tuple(data.get("intents"), f"{label}.intents"),
        examples=_string_tuple(data.get("examples"), f"{label}.examples"),
        archetype=_string(data.get("archetype"), f"{label}.archetype"),
        goal=_string(data.get("goal"), f"{label}.goal"),
        source=source,
    )


def _parse_source(data: dict[str, Any]) -> WorkflowSource:
    expected_keys = {
        "provider",
        "title",
        "author",
        "upstream_id",
        "upstream_url",
        "license_status",
        "observed_metric",
        "observed_value",
        "observed_at",
    }
    unexpected = set(data).difference(expected_keys)
    if unexpected:
        raise WorkflowCatalogError("source reference 含不允许字段")
    observed_value = data.get("observed_value")
    if isinstance(observed_value, bool) or not isinstance(observed_value, int):
        raise WorkflowCatalogError("source.observed_value 必须是 int")
    if observed_value < 0:
        raise WorkflowCatalogError("source.observed_value 不得为负")
    source = WorkflowSource(
        provider=_string(data.get("provider"), "source.provider"),
        title=_string(data.get("title"), "source.title"),
        author=_string(data.get("author"), "source.author"),
        upstream_id=_string(data.get("upstream_id"), "source.upstream_id"),
        upstream_url=_string(data.get("upstream_url"), "source.upstream_url"),
        license_status=_string(data.get("license_status"), "source.license_status"),
        observed_metric=_string(data.get("observed_metric"), "source.observed_metric"),
        observed_value=observed_value,
        observed_at=_string(data.get("observed_at"), "source.observed_at"),
    )
    validate_source_reference(source)
    return source


def _validate_descriptor_set(
    descriptors: tuple[WorkflowDescriptor, ...],
    *,
    expected_count: int | None,
) -> None:
    count = len(descriptors)
    if count > MAX_GENERATED_TEMPLATES:
        raise WorkflowCatalogError(
            f"generated workflow template 数不得超过 {MAX_GENERATED_TEMPLATES}: {count}"
        )
    if expected_count is not None and count != expected_count:
        raise WorkflowCatalogError(
            f"generated workflow template 数必须是 {expected_count}: {count}"
        )
    ids: set[str] = set()
    names: set[str] = set()
    for descriptor in descriptors:
        _validate_descriptor(descriptor)
        if descriptor.id in ids:
            raise WorkflowCatalogError("generated workflow template id 重复")
        if descriptor.name in names:
            raise WorkflowCatalogError("generated workflow template name 重复")
        ids.add(descriptor.id)
        names.add(descriptor.name)


def _parse_descriptor_json(source: str | bytes) -> dict[str, object]:
    if isinstance(source, bytes):
        if len(source) > MAX_DESCRIPTOR_CATALOG_BYTES:
            raise WorkflowCatalogError(
                f"workflow descriptor catalog 超过 {MAX_DESCRIPTOR_CATALOG_BYTES} bytes"
            )
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowCatalogError("workflow descriptor catalog 必须是 UTF-8") from exc
    else:
        try:
            encoded = source.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WorkflowCatalogError("workflow descriptor catalog 必须是合法 Unicode") from exc
        if len(encoded) > MAX_DESCRIPTOR_CATALOG_BYTES:
            raise WorkflowCatalogError(
                f"workflow descriptor catalog 超过 {MAX_DESCRIPTOR_CATALOG_BYTES} bytes"
            )
        text = source

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in pairs:
            if key in out:
                raise WorkflowCatalogError("workflow descriptor JSON object 含重复键")
            out[key] = value
        return out

    def reject_constant(value: str) -> object:
        raise WorkflowCatalogError(f"workflow descriptor JSON 不允许 {value}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except WorkflowCatalogError:
        raise
    except (json.JSONDecodeError, OverflowError, RecursionError, ValueError) as exc:
        raise WorkflowCatalogError("workflow descriptor catalog JSON 无效") from exc
    if not isinstance(value, dict):
        raise WorkflowCatalogError("workflow descriptor catalog 根必须是 object")
    if not all(isinstance(key, str) for key in value):
        raise WorkflowCatalogError("workflow descriptor catalog key 必须是字符串")
    return cast(dict[str, object], value)


def _validate_descriptor(descriptor: WorkflowDescriptor) -> None:
    if _ID_RE.fullmatch(descriptor.id) is None:
        raise WorkflowCatalogError("非法 generated template id")
    for label, value in (
        ("name", descriptor.name),
        ("description", descriptor.description),
        ("goal", descriptor.goal),
    ):
        if not isinstance(value, str) or not value.strip():
            raise WorkflowCatalogError(f"generated descriptor {label} 必须是非空字符串")
    for label, values in (
        ("categories", descriptor.categories),
        ("tags", descriptor.tags),
        ("intents", descriptor.intents),
        ("examples", descriptor.examples),
    ):
        if not isinstance(values, tuple) or not values:
            raise WorkflowCatalogError(f"generated descriptor {label} 必须是非空 tuple")
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise WorkflowCatalogError(f"generated descriptor {label} 只能含非空字符串")
        if len(values) != len(set(values)):
            raise WorkflowCatalogError(f"generated descriptor {label} 不得重复")
    if descriptor.archetype not in SUPPORTED_ARCHETYPES:
        raise WorkflowCatalogError("generated descriptor archetype 不受支持")
    if descriptor.source is not None:
        validate_source_reference(descriptor.source)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowCatalogError(f"{label} 必须是 object")
    if not all(isinstance(key, str) for key in value):
        raise WorkflowCatalogError(f"{label} key 必须是字符串")
    return cast(dict[str, Any], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowCatalogError(f"{label} 必须是非空字符串")
    return value.strip()


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise WorkflowCatalogError(f"{label} 必须是非空字符串 list")
    values = tuple(_string(item, f"{label}[]") for item in value)
    if len(values) != len(set(values)):
        raise WorkflowCatalogError(f"{label} 不得含重复值")
    return values


__all__ = [
    "DESCRIPTOR_SCHEMA_VERSION",
    "MAX_DESCRIPTOR_CATALOG_BYTES",
    "MAX_GENERATED_TEMPLATES",
    "SUPPORTED_ARCHETYPES",
    "WorkflowArchetype",
    "WorkflowDescriptor",
    "build_workflow_templates",
    "load_workflow_descriptors",
]
