"""Public natural-language workflow scaffolding facade."""

from __future__ import annotations

from ragspine.workflows.catalog import WorkflowCatalog, load_builtin_catalog
from ragspine.workflows.errors import WorkflowInputError
from ragspine.workflows.formats import parse_workflow
from ragspine.workflows.matching import (
    LexicalTemplateMatcher,
    TemplateMatcher,
    choose_reusable,
)
from ragspine.workflows.model import (
    ScaffoldResult,
    WorkflowCompatibility,
    WorkflowRequirement,
)
from ragspine.workflows.planner import DIFY_DSL_VERSION, generate_dify_yaml, normalize_description


def scaffold_workflow(
    description: str,
    *,
    catalog: WorkflowCatalog | None = None,
    matcher: TemplateMatcher | None = None,
    template_id: str | None = None,
    reuse: bool = True,
    threshold: float | None = None,
    margin: float | None = None,
) -> ScaffoldResult:
    """Reuse a close runnable template or render a constrained Dify workflow.

    This function is pure with respect to the caller's filesystem: it returns
    YAML and metadata but never writes or executes anything.
    """

    normalized = normalize_description(description)
    resolved_catalog = catalog or load_builtin_catalog()

    if template_id is not None:
        selected = resolved_catalog.get(template_id)
        if selected.compatibility.status != "runnable":
            raise WorkflowInputError(f"template {template_id!r} 不可运行")
        return ScaffoldResult(
            yaml=selected.yaml,
            workflow=selected.workflow,
            origin="template",
            template_id=selected.id,
            confidence=1.0,
            matcher="explicit",
            warnings=(),
            requirements=selected.requirements,
            compatibility=selected.compatibility,
            source=selected.source,
        )

    resolved_matcher = matcher or LexicalTemplateMatcher()
    if reuse:
        matches = resolved_matcher.rank(normalized, resolved_catalog._matching_refs())
        selected_match = choose_reusable(
            matches,
            threshold=(resolved_matcher.reuse_threshold if threshold is None else threshold),
            margin=resolved_matcher.reuse_margin if margin is None else margin,
        )
        if selected_match is not None:
            # Matcher candidates contain metadata only.  Resolve the id back
            # through the catalog to return one defensive workflow clone.
            selected = resolved_catalog.get(selected_match.template.id)
            return ScaffoldResult(
                yaml=selected.yaml,
                workflow=selected.workflow,
                origin="template",
                template_id=selected.id,
                confidence=selected_match.confidence,
                matcher=selected_match.matcher,
                warnings=(),
                requirements=selected.requirements,
                compatibility=selected.compatibility,
                source=selected.source,
            )

    generated_yaml = generate_dify_yaml(normalized)
    return ScaffoldResult(
        yaml=generated_yaml,
        workflow=parse_workflow(generated_yaml, format="yaml"),
        origin="generated",
        template_id=None,
        confidence=0.0,
        matcher=resolved_matcher.name if reuse else "disabled",
        warnings=(),
        requirements=(
            WorkflowRequirement(
                kind="llm_provider", name="langgenius/openai/openai", required=True
            ),
        ),
        compatibility=WorkflowCompatibility(
            format="dify", dsl_version=DIFY_DSL_VERSION, status="runnable"
        ),
        source=None,
    )


__all__ = ["scaffold_workflow"]
