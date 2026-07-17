"""Scalable, deterministic catalog generation from reviewed workflow descriptors."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path
from threading import Event, Lock
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from ragspine.agent.llm_provider import MockProvider
from ragspine.cli import main
from ragspine.dify.api import compile_dify_yaml
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.task_queue import FakeQueue
from ragspine.workflows.catalog import (
    GENERATED_CATALOG_RESOURCE,
    WorkflowCatalog,
    _load_builtin_catalog,
)
from ragspine.workflows.errors import WorkflowCatalogError
from ragspine.workflows.formats import parse_workflow
from ragspine.workflows.generated_catalog import (
    WorkflowDescriptor,
    build_workflow_templates,
    load_workflow_descriptors,
)
from ragspine.workflows.matching import LexicalTemplateMatcher
from ragspine.workflows.model import WorkflowSource, WorkflowTemplate
from ragspine.workflows.scaffold import scaffold_workflow

ARCHETYPES = ("analysis", "extraction", "routing", "synthesis", "transformation")
ARCHETYPE_CONTRACTS = {
    "analysis": (("evidence", "paragraph"), ("question", "text-input"), "analysis"),
    "extraction": (("source", "paragraph"), ("fields", "paragraph"), "extracted_data"),
    "routing": (("request", "paragraph"), ("policy", "paragraph"), "routing_decision"),
    "synthesis": (("materials", "paragraph"), ("audience", "text-input"), "synthesis"),
    "transformation": (
        ("content", "paragraph"),
        ("instruction", "paragraph"),
        "transformed_content",
    ),
}
ARCHETYPE_OUTPUT_CONSTRAINTS = {
    "analysis": "findings, supporting evidence, assumptions, and uncertainty",
    "extraction": "valid JSON object",
    "routing": "route, priority, rationale, and missing_information",
    "synthesis": "audience-ready brief",
    "transformation": "transformed content followed by concise change notes",
}
USE_CASE_CONTRACTS = {
    "alerting": (("events", "paragraph"), ("alert_policy", "paragraph"), "alert_decisions"),
    "analysis": (
        ("evidence", "paragraph"),
        ("analysis_question", "text-input"),
        "evidence_analysis",
    ),
    "classification": (
        ("item_to_classify", "paragraph"),
        ("taxonomy", "paragraph"),
        ("decision_rules", "paragraph"),
        "classification_result",
    ),
    "compliance-review": (
        ("material", "paragraph"),
        ("policy_criteria", "paragraph"),
        "compliance_assessment",
    ),
    "content-creation": (
        ("content_brief", "paragraph"),
        ("source_facts", "paragraph"),
        ("audience", "text-input"),
        "content_draft",
    ),
    "customer-support": (
        ("support_request", "paragraph"),
        ("support_policy", "paragraph"),
        ("known_context", "paragraph"),
        "support_resolution",
    ),
    "data-enrichment": (
        ("records", "paragraph"),
        ("enrichment_schema", "paragraph"),
        ("reference_data", "paragraph"),
        "enriched_records",
    ),
    "data-synchronization": (
        ("source_records", "paragraph"),
        ("target_schema", "paragraph"),
        ("sync_rules", "paragraph"),
        "sync_payload",
    ),
    "document-processing": (
        ("document_text", "paragraph"),
        ("processing_requirements", "paragraph"),
        "document_record",
    ),
    "execution-planning": (
        ("objective", "paragraph"),
        ("available_capabilities", "paragraph"),
        ("safety_constraints", "paragraph"),
        "execution_plan",
    ),
    "extraction": (
        ("source_document", "paragraph"),
        ("field_schema", "paragraph"),
        "extracted_fields",
    ),
    "general-assistance": (
        ("materials", "paragraph"),
        ("requested_outcome", "text-input"),
        "organized_result",
    ),
    "invoice-processing": (
        ("invoice_text", "paragraph"),
        ("validation_rules", "paragraph"),
        "invoice_review",
    ),
    "knowledge-retrieval": (
        ("knowledge_materials", "paragraph"),
        ("information_need", "text-input"),
        "knowledge_answer",
    ),
    "lead-generation": (
        ("prospect_records", "paragraph"),
        ("qualification_criteria", "paragraph"),
        "qualified_leads",
    ),
    "monitoring": (
        ("observations", "paragraph"),
        ("baseline", "paragraph"),
        ("monitoring_rules", "paragraph"),
        "signal_assessment",
    ),
    "onboarding": (
        ("procedures", "paragraph"),
        ("learner_profile", "paragraph"),
        ("scope", "text-input"),
        "onboarding_guide",
    ),
    "outreach": (
        ("recipient_context", "paragraph"),
        ("offer_context", "paragraph"),
        ("tone_constraints", "paragraph"),
        "outreach_draft",
    ),
    "question-answering": (
        ("reference_material", "paragraph"),
        ("question", "text-input"),
        "grounded_answer",
    ),
    "recommendation": (
        ("options", "paragraph"),
        ("decision_criteria", "paragraph"),
        ("context", "paragraph"),
        "recommendation_memo",
    ),
    "report-generation": (
        ("findings", "paragraph"),
        ("report_audience", "text-input"),
        ("report_structure", "paragraph"),
        "structured_report",
    ),
    "research": (
        ("research_evidence", "paragraph"),
        ("research_question", "text-input"),
        "research_brief",
    ),
    "routing": (
        ("request", "paragraph"),
        ("routing_policy", "paragraph"),
        ("available_routes", "paragraph"),
        "routing_decision",
    ),
    "scheduling": (
        ("scheduling_request", "paragraph"),
        ("availability", "paragraph"),
        ("constraints", "paragraph"),
        "schedule_proposal",
    ),
    "social-publishing": (
        ("source_content", "paragraph"),
        ("channel_constraints", "paragraph"),
        ("audience", "text-input"),
        "social_post_package",
    ),
    "summarization": (
        ("source_material", "paragraph"),
        ("summary_purpose", "text-input"),
        "evidence_summary",
    ),
    "translation": (
        ("source_text", "paragraph"),
        ("target_language", "text-input"),
        ("terminology", "paragraph"),
        "translated_text",
    ),
}
DIFY_REFERENCE_ID = "79472c38-268e-4e8b-9ac3-a4584260b708"
DIFY_OTHER_REFERENCE_ID = "838f5731-8c88-4d49-bb13-d9c586ea5dc5"


def _descriptor_payload(descriptors: tuple[WorkflowDescriptor, ...]) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "templates": [
                {
                    "id": descriptor.id,
                    "name": descriptor.name,
                    "description": descriptor.description,
                    "categories": descriptor.categories,
                    "tags": descriptor.tags,
                    "intents": descriptor.intents,
                    "examples": descriptor.examples,
                    "archetype": descriptor.archetype,
                    "goal": descriptor.goal,
                    "source": None,
                }
                for descriptor in descriptors
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")


@pytest.fixture(scope="module")
def thousand_descriptors() -> tuple[WorkflowDescriptor, ...]:
    """A synthetic scale fixture, not built-in catalog attribution or content."""

    return tuple(
        WorkflowDescriptor(
            id=f"sector-{industry:02d}-case-{use_case:02d}",
            name=f"Sector{industry:02d} Case{use_case:02d}",
            description=(f"Independent workflow for sector{industry:02d}, use case{use_case:02d}."),
            categories=(
                f"industry:sector{industry:02d}",
                f"use-case:case{use_case:02d}",
                f"archetype:{ARCHETYPES[use_case % len(ARCHETYPES)]}",
            ),
            tags=(f"sector{industry:02d}", f"case{use_case:02d}"),
            intents=(f"handle sector{industry:02d} case{use_case:02d}",),
            examples=(f"Create sector{industry:02d} case{use_case:02d} workflow",),
            archetype=ARCHETYPES[use_case % len(ARCHETYPES)],
            goal=f"Handle reviewed use case{use_case:02d} for sector{industry:02d}.",
            source=None,
        )
        for industry in range(40)
        for use_case in range(25)
    )


@pytest.fixture(scope="module")
def thousand_templates(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
) -> tuple[WorkflowTemplate, ...]:
    return build_workflow_templates(thousand_descriptors, expected_count=1000)


@pytest.fixture(scope="module")
def builtin_generated_descriptors() -> tuple[WorkflowDescriptor, ...]:
    payload = (
        files("ragspine.workflows.templates").joinpath(GENERATED_CATALOG_RESOURCE).read_bytes()
    )
    return load_workflow_descriptors(payload, expected_count=993)


def test_builder_produces_exactly_1000_unique_cross_industry_templates(
    thousand_templates: tuple[WorkflowTemplate, ...],
) -> None:
    assert len(thousand_templates) == 1000
    assert len({template.id for template in thousand_templates}) == 1000
    assert len({template.name for template in thousand_templates}) == 1000
    assert len({template.sha256 for template in thousand_templates}) == 1000

    industries = {
        category
        for template in thousand_templates
        for category in template.categories
        if category.startswith("industry:")
    }
    use_cases = {
        category
        for template in thousand_templates
        for category in template.categories
        if category.startswith("use-case:")
    }
    assert len(industries) == 40
    assert len(use_cases) == 25


def test_builder_is_deterministic(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
    thousand_templates: tuple[WorkflowTemplate, ...],
) -> None:
    rebuilt = build_workflow_templates(thousand_descriptors, expected_count=1000)

    assert [(template.id, template.yaml, template.sha256) for template in rebuilt] == [
        (template.id, template.yaml, template.sha256) for template in thousand_templates
    ]


def test_every_generated_template_has_a_matching_safe_dify_document(
    thousand_templates: tuple[WorkflowTemplate, ...],
) -> None:
    for template in thousand_templates:
        assert template.source is None
        assert template.compatibility.format == "dify"
        assert template.compatibility.dsl_version == "0.6.0"
        assert template.compatibility.status == "runnable"
        assert hashlib.sha256(template.yaml.encode("utf-8")).hexdigest() == template.sha256
        assert template.workflow["version"] == "0.6.0"
        workflow = template.workflow["workflow"]
        assert isinstance(workflow, dict)
        assert workflow["environment_variables"] == []
        assert workflow["conversation_variables"] == []

    for archetype in ARCHETYPES:
        representative = next(
            template
            for template in thousand_templates
            if f"archetype:{archetype}" in template.categories
        )
        assert parse_workflow(representative.yaml, format="yaml") == representative.workflow
        assert compile_dify_yaml(representative.yaml).code.warnings == ()


def test_each_archetype_has_a_distinct_practical_input_and_output_contract(
    thousand_templates: tuple[WorkflowTemplate, ...],
) -> None:
    user_prompts: set[str] = set()
    system_prompts: set[str] = set()

    for archetype, contract in ARCHETYPE_CONTRACTS.items():
        representative = next(
            template
            for template in thousand_templates
            if f"archetype:{archetype}" in template.categories
        )
        workflow = representative.workflow["workflow"]
        assert isinstance(workflow, dict)
        graph = workflow["graph"]
        assert isinstance(graph, dict)
        raw_nodes = graph["nodes"]
        assert isinstance(raw_nodes, list)
        nodes = {
            node["data"]["type"]: node["data"]
            for node in raw_nodes
            if isinstance(node, dict) and isinstance(node.get("data"), dict)
        }

        start = nodes["start"]
        variables = start["variables"]
        assert isinstance(variables, list)
        expected_inputs = contract[:-1]
        assert tuple((item["variable"], item["type"]) for item in variables) == expected_inputs
        assert all(item["required"] is True for item in variables)

        llm = nodes["llm"]
        prompts = llm["prompt_template"]
        assert isinstance(prompts, list)
        system_prompt = next(item["text"] for item in prompts if item["role"] == "system")
        user_prompt = next(item["text"] for item in prompts if item["role"] == "user")
        assert isinstance(system_prompt, str)
        assert isinstance(user_prompt, str)
        assert ARCHETYPE_OUTPUT_CONSTRAINTS[archetype] in system_prompt
        for variable, _ in expected_inputs:
            assert f"{{{{#start_1.{variable}#}}}}" in user_prompt
        system_prompts.add(system_prompt)
        user_prompts.add(user_prompt)

        end = nodes["end"]
        outputs = end["outputs"]
        assert isinstance(outputs, list)
        assert [item["variable"] for item in outputs] == [contract[-1]]

    assert len(system_prompts) == len(ARCHETYPES)
    assert len(user_prompts) == len(ARCHETYPES)


def test_builtin_starters_have_27_use_case_specific_contracts(
    builtin_generated_descriptors: tuple[WorkflowDescriptor, ...],
) -> None:
    descriptor_by_use_case: dict[str, WorkflowDescriptor] = {}
    for descriptor in builtin_generated_descriptors:
        use_case = next(
            category.removeprefix("use-case:")
            for category in descriptor.categories
            if category.startswith("use-case:")
        )
        descriptor_by_use_case.setdefault(use_case, descriptor)

    assert set(descriptor_by_use_case) == set(USE_CASE_CONTRACTS)
    representatives = build_workflow_templates(
        tuple(descriptor_by_use_case.values()),
        expected_count=len(USE_CASE_CONTRACTS),
    )
    observed_contracts: set[tuple[tuple[str, ...], str]] = set()
    system_prompts: set[str] = set()

    for template in representatives:
        use_case = next(
            category.removeprefix("use-case:")
            for category in template.categories
            if category.startswith("use-case:")
        )
        contract = USE_CASE_CONTRACTS[use_case]
        workflow = template.workflow["workflow"]
        assert isinstance(workflow, dict)
        graph = workflow["graph"]
        assert isinstance(graph, dict)
        raw_nodes = graph["nodes"]
        assert isinstance(raw_nodes, list)
        nodes = {
            node["data"]["type"]: node["data"]
            for node in raw_nodes
            if isinstance(node, dict) and isinstance(node.get("data"), dict)
        }

        variables = nodes["start"]["variables"]
        assert isinstance(variables, list)
        expected_inputs = contract[:-1]
        assert tuple((item["variable"], item["type"]) for item in variables) == expected_inputs
        assert all(item["required"] is True for item in variables)

        prompts = nodes["llm"]["prompt_template"]
        assert isinstance(prompts, list)
        system_prompt = next(item["text"] for item in prompts if item["role"] == "system")
        user_prompt = next(item["text"] for item in prompts if item["role"] == "user")
        assert f"Use-case contract: {use_case}." in system_prompt
        for variable, _ in expected_inputs:
            assert f"{{{{#start_1.{variable}#}}}}" in user_prompt

        outputs = nodes["end"]["outputs"]
        assert isinstance(outputs, list)
        assert [item["variable"] for item in outputs] == [contract[-1]]
        observed_contracts.add((tuple(item[0] for item in expected_inputs), contract[-1]))
        system_prompts.add(system_prompt)
        assert compile_dify_yaml(template.yaml).code.warnings == ()

    assert len(observed_contracts) == len(USE_CASE_CONTRACTS)
    assert len(system_prompts) == len(USE_CASE_CONTRACTS)


def test_source_reference_is_optional_metadata_and_never_copied_into_yaml() -> None:
    source = WorkflowSource(
        provider="n8n",
        title="External Reference Title",
        author="Reference Author",
        upstream_id="42",
        upstream_url="https://n8n.io/workflows/42-reference/",
        license_status="reference-only-not-redistributed",
        observed_metric="totalViews",
        observed_value=42,
        observed_at="2026-07-15T00:00:00+08:00",
    )
    descriptor = WorkflowDescriptor(
        id="independent-reference-rewrite",
        name="Independent Reference Rewrite",
        description="A Spine-authored workflow with attribution-only metadata.",
        categories=("industry:test", "use-case:analysis", "archetype:analysis"),
        tags=("independent", "reference"),
        intents=("analyze supplied material",),
        examples=("Analyze this supplied material",),
        archetype="analysis",
        goal="Analyze supplied material without importing external configuration or prompts.",
        source=source,
    )

    template = build_workflow_templates((descriptor,), expected_count=1)[0]

    alternate_source = WorkflowSource(
        provider="n8n",
        title="Entirely Different Listing Metadata",
        author="Different Reference Author",
        upstream_id="43",
        upstream_url="https://n8n.io/workflows/43-other-reference/",
        license_status="reference-only-not-redistributed",
        observed_metric="totalViews",
        observed_value=999,
        observed_at="2026-07-15T00:00:00+08:00",
    )
    alternate = build_workflow_templates(
        (WorkflowDescriptor(**{**descriptor.__dict__, "source": alternate_source}),),
        expected_count=1,
    )[0]

    assert template.source is source
    assert source.upstream_url not in template.yaml
    assert source.title not in template.yaml
    assert source.author not in template.yaml
    assert alternate_source.upstream_url not in alternate.yaml
    assert alternate_source.title not in alternate.yaml
    assert alternate_source.author not in alternate.yaml
    assert alternate.yaml == template.yaml
    assert alternate.sha256 == template.sha256


@pytest.mark.parametrize(
    ("provider", "upstream_id", "url", "metric"),
    [
        (
            "dify",
            DIFY_REFERENCE_ID,
            f"https://marketplace.dify.ai/template/example?templateId={DIFY_REFERENCE_ID}",
            "usage_count",
        ),
        (
            "dify",
            DIFY_REFERENCE_ID,
            "https://marketplace.dify.ai/template/author/example?creationType=templates&"
            f"templateId={DIFY_REFERENCE_ID}",
            "usage_count_rounded",
        ),
        ("n8n", "42", "https://n8n.io/workflows/42-example/", "totalViews"),
        ("n8n", "42", "https://n8n.io/workflows/42/", "totalViews"),
    ],
)
def test_source_reference_accepts_only_bound_provider_host_and_metric(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
    provider: str,
    upstream_id: str,
    url: str,
    metric: str,
) -> None:
    source = WorkflowSource(
        provider=provider,
        title="Reviewed public reference",
        author="Public author",
        upstream_id=upstream_id,
        upstream_url=url,
        license_status="reference-only-not-redistributed",
        observed_metric=metric,
        observed_value=42,
        observed_at="2026-07-15T00:00:00+08:00",
    )
    descriptor = WorkflowDescriptor(
        **{
            **thousand_descriptors[0].__dict__,
            "id": f"valid-{provider}-{metric.replace('_', '-').lower()}",
            "name": f"Valid {provider} {metric}",
            "source": source,
        }
    )

    template = build_workflow_templates((descriptor,), expected_count=1)[0]

    assert template.source == source


@pytest.mark.parametrize(
    ("provider", "upstream_id", "url", "metric"),
    [
        ("github", "42", "https://github.com/example/workflow", "stars"),
        (
            "dify",
            DIFY_REFERENCE_ID,
            "https://marketplace.dify.ai.evil.example/template/example?"
            f"templateId={DIFY_REFERENCE_ID}",
            "usage_count",
        ),
        (
            "dify",
            DIFY_REFERENCE_ID,
            f"https://marketplace.dify.ai:443/template/example?templateId={DIFY_REFERENCE_ID}",
            "usage_count",
        ),
        (
            "dify",
            DIFY_REFERENCE_ID,
            "https://sk-proj-sensitive-value-123456789@marketplace.dify.ai/template/"
            f"example?templateId={DIFY_REFERENCE_ID}",
            "usage_count",
        ),
        (
            "dify",
            DIFY_REFERENCE_ID,
            f"https://marketplace.dify.ai/template/example?templateId={DIFY_REFERENCE_ID}",
            "totalViews",
        ),
        ("n8n", "42", "https://n8n.io/workflows/42-example/", "usage_count"),
        ("n8n", "43", "https://n8n.io/workflows/42-example/", "totalViews"),
        ("n8n", "public-42", "https://n8n.io/workflows/public-42/", "totalViews"),
        (
            "dify",
            DIFY_REFERENCE_ID,
            f"https://marketplace.dify.ai/template/example?templateId={DIFY_OTHER_REFERENCE_ID}",
            "usage_count",
        ),
        (
            "dify",
            DIFY_REFERENCE_ID,
            "https://marketplace.dify.ai/template/example",
            "usage_count",
        ),
        (
            "dify",
            DIFY_REFERENCE_ID,
            f"https://marketplace.dify.ai/templates/example?templateId={DIFY_REFERENCE_ID}",
            "usage_count",
        ),
    ],
)
def test_source_reference_rejects_unbound_provider_host_port_userinfo_or_metric(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
    provider: str,
    upstream_id: str,
    url: str,
    metric: str,
) -> None:
    source = WorkflowSource(
        provider=provider,
        title="Reviewed public reference",
        author="Public author",
        upstream_id=upstream_id,
        upstream_url=url,
        license_status="reference-only-not-redistributed",
        observed_metric=metric,
        observed_value=42,
        observed_at="2026-07-15T00:00:00+08:00",
    )
    descriptor = WorkflowDescriptor(
        **{
            **thousand_descriptors[0].__dict__,
            "id": "invalid-source-binding",
            "name": "Invalid Source Binding",
            "source": source,
        }
    )

    with pytest.raises(WorkflowCatalogError) as error:
        build_workflow_templates((descriptor,), expected_count=1)

    assert "sk-proj-sensitive-value" not in str(error.value)
    assert url not in str(error.value)


def test_descriptor_json_loader_accepts_null_and_reference_sources() -> None:
    payload = {
        "schema_version": 1,
        "templates": [
            {
                "id": "reviewed-analysis",
                "name": "Reviewed Analysis",
                "description": "Independently authored analysis workflow.",
                "categories": ["analysis"],
                "tags": ["reviewed"],
                "intents": ["analyze reviewed material"],
                "examples": ["Analyze this reviewed material"],
                "archetype": "analysis",
                "goal": "Analyze the supplied material.",
                "source": None,
            }
        ],
    }

    descriptors = load_workflow_descriptors(json.dumps(payload).encode("utf-8"), expected_count=1)

    assert descriptors[0].source is None
    assert descriptors[0].id == "reviewed-analysis"


def test_descriptor_json_loader_scales_to_exactly_1000_reviewed_entries(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
) -> None:
    payload = _descriptor_payload(thousand_descriptors)
    started = perf_counter()

    loaded = load_workflow_descriptors(payload, expected_count=1000)
    elapsed = perf_counter() - started

    assert loaded == thousand_descriptors
    assert elapsed < 2.0


def test_descriptor_loader_rejects_upstream_configuration_and_ambiguous_json() -> None:
    forbidden_config = {
        "schema_version": 1,
        "templates": [
            {
                "id": "must-not-copy-upstream-config",
                "name": "Must Not Copy Upstream Config",
                "description": "Metadata-only reference.",
                "categories": ["analysis"],
                "tags": ["reviewed"],
                "intents": ["analyze material"],
                "examples": ["Analyze this material"],
                "archetype": "analysis",
                "goal": "Analyze only the supplied material.",
                "source": None,
                "yaml": "app: copied-upstream-config",
            }
        ],
    }

    with pytest.raises(WorkflowCatalogError, match="YAML/config"):
        load_workflow_descriptors(json.dumps(forbidden_config), expected_count=1)
    root_config = {
        "schema_version": 1,
        "templates": [],
        "workflow": {"copied": "upstream config"},
    }
    with pytest.raises(WorkflowCatalogError, match="YAML/config"):
        load_workflow_descriptors(json.dumps(root_config))
    with pytest.raises(WorkflowCatalogError, match="重复键"):
        load_workflow_descriptors('{"schema_version": 1, "schema_version": 1, "templates": []}')


def test_source_schema_error_does_not_reflect_sensitive_field_name() -> None:
    sensitive = "sk-proj-sensitive-field-name-123456789"
    payload = {
        "schema_version": 1,
        "templates": [
            {
                "id": "safe-source-error",
                "name": "Safe Source Error",
                "description": "Reject ambiguous source metadata safely.",
                "categories": ["analysis"],
                "tags": ["reviewed"],
                "intents": ["analyze material"],
                "examples": ["Analyze this material"],
                "archetype": "analysis",
                "goal": "Analyze only supplied material.",
                "source": {
                    "provider": "n8n",
                    "title": "Reference",
                    "author": "Author",
                    "upstream_id": "42",
                    "upstream_url": "https://n8n.io/workflows/42-example/",
                    "license_status": "reference-only-not-redistributed",
                    "observed_metric": "totalViews",
                    "observed_value": 42,
                    "observed_at": "2026-07-15T00:00:00+08:00",
                    sensitive: "must be rejected",
                },
            }
        ],
    }

    with pytest.raises(WorkflowCatalogError) as error:
        load_workflow_descriptors(json.dumps(payload), expected_count=1)

    assert sensitive not in str(error.value)


def test_generated_descriptor_rejects_credential_shaped_content_without_reflection(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
) -> None:
    secret = "sk-proj-this-must-never-be-reflected-123456789"
    unsafe = WorkflowDescriptor(
        **{
            **thousand_descriptors[0].__dict__,
            "id": "credential-shaped-goal",
            "name": "Credential Shaped Goal",
            "goal": f"Analyze input with {secret}",
        }
    )

    with pytest.raises(WorkflowCatalogError) as error:
        build_workflow_templates((unsafe,), expected_count=1)

    assert secret not in str(error.value)


def test_generated_descriptor_secret_shaped_id_is_never_reflected(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
) -> None:
    secret_id = "sk-proj-secretidmustneverbereflected123456789"
    unsafe = WorkflowDescriptor(
        **{
            **thousand_descriptors[0].__dict__,
            "id": secret_id,
            "name": "Secret-shaped ID",
        }
    )

    with pytest.raises(WorkflowCatalogError) as error:
        build_workflow_templates((unsafe,), expected_count=1)

    assert secret_id not in str(error.value)


@pytest.mark.parametrize(
    ("field", "secret"),
    [
        ("goal", "AIza" + "A" * 35),
        ("description", "sk_live_" + "B" * 24),
        ("examples", "xoxb-" + "C" * 32),
        ("goal", "SG." + "D" * 22 + "." + "E" * 43),
        ("description", "SK" + "0123456789abcdef" * 2),
    ],
)
def test_generated_descriptor_rejects_common_credentials_in_public_metadata(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
    field: str,
    secret: str,
) -> None:
    replacement: object = f"Unsafe metadata {secret}"
    if field == "examples":
        replacement = (f"Unsafe example {secret}",)
    unsafe = WorkflowDescriptor(
        **{
            **thousand_descriptors[0].__dict__,
            "id": f"unsafe-{field}",
            "name": f"Unsafe {field}",
            field: replacement,
        }
    )

    with pytest.raises(WorkflowCatalogError) as error:
        build_workflow_templates((unsafe,), expected_count=1)

    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "near_miss",
    [
        "SG." + "A" * 21 + "." + "B" * 43,
        "SG." + "A" * 22 + "." + "B" * 42,
        "SK" + "0123456789abcdef" + "0123456789abcde",
        "benign-high-entropy-" + "Z" * 80,
    ],
)
def test_credential_detection_does_not_use_generic_entropy_scanning(
    thousand_descriptors: tuple[WorkflowDescriptor, ...], near_miss: str
) -> None:
    descriptor = WorkflowDescriptor(
        **{
            **thousand_descriptors[0].__dict__,
            "id": "benign-near-miss",
            "name": "Benign Near Miss",
            "goal": f"Analyze supplied material labeled {near_miss}.",
        }
    )

    templates = build_workflow_templates((descriptor,), expected_count=1)

    assert templates[0].id == "benign-near-miss"


def test_credential_is_rejected_before_any_yaml_render(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ragspine.workflows import generated_catalog as generated_module

    secret = "AIza" + "D" * 35
    unsafe = WorkflowDescriptor(
        **{
            **thousand_descriptors[0].__dict__,
            "id": "unsafe-pre-render-goal",
            "name": "Unsafe Pre-render Goal",
            "goal": f"Never render {secret}",
        }
    )
    real_render = generated_module.render_blueprint
    rendered_secret = False

    def guarded_render(blueprint) -> str:
        nonlocal rendered_secret
        if secret in blueprint.goal:
            rendered_secret = True
        return real_render(blueprint)

    monkeypatch.setattr(generated_module, "render_blueprint", guarded_render)

    with pytest.raises(WorkflowCatalogError):
        build_workflow_templates((unsafe,), expected_count=1)

    assert rendered_secret is False


@pytest.mark.parametrize(
    ("observed_at", "license_status"),
    [
        ("2026-07-15T00:00:00", "unknown-not-redistributed"),
        ("not-a-time", "unknown-not-redistributed"),
        ("2026-07-15T00:00:00+08:00", "copied-without-review"),
    ],
)
def test_source_reference_requires_timezone_and_allowlisted_license(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
    observed_at: str,
    license_status: str,
) -> None:
    source = WorkflowSource(
        provider="n8n",
        title="Reviewed public reference",
        author="Public author",
        upstream_id="42",
        upstream_url="https://n8n.io/workflows/42-example/",
        license_status=license_status,
        observed_metric="totalViews",
        observed_value=42,
        observed_at=observed_at,
    )
    descriptor = WorkflowDescriptor(
        **{
            **thousand_descriptors[0].__dict__,
            "id": "invalid-source-time-license",
            "name": "Invalid Source Time License",
            "source": source,
        }
    )

    with pytest.raises(WorkflowCatalogError) as error:
        build_workflow_templates((descriptor,), expected_count=1)

    assert observed_at not in str(error.value)
    assert license_status not in str(error.value)


def test_builtin_loader_combines_7_curated_and_993_reviewed_descriptors(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
    tmp_path: Path,
) -> None:
    destination = tmp_path / "templates"
    destination.mkdir()
    source = files("ragspine.workflows.templates")
    for resource in source.iterdir():
        if resource.is_file() and resource.name != "__init__.py":
            destination.joinpath(resource.name).write_bytes(resource.read_bytes())
    destination.joinpath(GENERATED_CATALOG_RESOURCE).write_bytes(
        _descriptor_payload(thousand_descriptors[:993])
    )
    started = perf_counter()

    catalog = _load_builtin_catalog(destination)
    templates = catalog.list()
    elapsed = perf_counter() - started

    assert len(templates) == 1000
    assert templates[0].id == "rag-paper-qa"
    assert templates[7].id == "sector-00-case-00"
    assert templates[-1].id == thousand_descriptors[992].id
    # Cold-load timing is intentionally a broad regression guard: full-suite
    # runs share CPU and filesystem bandwidth with other tests (and CI jobs),
    # so leave cross-platform scheduling headroom over the ~1.3 s local
    # isolated baseline.
    assert elapsed < 5.0


def test_builtin_catalog_cold_load_is_single_flight_and_cache_is_clearable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ragspine.workflows import catalog as catalog_module

    real_loader = catalog_module._load_builtin_catalog
    started = Event()
    release = Event()
    counter_lock = Lock()
    calls = 0

    def blocking_loader(root):
        nonlocal calls
        with counter_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=10)
        return real_loader(root)

    catalog_module.clear_builtin_catalog_cache()
    monkeypatch.setattr(catalog_module, "_load_builtin_catalog", blocking_loader)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(catalog_module.load_builtin_catalog)
            assert started.wait(timeout=10)
            second_future = pool.submit(catalog_module.load_builtin_catalog)
            release.set()
            first = first_future.result(timeout=20)
            second = second_future.result(timeout=20)

        assert calls == 1
        assert first is second
        assert catalog_module.load_builtin_catalog() is first

        catalog_module.clear_builtin_catalog_cache()
        rebuilt = catalog_module.load_builtin_catalog()
        assert calls == 2
        assert rebuilt is not first
    finally:
        catalog_module.clear_builtin_catalog_cache()


def test_builder_rejects_wrong_count_duplicate_ids_and_unknown_archetypes(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
) -> None:
    with pytest.raises(WorkflowCatalogError, match="1000"):
        build_workflow_templates(thousand_descriptors[:-1], expected_count=1000)
    with pytest.raises(WorkflowCatalogError, match="id"):
        build_workflow_templates(
            (thousand_descriptors[0], thousand_descriptors[0]), expected_count=2
        )

    invalid = WorkflowDescriptor(
        **{
            **thousand_descriptors[0].__dict__,
            "id": "unknown-archetype",
            "name": "Unknown Archetype",
            "archetype": "network-execution",
        }
    )
    with pytest.raises(WorkflowCatalogError, match="archetype"):
        build_workflow_templates((invalid,), expected_count=1)


def test_1000_template_build_and_catalog_load_stay_within_latency_budget(
    thousand_descriptors: tuple[WorkflowDescriptor, ...],
) -> None:
    started = perf_counter()
    templates = build_workflow_templates(thousand_descriptors, expected_count=1000)
    catalog = WorkflowCatalog(templates)
    elapsed = perf_counter() - started

    assert len(catalog.list()) == 1000
    # This is another coarse wall-clock regression guard, not a microbenchmark.
    # Keep enough margin for contended macOS/Windows/CI runners; the isolated
    # local baseline is about 0.9 s.
    assert elapsed < 4.0


def test_natural_language_scaffold_over_1000_templates_stays_within_latency_budget(
    thousand_templates: tuple[WorkflowTemplate, ...],
) -> None:
    catalog = WorkflowCatalog(thousand_templates)
    started = perf_counter()
    result = scaffold_workflow(
        "Create sector17 case09 workflow",
        catalog=catalog,
        matcher=LexicalTemplateMatcher(),
    )
    elapsed = perf_counter() - started

    assert result.origin == "template"
    assert result.template_id == "sector-17-case-09"
    assert elapsed < 2.0


def test_generated_catalog_works_through_cli_list_show_and_implicit_create(
    thousand_templates: tuple[WorkflowTemplate, ...],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ragspine.workflows import catalog as catalog_module
    from ragspine.workflows import scaffold as scaffold_module

    catalog = WorkflowCatalog(thousand_templates)
    monkeypatch.setattr(catalog_module, "load_builtin_catalog", lambda: catalog)
    monkeypatch.setattr(scaffold_module, "load_builtin_catalog", lambda: catalog)

    assert main(["workflow", "list"]) == 0
    listing = capsys.readouterr().out.strip().splitlines()
    assert len(listing) == 1000
    assert listing[0].startswith("sector-00-case-00\t")

    assert main(["workflow", "show", "sector-17-case-09", "--format", "json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["app"]["name"] == "Sector17 Case09"

    assert (
        main(
            [
                "Create sector17 case09 workflow",
                "--matcher",
                "lexical",
                "--format",
                "json",
                "--stdout",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["app"]["name"] == "Sector17 Case09"


def test_generated_catalog_works_through_template_and_scaffold_apis(
    thousand_templates: tuple[WorkflowTemplate, ...],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from ragspine.workflows import catalog as catalog_module

    catalog = WorkflowCatalog(thousand_templates)
    monkeypatch.setattr(catalog_module, "load_builtin_catalog", lambda: catalog)
    app = create_app(
        ServiceConfig(db_path=str(tmp_path / "fact.db")),
        provider=MockProvider(),
        queue=FakeQueue(),
        faq_cache=FAQCache.empty(),
        workflow_matcher=LexicalTemplateMatcher(),
    )
    client = TestClient(app)

    listing = client.get("/v1/workflow-templates")
    assert listing.status_code == 200
    listing_body = listing.json()
    assert listing_body["total"] == 1000
    assert len(listing_body["templates"]) == 100

    detail = client.get("/v1/workflow-templates/sector-17-case-09")
    assert detail.status_code == 200
    assert detail.json()["workflow"]["app"]["name"] == "Sector17 Case09"

    scaffold = client.post(
        "/v1/workflow-scaffold",
        json={"description": "Create sector17 case09 workflow"},
    )
    assert scaffold.status_code == 200
    assert scaffold.json()["origin"] == "template"
    assert scaffold.json()["template_id"] == "sector-17-case-09"
