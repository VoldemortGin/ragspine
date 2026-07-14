"""Immutable public models for workflow catalog, matching, and scaffolding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

WorkflowCompatibilityStatus = Literal["runnable", "import_only", "blocked"]
WorkflowOrigin = Literal["template", "generated"]


@dataclass(frozen=True)
class WorkflowSource:
    """Attribution-only metadata for an upstream workflow idea.

    Bundled templates are authored from scratch by Spine.  This record never
    contains copied upstream YAML, prompts, credentials, or long descriptions.
    """

    provider: str
    title: str
    author: str
    upstream_id: str
    upstream_url: str
    license_status: str
    observed_metric: str
    observed_value: int
    observed_at: str


@dataclass(frozen=True)
class WorkflowCompatibility:
    """Wire format and local execution status of a template."""

    format: str
    dsl_version: str
    status: WorkflowCompatibilityStatus


@dataclass(frozen=True)
class WorkflowRequirement:
    """A user-supplied binding needed after importing a workflow."""

    kind: str
    name: str
    required: bool = True


@dataclass(frozen=True)
class WorkflowTemplate:
    """A validated, in-memory template from the versioned bundled catalog."""

    id: str
    name: str
    description: str
    categories: tuple[str, ...]
    tags: tuple[str, ...]
    intents: tuple[str, ...]
    examples: tuple[str, ...]
    compatibility: WorkflowCompatibility
    requirements: tuple[WorkflowRequirement, ...]
    source: WorkflowSource | None
    workflow: dict[str, object]
    yaml: str
    sha256: str

    @property
    def search_text(self) -> str:
        """Public, original metadata used for matching; never upstream YAML."""

        return " ".join(
            (
                self.name,
                self.description,
                *self.categories,
                *self.tags,
                *self.intents,
                *self.examples,
            )
        )


@dataclass(frozen=True)
class TemplateMatch:
    """One ranked template candidate."""

    template: WorkflowTemplate
    confidence: float
    matcher: str


@dataclass(frozen=True)
class ScaffoldResult:
    """A complete Dify YAML scaffold and explainable selection metadata."""

    yaml: str
    workflow: dict[str, object]
    origin: WorkflowOrigin
    template_id: str | None
    confidence: float
    matcher: str
    warnings: tuple[str, ...]
    requirements: tuple[WorkflowRequirement, ...]
    compatibility: WorkflowCompatibility
    source: WorkflowSource | None
