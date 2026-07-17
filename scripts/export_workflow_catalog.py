#!/usr/bin/env python3
"""Export RAGSpine's validated workflow catalog to the static website.

The package catalog is the only content source.  The previous website snapshot
is read solely as an ownership manifest, so obsolete managed YAML can be
removed without ever deleting an unknown operator-owned file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ragspine.workflows.catalog import (
    BUILTIN_CATALOG_TARGET_SIZE,
    load_builtin_catalog,
)
from ragspine.workflows.model import WorkflowTemplate
from ragspine.workflows.preview import WorkflowPreviewError, build_workflow_preview

SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_RELATIVE_PATH = Path("src") / "data" / "workflow-catalog.json"
WORKFLOWS_RELATIVE_PATH = Path("public") / "workflows"
_YAML_FILENAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.ya?ml$")
_INDUSTRY_PREFIX = "industry:"
_USE_CASE_PREFIX = "use-case:"
_DISPLAY_LABEL_OVERRIDES = {
    "cross-industry": "Cross-industry",
    "ecommerce": "E-commerce",
}

# These seven values are the human-edited website labels that shipped before
# generated descriptors were added.  Freezing them here makes --check detect
# accidental website drift instead of accepting drift as its own source.
_LEGACY_CLASSIFICATION: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "rag-paper-qa": (("Cross-industry",), ("Research & knowledge",)),
    "executive-summary": (("Cross-industry",), ("Summarization",)),
    "multilingual-translation": (
        ("Cross-industry",),
        ("Translation & localization",),
    ),
    "batch-content-processing": (("Cross-industry",), ("Content operations",)),
    "parallel-perspective-analysis": (
        ("Cross-industry",),
        ("Research & analysis",),
    ),
    "structured-information-extraction": (
        ("Cross-industry",),
        ("Data extraction",),
    ),
    "conditional-response-routing": (
        ("Cross-industry",),
        ("Request routing",),
    ),
}


class CatalogExportError(RuntimeError):
    """The website export cannot proceed without risking an invalid tree."""


class CatalogDriftError(CatalogExportError):
    """The checked website export differs from the package catalog."""


@dataclass(frozen=True)
class ExportResult:
    """Summary of one successful export or clean check."""

    template_count: int
    written_paths: tuple[Path, ...]
    removed_paths: tuple[Path, ...]
    checked: bool


@dataclass(frozen=True)
class _ExportPlan:
    snapshot: bytes
    yaml_by_name: dict[str, bytes]


def export_workflow_catalog(web_root: Path, *, check: bool = False) -> ExportResult:
    """Export all validated built-ins under one website application root.

    Args:
        web_root: Root containing ``src/data`` and ``public/workflows``.
        check: Compare the complete export without changing the filesystem.

    Returns:
        Counts and paths changed by the operation.

    Raises:
        CatalogExportError: If paths, old ownership metadata, or catalog data are unsafe.
        CatalogDriftError: If ``check`` finds missing, stale, or obsolete managed files.
    """

    web_root = Path(web_root)
    _validate_web_root(web_root)
    plan = _build_export_plan()
    snapshot_path = web_root / SNAPSHOT_RELATIVE_PATH
    workflows_root = web_root / WORKFLOWS_RELATIVE_PATH

    previous_managed, previous_snapshot_invalid = _read_previous_managed_yaml(
        snapshot_path,
        tolerate_invalid=check,
    )
    existing_yaml = _scan_existing_yaml(workflows_root)
    desired_names = set(plan.yaml_by_name)
    unknown = existing_yaml - previous_managed - desired_names
    if unknown:
        names = ", ".join(sorted(unknown))
        raise CatalogExportError(f"拒绝未知 workflow YAML: {names}")

    obsolete = tuple(
        workflows_root / name for name in sorted((previous_managed - desired_names) & existing_yaml)
    )
    desired_paths = {workflows_root / name: payload for name, payload in plan.yaml_by_name.items()}

    if check:
        drifted: list[Path] = []
        if previous_snapshot_invalid or not _matches(snapshot_path, plan.snapshot):
            drifted.append(snapshot_path)
        drifted.extend(
            path for path, payload in desired_paths.items() if not _matches(path, payload)
        )
        drifted.extend(obsolete)
        if drifted:
            raise CatalogDriftError(_format_drift(web_root, drifted))
        return ExportResult(
            template_count=len(plan.yaml_by_name),
            written_paths=(),
            removed_paths=(),
            checked=True,
        )

    written: list[Path] = []
    for path, payload in desired_paths.items():
        if not _matches(path, payload):
            _atomic_write(path, payload)
            written.append(path)

    # Remove only files explicitly named by the previous snapshot.  The new
    # snapshot is replaced last so a failed run remains safely retryable.
    for path in obsolete:
        path.unlink()

    if not _matches(snapshot_path, plan.snapshot):
        _atomic_write(snapshot_path, plan.snapshot)
        written.append(snapshot_path)

    return ExportResult(
        template_count=len(plan.yaml_by_name),
        written_paths=tuple(written),
        removed_paths=obsolete,
        checked=False,
    )


def _build_export_plan() -> _ExportPlan:
    templates = load_builtin_catalog().list()
    if len(templates) != BUILTIN_CATALOG_TARGET_SIZE:
        raise CatalogExportError(
            "built-in workflow catalog 数量错误: "
            f"expected={BUILTIN_CATALOG_TARGET_SIZE} actual={len(templates)}"
        )

    present_legacy_ids = {template.id for template in templates} & set(_LEGACY_CLASSIFICATION)
    if present_legacy_ids != set(_LEGACY_CLASSIFICATION):
        missing = ", ".join(sorted(set(_LEGACY_CLASSIFICATION) - present_legacy_ids))
        raise CatalogExportError(f"legacy workflow 缺失: {missing}")

    rows: list[dict[str, object]] = []
    yaml_by_name: dict[str, bytes] = {}
    for template in templates:
        filename = f"{template.id}.yml"
        if _YAML_FILENAME_RE.fullmatch(filename) is None:
            raise CatalogExportError(f"非法 workflow 文件名: {filename!r}")
        if filename in yaml_by_name:
            raise CatalogExportError(f"重复 workflow 文件名: {filename}")

        payload = template.yaml.encode("utf-8")
        actual_sha = hashlib.sha256(payload).hexdigest()
        if actual_sha != template.sha256:
            raise CatalogExportError(
                f"workflow hash 漂移: {template.id} expected={template.sha256} actual={actual_sha}"
            )
        yaml_by_name[filename] = payload
        rows.append(_metadata_row(template, filename=filename))

    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "templates": rows,
    }
    payload = (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode(
        "utf-8"
    )
    return _ExportPlan(snapshot=payload, yaml_by_name=yaml_by_name)


def _metadata_row(template: WorkflowTemplate, *, filename: str) -> dict[str, object]:
    legacy = _LEGACY_CLASSIFICATION.get(template.id)
    if legacy is None:
        industries, use_cases, categories = _split_generated_categories(
            template.categories,
            template_id=template.id,
        )
    else:
        industries, use_cases = legacy
        categories = template.categories

    try:
        preview = build_workflow_preview(template.workflow).to_dict()
    except WorkflowPreviewError as exc:
        raise CatalogExportError(f"workflow preview 无效: {template.id}") from exc

    return {
        "id": template.id,
        "name": template.name,
        "description": template.description,
        "industries": list(industries),
        "use_cases": list(use_cases),
        "categories": list(categories),
        "tags": list(template.tags),
        "intents": list(template.intents),
        "examples": list(template.examples),
        "compatibility": asdict(template.compatibility),
        "requirements": [asdict(requirement) for requirement in template.requirements],
        "source": None if template.source is None else asdict(template.source),
        "preview": preview,
        "yaml": filename,
        "sha256": template.sha256,
    }


def _split_generated_categories(
    categories: tuple[str, ...],
    *,
    template_id: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    industries = tuple(
        _display_label(value[len(_INDUSTRY_PREFIX) :])
        for value in categories
        if value.startswith(_INDUSTRY_PREFIX)
    )
    use_cases = tuple(
        _display_label(value[len(_USE_CASE_PREFIX) :])
        for value in categories
        if value.startswith(_USE_CASE_PREFIX)
    )
    display_categories = tuple(
        value for value in categories if not value.startswith((_INDUSTRY_PREFIX, _USE_CASE_PREFIX))
    )
    if len(industries) != 1 or len(use_cases) != 1 or not industries[0] or not use_cases[0]:
        raise CatalogExportError(
            f"generated workflow 分类必须各含一个 industry/use-case: {template_id}"
        )
    return industries, use_cases, display_categories


def _display_label(slug: str) -> str:
    """Turn one stable taxonomy slug into its user-facing filter label."""

    override = _DISPLAY_LABEL_OVERRIDES.get(slug)
    if override is not None:
        return override
    return slug.replace("-", " ").capitalize()


def _read_previous_managed_yaml(
    snapshot_path: Path,
    *,
    tolerate_invalid: bool,
) -> tuple[set[str], bool]:
    if snapshot_path.is_symlink():
        raise CatalogExportError(f"snapshot 不得是符号链接: {snapshot_path}")
    if not snapshot_path.exists():
        return set(), False
    if not snapshot_path.is_file():
        raise CatalogExportError(f"snapshot 不是普通文件: {snapshot_path}")

    try:
        document = json.loads(snapshot_path.read_text(encoding="utf-8"))
        managed = _managed_yaml_from_document(document)
    except (CatalogExportError, OSError, UnicodeError, json.JSONDecodeError):
        if tolerate_invalid:
            return set(), True
        raise CatalogExportError(f"旧 workflow snapshot 无效: {snapshot_path}") from None
    return managed, False


def _managed_yaml_from_document(document: Any) -> set[str]:
    if not isinstance(document, dict):
        raise CatalogExportError("旧 workflow snapshot 顶层必须是 object")
    if document.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise CatalogExportError("旧 workflow snapshot schema_version 非法")
    templates = document.get("templates")
    if not isinstance(templates, list):
        raise CatalogExportError("旧 workflow snapshot.templates 必须是 list")

    managed: set[str] = set()
    for index, value in enumerate(templates):
        if not isinstance(value, dict):
            raise CatalogExportError(f"旧 workflow snapshot.templates[{index}] 非法")
        filename = value.get("yaml")
        if not isinstance(filename, str) or _YAML_FILENAME_RE.fullmatch(filename) is None:
            raise CatalogExportError(f"旧 workflow snapshot YAML 文件名非法: index={index}")
        if filename in managed:
            raise CatalogExportError(f"旧 workflow snapshot YAML 文件名重复: {filename}")
        managed.add(filename)
    return managed


def _scan_existing_yaml(workflows_root: Path) -> set[str]:
    if workflows_root.is_symlink():
        raise CatalogExportError(f"workflow 目录不得是符号链接: {workflows_root}")
    if not workflows_root.exists():
        return set()
    if not workflows_root.is_dir():
        raise CatalogExportError(f"workflow 路径不是目录: {workflows_root}")

    names: set[str] = set()
    for entry in workflows_root.iterdir():
        if entry.suffix.lower() not in {".yml", ".yaml"}:
            continue
        if entry.is_symlink() or not entry.is_file():
            raise CatalogExportError(f"workflow YAML 必须是普通文件: {entry.name}")
        names.add(entry.name)
    return names


def _validate_web_root(web_root: Path) -> None:
    if web_root.is_symlink():
        raise CatalogExportError(f"web root 不得是符号链接: {web_root}")
    if web_root.exists() and not web_root.is_dir():
        raise CatalogExportError(f"web root 不是目录: {web_root}")

    for relative in (
        Path("src"),
        Path("src") / "data",
        Path("public"),
        WORKFLOWS_RELATIVE_PATH,
    ):
        candidate = web_root / relative
        if candidate.is_symlink():
            raise CatalogExportError(f"导出目录不得是符号链接: {candidate}")
        if candidate.exists() and not candidate.is_dir():
            raise CatalogExportError(f"导出路径不是目录: {candidate}")


def _matches(path: Path, payload: bytes) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return path.read_bytes() == payload
    except OSError:
        return False


def _format_drift(web_root: Path, paths: Sequence[Path]) -> str:
    unique = tuple(dict.fromkeys(paths))
    labels: list[str] = []
    for path in unique[:10]:
        try:
            labels.append(str(path.relative_to(web_root)))
        except ValueError:
            labels.append(str(path))
    suffix = "" if len(unique) <= 10 else f" … 另有 {len(unique) - 10} 个"
    return f"workflow website export 有漂移: {', '.join(labels)}{suffix}"


def _atomic_write(destination: Path, payload: bytes) -> None:
    """Replace one file from a flushed sibling temporary file."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export the validated RAGSpine workflow catalog to the static website.",
    )
    parser.add_argument(
        "--web-root",
        required=True,
        type=Path,
        help="website app root containing src/data and public/workflows",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the complete export without writing or deleting files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the cross-platform command-line exporter."""

    args = _parser().parse_args(argv)
    try:
        result = export_workflow_catalog(args.web_root, check=args.check)
    except CatalogExportError as exc:
        print(f"workflow catalog export failed: {exc}", file=sys.stderr)
        return 1

    if result.checked:
        print(f"workflow catalog export is current ({result.template_count} templates)")
    else:
        print(
            "workflow catalog exported "
            f"({result.template_count} templates, "
            f"{len(result.written_paths)} files written, "
            f"{len(result.removed_paths)} removed)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
