"""Workflow catalog, scaffolding, and preview domain errors."""

from corespine import CorespineError


class WorkflowError(CorespineError):
    """Base error for the workflow catalog/scaffolder domain."""

    code = "workflow.error"


class WorkflowCatalogError(WorkflowError):
    """The bundled workflow catalog is malformed or fails integrity checks."""

    code = "workflow.catalog"


class WorkflowTemplateNotFoundError(WorkflowCatalogError):
    """A requested workflow template id does not exist."""

    code = "workflow.template_not_found"


class WorkflowInputError(WorkflowError):
    """A scaffold request is empty, too large, or otherwise invalid."""

    code = "workflow.input"


class WorkflowMatcherError(WorkflowError):
    """A requested semantic matcher is unavailable or returned invalid data."""

    code = "workflow.matcher"


class WorkflowFormatError(WorkflowError):
    """A workflow wire document is malformed, oversized, or too complex."""

    code = "workflow.format"


class WorkflowPreviewError(WorkflowError):
    """A workflow graph cannot be represented by the safe preview contract."""

    code = "workflow.preview"
