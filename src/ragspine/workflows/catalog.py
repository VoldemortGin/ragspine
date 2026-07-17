"""Validated, immutable workflow catalog loaded from package resources.

The catalog is a release-time snapshot.  Runtime code never downloads or
executes an upstream workflow.  YAML paths are an id-to-resource allowlist and
are integrity checked before entering memory.
"""

from __future__ import annotations

import hashlib
import re
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import replace
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any, cast

from ragspine.workflows.errors import (
    WorkflowCatalogError,
    WorkflowFormatError,
    WorkflowTemplateNotFoundError,
)
from ragspine.workflows.formats import parse_workflow, read_bounded_file
from ragspine.workflows.model import (
    WorkflowCompatibility,
    WorkflowRequirement,
    WorkflowSource,
    WorkflowTemplate,
)
from ragspine.workflows.source_policy import validate_source_reference

CATALOG_SCHEMA_VERSION = 1
BUILTIN_CATALOG_TARGET_SIZE = 1000
GENERATED_CATALOG_RESOURCE = "generated-catalog.json"
MAX_CATALOG_BYTES = 128 * 1024
MAX_TEMPLATE_BYTES = 128 * 1024
MAX_TEMPLATES = 128
_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_NODE_TYPES = frozenset(
    {
        "answer",
        "end",
        "if-else",
        "iteration",
        "iteration-start",
        "knowledge-retrieval",
        "llm",
        "parameter-extractor",
        "start",
        "template-transform",
    }
)
_ALLOWED_PLUGIN_IDENTIFIERS = frozenset(
    {
        "langgenius/openai:0.3.8@592c8252795b5f75807de2d609a03196ed02596b409f7642b4a07548c7ff57ef",
    }
)
_ALLOWED_MODEL_PROVIDERS = frozenset({"langgenius/openai/openai"})
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "authorization",
        "auth_token",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_security_token",
        "aws_session_token",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "token",
        "webhook_secret",
    }
)
_PLACEHOLDER_LITERALS = frozenset(
    {"~", "null", "none", "unset", "redacted", "change_me", "change_me_before_use"}
    | {f"your_{key}" for key in _SENSITIVE_KEYS}
)
_PLACEHOLDER_PATTERNS = (
    re.compile(r"\$\{[a-z_][a-z0-9_]*\}"),
    re.compile(r"\{\{\s*#?[a-z0-9_.-]+#?\s*\}\}"),
    re.compile(r"<[a-z][a-z0-9_.-]*>"),
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|ant-api\d*-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"(?<![A-Za-z0-9_])whsec_[A-Za-z0-9]{16,}(?![A-Za-z0-9_])"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(
        r"(?<![A-Za-z0-9_-])SG\.[A-Za-z0-9_-]{22,}\.[A-Za-z0-9_-]{43,}"
        r"(?![A-Za-z0-9_-])"
    ),
    re.compile(r"(?<![A-Za-z0-9_])SK[0-9A-Fa-f]{32}(?![A-Za-z0-9_])"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}={0,2}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"),
)


class WorkflowCatalog:
    """Read-only collection of validated workflow templates."""

    def __init__(self, templates: tuple[WorkflowTemplate, ...]) -> None:
        ids = [template.id for template in templates]
        if len(ids) != len(set(ids)):
            raise WorkflowCatalogError("workflow catalog template id 必须唯一")
        self._templates = tuple(_clone_template(template) for template in templates)
        self._by_id = {template.id: template for template in self._templates}
        self._template_refs = tuple(
            replace(template, workflow={}, yaml="") for template in self._templates
        )
        self._runnable_template_refs = tuple(
            template
            for template in self._template_refs
            if template.compatibility.status == "runnable"
        )

    @classmethod
    def default(cls) -> WorkflowCatalog:
        """Return the cached catalog bundled with this RAGSpine release."""

        return load_builtin_catalog()

    def list(self) -> tuple[WorkflowTemplate, ...]:
        """Return templates in deterministic catalog order."""

        return tuple(_clone_template(template) for template in self._templates)

    def runnable(self) -> tuple[WorkflowTemplate, ...]:
        """Return only templates proven runnable by the release gate."""

        return tuple(
            _clone_template(template)
            for template in self._templates
            if template.compatibility.status == "runnable"
        )

    def _metadata_refs(self) -> tuple[WorkflowTemplate, ...]:
        """Internal metadata-only refs; never expose catalog workflow/YAML state."""

        return self._template_refs

    def _matching_refs(self) -> tuple[WorkflowTemplate, ...]:
        """Internal runnable refs for matchers; selection must resolve through ``get``."""

        return self._runnable_template_refs

    def get(self, template_id: str) -> WorkflowTemplate:
        """Look up an allowlisted template id; never interpret it as a path."""

        template = self._by_id.get(template_id)
        if template is None:
            raise WorkflowTemplateNotFoundError("workflow template 不存在")
        return _clone_template(template)


_BUILTIN_CATALOG_LOCK = Lock()
_builtin_catalog_cached: WorkflowCatalog | None = None
_builtin_catalog_flight: Future[WorkflowCatalog] | None = None


def load_builtin_catalog() -> WorkflowCatalog:
    """Load package resources once, coalescing concurrent cold callers."""

    global _builtin_catalog_cached, _builtin_catalog_flight

    with _BUILTIN_CATALOG_LOCK:
        if _builtin_catalog_cached is not None:
            return _builtin_catalog_cached
        flight = _builtin_catalog_flight
        leader = flight is None
        if flight is None:
            flight = Future()
            _builtin_catalog_flight = flight

    if not leader:
        return flight.result()

    try:
        root = files("ragspine.workflows.templates")
        loaded = _load_builtin_catalog(root)
    except BaseException as exc:
        with _BUILTIN_CATALOG_LOCK:
            if _builtin_catalog_flight is flight:
                _builtin_catalog_flight = None
        flight.set_exception(exc)
        raise

    with _BUILTIN_CATALOG_LOCK:
        if _builtin_catalog_cached is None:
            _builtin_catalog_cached = loaded
        resolved = _builtin_catalog_cached
        if _builtin_catalog_flight is flight:
            _builtin_catalog_flight = None
    flight.set_result(resolved)
    return resolved


def clear_builtin_catalog_cache() -> None:
    """Clear the built-in cache after any in-flight load; intended for tests."""

    global _builtin_catalog_cached

    while True:
        with _BUILTIN_CATALOG_LOCK:
            flight = _builtin_catalog_flight
            if flight is None:
                _builtin_catalog_cached = None
                return
        try:
            flight.result()
        except BaseException:
            # A failed flight is already evicted and a later caller may retry.
            pass


def load_catalog(directory: Path) -> WorkflowCatalog:
    """Load a catalog directory; intended for tests and release-time validation."""

    if directory.is_symlink():
        raise WorkflowCatalogError(f"catalog 根目录不得是符号链接: {directory}")
    if not directory.is_dir():
        raise WorkflowCatalogError(f"catalog 根目录不存在: {directory}")
    return _load_catalog(directory)


def _load_builtin_catalog(root: Traversable) -> WorkflowCatalog:
    """Combine curated YAML resources with an optional reviewed descriptor snapshot."""

    curated = _load_catalog(root)
    descriptor_resource = root.joinpath(GENERATED_CATALOG_RESOURCE)
    if not descriptor_resource.is_file():
        return curated

    from ragspine.workflows.generated_catalog import (
        MAX_DESCRIPTOR_CATALOG_BYTES,
        build_workflow_templates,
        load_workflow_descriptors,
    )

    curated_templates = curated.list()
    expected_generated = BUILTIN_CATALOG_TARGET_SIZE - len(curated_templates)
    if expected_generated < 0:
        raise WorkflowCatalogError(
            f"curated workflow template 数超过目标 {BUILTIN_CATALOG_TARGET_SIZE}"
        )
    raw_descriptors = _read_limited(
        descriptor_resource,
        MAX_DESCRIPTOR_CATALOG_BYTES,
        label=GENERATED_CATALOG_RESOURCE,
    )
    descriptors = load_workflow_descriptors(
        raw_descriptors,
        expected_count=expected_generated,
    )
    generated = build_workflow_templates(
        descriptors,
        expected_count=expected_generated,
    )
    combined = WorkflowCatalog((*curated_templates, *generated))
    if len(combined.list()) != BUILTIN_CATALOG_TARGET_SIZE:
        raise WorkflowCatalogError(
            f"built-in workflow catalog 必须是 {BUILTIN_CATALOG_TARGET_SIZE} 条"
        )
    return combined


def _load_catalog(root: Traversable) -> WorkflowCatalog:
    catalog_resource = root.joinpath("catalog.json")
    raw_catalog = _read_limited(catalog_resource, MAX_CATALOG_BYTES, label="catalog.json")
    try:
        data = parse_workflow(raw_catalog, format="json")
    except WorkflowFormatError as exc:
        raise WorkflowCatalogError("catalog.json 无效") from exc
    if _as_int(data.get("schema_version"), "schema_version") != CATALOG_SCHEMA_VERSION:
        raise WorkflowCatalogError(f"不支持 catalog schema_version: {data.get('schema_version')!r}")
    raw_templates = data.get("templates")
    if not isinstance(raw_templates, list):
        raise WorkflowCatalogError("catalog.templates 必须是 list")
    if not 1 <= len(raw_templates) <= MAX_TEMPLATES:
        raise WorkflowCatalogError(
            f"catalog template 数必须在 1..{MAX_TEMPLATES}，得到 {len(raw_templates)}"
        )

    templates = tuple(
        _load_template(root, _as_mapping(item, f"templates[{index}]"), index=index)
        for index, item in enumerate(raw_templates)
    )
    return WorkflowCatalog(templates)


def _load_template(root: Traversable, data: dict[str, Any], *, index: int) -> WorkflowTemplate:
    label = f"templates[{index}]"
    template_id = _as_str(data.get("id"), f"{label}.id")
    _reject_secrets(data, template_id=template_id)
    if _ID_RE.fullmatch(template_id) is None:
        raise WorkflowCatalogError("非法 template id")
    relative = _safe_resource_path(_as_str(data.get("yaml"), f"{label}.yaml"))
    resource = root.joinpath(*relative.parts)
    if isinstance(resource, Path) and resource.is_symlink():
        raise WorkflowCatalogError(f"template 不得是符号链接: {relative}")
    yaml_bytes = _read_limited(resource, MAX_TEMPLATE_BYTES, label=str(relative))
    expected_sha = _as_str(data.get("sha256"), f"{label}.sha256")
    if _SHA256_RE.fullmatch(expected_sha) is None:
        raise WorkflowCatalogError("非法 template sha256")
    actual_sha = hashlib.sha256(yaml_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise WorkflowCatalogError(
            f"template hash 漂移: expected={expected_sha} actual={actual_sha}"
        )
    try:
        yaml_text = yaml_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowCatalogError("template 不是 UTF-8") from exc
    compatibility_data = _as_mapping(data.get("compatibility"), f"{label}.compatibility")
    compatibility = WorkflowCompatibility(
        format=_as_str(compatibility_data.get("format"), "compatibility.format"),
        dsl_version=_as_str(compatibility_data.get("dsl_version"), "compatibility.dsl_version"),
        status=cast("Any", _as_str(compatibility_data.get("status"), "compatibility.status")),
    )
    if compatibility.format != "dify" or compatibility.dsl_version != "0.6.0":
        raise WorkflowCatalogError("template 必须是 Dify DSL 0.6.0")
    if compatibility.status not in {"runnable", "import_only", "blocked"}:
        raise WorkflowCatalogError("template compatibility.status 非法")
    try:
        workflow = parse_workflow(yaml_bytes, format="yaml")
    except WorkflowFormatError as exc:
        raise WorkflowCatalogError("template YAML 无效") from exc
    _validate_workflow(
        template_id,
        workflow,
        runnable=compatibility.status == "runnable",
    )
    if workflow.get("version") != "0.6.0":
        raise WorkflowCatalogError("template wire version 非 0.6.0")

    raw_requirements = data.get("requirements", [])
    if not isinstance(raw_requirements, list):
        raise WorkflowCatalogError(f"{label}.requirements 必须是 list")
    requirements = tuple(
        _parse_requirement(_as_mapping(item, f"{label}.requirements[{req_index}]"))
        for req_index, item in enumerate(raw_requirements)
    )
    raw_source = data.get("source")
    source = None if raw_source is None else _parse_source(_as_mapping(raw_source, "source"))

    return WorkflowTemplate(
        id=template_id,
        name=_as_str(data.get("name"), f"{label}.name"),
        description=_as_str(data.get("description"), f"{label}.description"),
        categories=_as_str_tuple(data.get("categories"), f"{label}.categories"),
        tags=_as_str_tuple(data.get("tags"), f"{label}.tags"),
        intents=_as_str_tuple(data.get("intents"), f"{label}.intents"),
        examples=_as_str_tuple(data.get("examples"), f"{label}.examples"),
        compatibility=compatibility,
        requirements=requirements,
        source=source,
        workflow=workflow,
        yaml=yaml_text,
        sha256=actual_sha,
    )


def _parse_requirement(data: dict[str, Any]) -> WorkflowRequirement:
    required = data.get("required", True)
    if not isinstance(required, bool):
        raise WorkflowCatalogError("requirement.required 必须是 bool")
    return WorkflowRequirement(
        kind=_as_str(data.get("kind"), "requirement.kind"),
        name=_as_str(data.get("name"), "requirement.name"),
        required=required,
    )


def _parse_source(data: dict[str, Any]) -> WorkflowSource:
    value = _as_int(data.get("observed_value"), "source.observed_value")
    source = WorkflowSource(
        provider=_as_str(data.get("provider"), "source.provider"),
        title=_as_str(data.get("title"), "source.title"),
        author=_as_str(data.get("author"), "source.author"),
        upstream_id=_as_str(data.get("upstream_id"), "source.upstream_id"),
        upstream_url=_as_str(data.get("upstream_url"), "source.upstream_url"),
        license_status=_as_str(data.get("license_status"), "source.license_status"),
        observed_metric=_as_str(data.get("observed_metric"), "source.observed_metric"),
        observed_value=value,
        observed_at=_as_str(data.get("observed_at"), "source.observed_at"),
    )
    validate_source_reference(source)
    return source


def _validate_workflow(
    template_id: str,
    document: dict[str, object],
    *,
    runnable: bool,
) -> None:
    """Validate parsed structure so YAML quoting/flow style cannot bypass gates."""

    if document.get("version") != "0.6.0":
        raise WorkflowCatalogError("template 不是 Dify DSL 0.6.0")
    _reject_secrets(document, template_id=template_id)
    if not runnable:
        return

    workflow = _as_mapping(document.get("workflow"), "workflow")
    environment = workflow.get("environment_variables", [])
    if environment != []:
        raise WorkflowCatalogError("runnable template 不得内置环境变量或凭据")
    if workflow.get("conversation_variables", []) != []:
        raise WorkflowCatalogError("runnable template 不得内置会话状态")
    if workflow.get("features", {}) != {}:
        raise WorkflowCatalogError("runnable template 不得预启用扩展能力")

    dependencies = document.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise WorkflowCatalogError("template dependencies 必须是 list")
    for dependency in dependencies:
        item = _as_mapping(dependency, "dependency")
        value = _as_mapping(item.get("value"), "dependency.value")
        identifier = _as_str(
            value.get("marketplace_plugin_unique_identifier"),
            "dependency.value.marketplace_plugin_unique_identifier",
        )
        if item.get("type") != "marketplace" or identifier not in _ALLOWED_PLUGIN_IDENTIFIERS:
            raise WorkflowCatalogError("runnable template 含未允许的插件依赖")

    graph = _as_mapping(workflow.get("graph"), "workflow.graph")
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        raise WorkflowCatalogError("workflow.graph.nodes 必须是 list")
    for raw_node in nodes:
        node = _as_mapping(raw_node, "workflow.graph.nodes[]")
        data = _as_mapping(node.get("data"), "workflow.graph.nodes[].data")
        node_type = _as_str(data.get("type"), "workflow.graph.nodes[].data.type")
        if node_type not in _ALLOWED_NODE_TYPES:
            raise WorkflowCatalogError("runnable template 含禁止节点")
        if node_type in {"llm", "parameter-extractor"}:
            model = _as_mapping(data.get("model"), "node.data.model")
            provider = _as_str(model.get("provider"), "node.data.model.provider")
            if provider not in _ALLOWED_MODEL_PROVIDERS:
                raise WorkflowCatalogError("runnable template 含未允许的模型插件")


def _reject_secrets(value: object, *, template_id: str) -> None:
    """Reject credential-shaped values without reflecting them into errors."""

    del template_id

    def visit(item: object) -> None:
        if isinstance(item, str):
            if "\x00" in item or any(pattern.search(item) for pattern in _SECRET_PATTERNS):
                raise WorkflowCatalogError("template 疑似含凭据或敏感值")
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return

        normalized: dict[str, object] = {}
        for raw_key, child in item.items():
            key = _normalize_key(str(raw_key))
            if key in normalized:
                raise WorkflowCatalogError("template 字段规范化后冲突，疑似含歧义配置")
            normalized[key] = child
            if key in _SENSITIVE_KEYS and not _is_placeholder(child):
                raise WorkflowCatalogError("template 疑似含凭据或敏感值")
            visit(child)

        label = normalized.get("name", normalized.get("variable"))
        if isinstance(label, str) and _normalize_key(label) in _SENSITIVE_KEYS:
            for value_key in ("value", "default", "default_value"):
                if value_key in normalized and not _is_placeholder(normalized[value_key]):
                    raise WorkflowCatalogError("template 疑似含凭据或敏感值")

    visit(value)


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _is_placeholder(value: object) -> bool:
    if value is None or value == "":
        return True
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized in _PLACEHOLDER_LITERALS or any(
        pattern.fullmatch(normalized) is not None for pattern in _PLACEHOLDER_PATTERNS
    )


def _safe_resource_path(value: str) -> PurePosixPath:
    if "\\" in value:
        raise WorkflowCatalogError(f"template 路径不得含反斜杠: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.suffix not in {".yml", ".yaml"}:
        raise WorkflowCatalogError(f"非法 template 路径: {value!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise WorkflowCatalogError(f"template 路径越界: {value!r}")
    if len(path.parts) != 1:
        raise WorkflowCatalogError(f"template 只允许 catalog 根目录内文件: {value!r}")
    return path


def _read_limited(resource: Traversable, limit: int, *, label: str) -> bytes:
    if not resource.is_file():
        raise WorkflowCatalogError(f"catalog resource 不存在或不是文件: {label}")
    if isinstance(resource, Path):
        return _read_path_limited(resource, limit, label=label)
    with resource.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise WorkflowCatalogError(f"catalog resource 过大: {label} > {limit} bytes")
    return data


def _read_path_limited(path: Path, limit: int, *, label: str) -> bytes:
    """Open one regular file without following links and verify file identity."""

    try:
        return read_bounded_file(path, limit=limit, label=label)
    except WorkflowFormatError as exc:
        raise WorkflowCatalogError(str(exc)) from exc


def _as_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowCatalogError(f"{label} 必须是 object")
    return {str(key): item for key, item in value.items()}


def _as_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowCatalogError(f"{label} 必须是非空字符串")
    return value.strip()


def _as_str_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise WorkflowCatalogError(f"{label} 必须是非空字符串 list")
    out = tuple(_as_str(item, f"{label}[]") for item in value)
    if len(out) != len(set(out)):
        raise WorkflowCatalogError(f"{label} 不得含重复值")
    return out


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkflowCatalogError(f"{label} 必须是 int")
    return value


def _clone_template(template: WorkflowTemplate) -> WorkflowTemplate:
    """Return a defensive copy so cached catalog state cannot be mutated."""

    return replace(template, workflow=deepcopy(template.workflow))


__all__ = [
    "WorkflowCatalog",
    "load_builtin_catalog",
    "clear_builtin_catalog_cache",
    "load_catalog",
    "BUILTIN_CATALOG_TARGET_SIZE",
    "CATALOG_SCHEMA_VERSION",
    "GENERATED_CATALOG_RESOURCE",
    "MAX_CATALOG_BYTES",
    "MAX_TEMPLATE_BYTES",
]
