"""Shared validation policy for attribution-only workflow source references."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from ragspine.workflows.errors import WorkflowCatalogError
from ragspine.workflows.model import WorkflowSource

_SOURCE_LICENSES = frozenset({"unknown-not-redistributed", "reference-only-not-redistributed"})
_SOURCE_POLICIES: dict[str, tuple[str, frozenset[str]]] = {
    "dify": (
        "marketplace.dify.ai",
        frozenset({"usage_count", "usage_count_rounded"}),
    ),
    "n8n": ("n8n.io", frozenset({"totalViews"})),
}


def validate_source_reference(source: WorkflowSource) -> None:
    """Enforce provider-bound URL identity, metric, license, and timestamp rules."""

    for value in (
        source.provider,
        source.title,
        source.author,
        source.upstream_id,
        source.upstream_url,
        source.license_status,
        source.observed_metric,
        source.observed_at,
    ):
        if not isinstance(value, str) or not value.strip():
            raise WorkflowCatalogError("source reference 字符串字段不得为空")
    if (
        isinstance(source.observed_value, bool)
        or not isinstance(source.observed_value, int)
        or source.observed_value < 0
    ):
        raise WorkflowCatalogError("source reference observed value 无效")

    parsed = urlparse(source.upstream_url)
    policy = _SOURCE_POLICIES.get(source.provider)
    try:
        port = parsed.port
    except ValueError:
        raise WorkflowCatalogError("source reference provider/host/metric 绑定无效") from None
    if (
        policy is None
        or parsed.scheme != "https"
        or parsed.hostname != policy[0]
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or source.observed_metric not in policy[1]
    ):
        raise WorkflowCatalogError("source reference provider/host/metric 绑定无效")
    if not _source_identity_matches(source, parsed.path, parsed.query, parsed.fragment):
        raise WorkflowCatalogError("source reference path/upstream id 绑定无效")
    if source.license_status not in _SOURCE_LICENSES:
        raise WorkflowCatalogError("source reference license status 无效")
    if not _is_timezone_aware_iso8601(source.observed_at):
        raise WorkflowCatalogError("source reference observed time 无效")


def _source_identity_matches(
    source: WorkflowSource,
    path: str,
    query: str,
    fragment: str,
) -> bool:
    if fragment:
        return False
    if source.provider == "n8n":
        if re.fullmatch(r"[0-9]+", source.upstream_id) is None or query:
            return False
        expected_path = re.compile(rf"^/workflows/{re.escape(source.upstream_id)}(?:-[^/]+)?/$")
        return expected_path.fullmatch(path) is not None
    if source.provider != "dify":
        return False
    try:
        canonical_id = str(UUID(source.upstream_id))
    except ValueError:
        return False
    if source.upstream_id != canonical_id:
        return False
    if not path.startswith("/template/") or len(path) <= len("/template/"):
        return False
    template_ids = parse_qs(query, keep_blank_values=True).get("templateId")
    return template_ids == [source.upstream_id]


def _is_timezone_aware_iso8601(value: str) -> bool:
    if "T" not in value:
        return False
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


__all__ = ["validate_source_reference"]
