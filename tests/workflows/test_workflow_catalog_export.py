"""Release-time export of the validated workflow catalog to the static website."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from types import ModuleType

import pytest

from ragspine.workflows.catalog import load_builtin_catalog
from ragspine.workflows.preview import build_workflow_preview

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPORTER_PATH = PROJECT_ROOT / "scripts" / "export_workflow_catalog.py"


def _load_exporter() -> ModuleType:
    name = "_ragspine_workflow_catalog_exporter"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, EXPORTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def exporter() -> ModuleType:
    return _load_exporter()


@pytest.fixture(scope="module")
def exported_web_root(
    tmp_path_factory: pytest.TempPathFactory,
    exporter: ModuleType,
) -> Path:
    web_root = tmp_path_factory.mktemp("workflow-web-export")
    result = exporter.export_workflow_catalog(web_root)
    assert result.template_count == 1000
    return web_root


def _copy_export(exported_web_root: Path, tmp_path: Path) -> Path:
    web_root = tmp_path / "web"
    # Contents, not source metadata, are the contract. Avoid copy2's chmod/chflags
    # round-trips for 1,000 generated files; they dominate the full local gate on APFS.
    shutil.copytree(exported_web_root, web_root, copy_function=shutil.copyfile)
    return web_root


def test_exports_1000_metadata_records_and_integrity_checked_yaml(
    exporter: ModuleType,
    exported_web_root: Path,
) -> None:
    catalog = load_builtin_catalog().list()
    snapshot_path = exported_web_root / "src" / "data" / "workflow-catalog.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    workflow_root = exported_web_root / "public" / "workflows"
    yaml_paths = sorted(workflow_root.glob("*.yml"))

    assert snapshot["schema_version"] == 1
    assert len(snapshot["templates"]) == len(catalog) == 1000
    assert len(yaml_paths) == 1000
    assert {path.name for path in yaml_paths} == {f"{template.id}.yml" for template in catalog}

    by_id = {item["id"]: item for item in snapshot["templates"]}
    for template in catalog:
        item = by_id[template.id]
        yaml_path = workflow_root / item["yaml"]
        payload = yaml_path.read_bytes()

        assert item["yaml"] == f"{template.id}.yml"
        assert hashlib.sha256(payload).hexdigest() == item["sha256"] == template.sha256
        assert payload == template.yaml.encode("utf-8")
        assert item["compatibility"] == asdict(template.compatibility)
        assert item["requirements"] == [
            asdict(requirement) for requirement in template.requirements
        ]
        assert item["source"] == (None if template.source is None else asdict(template.source))
        assert item["preview"] == build_workflow_preview(template.workflow).to_dict()
        assert set(item["preview"]) == {"preview_schema_version", "nodes", "edges"}
        assert item["preview"]["preview_schema_version"] == 1
        assert item["preview"]["nodes"]
        node_ids = {node["id"] for node in item["preview"]["nodes"]}
        assert len(node_ids) == len(item["preview"]["nodes"])
        assert all(
            set(node) <= {"id", "title", "type", "x", "y", "width", "height", "parent_id"}
            for node in item["preview"]["nodes"]
        )
        assert all(
            set(edge) <= {"id", "source", "target", "label"}
            and edge["source"] in node_ids
            and edge["target"] in node_ids
            for edge in item["preview"]["edges"]
        )

    legacy = by_id["rag-paper-qa"]
    assert legacy["industries"] == ["Cross-industry"]
    assert legacy["use_cases"] == ["Research & knowledge"]
    assert legacy["categories"] == ["rag", "research"]
    assert len(legacy["preview"]["nodes"]) == 4
    assert len(legacy["preview"]["edges"]) == 3

    branch = by_id["conditional-response-routing"]
    assert {edge.get("label") for edge in branch["preview"]["edges"]} >= {"IF", "ELSE"}

    iteration = by_id["batch-content-processing"]
    assert {node.get("parent_id") for node in iteration["preview"]["nodes"]} >= {"iteration_1"}

    generated_template = catalog[7]
    generated = by_id[generated_template.id]
    assert generated["industries"] == [
        exporter._display_label(category.removeprefix("industry:"))
        for category in generated_template.categories
        if category.startswith("industry:")
    ]
    assert generated["use_cases"] == [
        exporter._display_label(category.removeprefix("use-case:"))
        for category in generated_template.categories
        if category.startswith("use-case:")
    ]
    assert generated["categories"] == [
        category
        for category in generated_template.categories
        if not category.startswith(("industry:", "use-case:"))
    ]


@pytest.mark.parametrize(
    ("slug", "label"),
    (
        ("cross-industry", "Cross-industry"),
        ("real-estate", "Real estate"),
        ("knowledge-retrieval", "Knowledge retrieval"),
        ("ecommerce", "E-commerce"),
        ("customer-support", "Customer support"),
    ),
)
def test_generated_taxonomy_slugs_have_stable_display_labels(
    exporter: ModuleType,
    slug: str,
    label: str,
) -> None:
    assert exporter._display_label(slug) == label


def test_check_detects_drift_without_writing_and_export_repairs_it(
    exporter: ModuleType,
    exported_web_root: Path,
    tmp_path: Path,
) -> None:
    web_root = _copy_export(exported_web_root, tmp_path)
    yaml_path = next((web_root / "public" / "workflows").glob("*.yml"))
    yaml_path.write_text("drift\n", encoding="utf-8")

    with pytest.raises(exporter.CatalogDriftError, match=yaml_path.name):
        exporter.export_workflow_catalog(web_root, check=True)
    assert yaml_path.read_text(encoding="utf-8") == "drift\n"

    result = exporter.export_workflow_catalog(web_root)
    assert result.template_count == 1000
    exporter.export_workflow_catalog(web_root, check=True)

    snapshot_path = web_root / "src" / "data" / "workflow-catalog.json"
    snapshot_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(exporter.CatalogDriftError, match="workflow-catalog.json"):
        exporter.export_workflow_catalog(web_root, check=True)
    assert snapshot_path.read_text(encoding="utf-8") == "{}\n"

    absent_web_root = tmp_path / "absent-web"
    with pytest.raises(exporter.CatalogDriftError):
        exporter.export_workflow_catalog(absent_web_root, check=True)
    assert not absent_web_root.exists()


def test_unknown_yaml_is_rejected_before_any_export_write(
    exporter: ModuleType,
    exported_web_root: Path,
    tmp_path: Path,
) -> None:
    web_root = _copy_export(exported_web_root, tmp_path)
    snapshot_path = web_root / "src" / "data" / "workflow-catalog.json"
    snapshot_before = snapshot_path.read_bytes()
    unknown = web_root / "public" / "workflows" / "operator-notes.yml"
    unknown.write_text("do not delete me\n", encoding="utf-8")

    with pytest.raises(exporter.CatalogExportError, match="operator-notes.yml"):
        exporter.export_workflow_catalog(web_root)

    assert unknown.read_text(encoding="utf-8") == "do not delete me\n"
    assert snapshot_path.read_bytes() == snapshot_before


def test_only_obsolete_yaml_managed_by_the_previous_snapshot_is_deleted(
    exporter: ModuleType,
    exported_web_root: Path,
    tmp_path: Path,
) -> None:
    web_root = _copy_export(exported_web_root, tmp_path)
    snapshot_path = web_root / "src" / "data" / "workflow-catalog.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["templates"].append({"id": "retired", "yaml": "retired.yml"})
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    retired = web_root / "public" / "workflows" / "retired.yml"
    retired.write_text("retired\n", encoding="utf-8")

    result = exporter.export_workflow_catalog(web_root)

    assert not retired.exists()
    assert result.removed_paths == (retired,)
    exporter.export_workflow_catalog(web_root, check=True)


def test_each_file_write_uses_same_directory_replace_and_preserves_old_file_on_failure(
    exporter: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "catalog.json"
    destination.write_bytes(b"old")
    seen_temporary: list[Path] = []

    def fail_replace(source: str | Path, target: str | Path) -> None:
        temporary = Path(source)
        seen_temporary.append(temporary)
        assert temporary.parent == destination.parent
        assert Path(target) == destination
        assert temporary.read_bytes() == b"new"
        raise OSError("simulated replace failure")

    monkeypatch.setattr(exporter.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        exporter._atomic_write(destination, b"new")

    assert destination.read_bytes() == b"old"
    assert len(seen_temporary) == 1
    assert not seen_temporary[0].exists()


def test_preview_error_is_wrapped_without_echoing_private_values(
    exporter: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_value = "SK" + "0123456789abcdef" * 2
    template = load_builtin_catalog().list()[0]

    def reject_preview(workflow: object) -> object:
        del workflow
        raise exporter.WorkflowPreviewError(f"invalid: {private_value}")

    monkeypatch.setattr(exporter, "build_workflow_preview", reject_preview)

    with pytest.raises(exporter.CatalogExportError, match="workflow preview 无效") as exc_info:
        exporter._metadata_row(template, filename=f"{template.id}.yml")

    assert private_value not in str(exc_info.value)
