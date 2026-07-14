"""Integrity, attribution, and security contracts for the bundled catalog."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import pytest

from ragspine.workflows.catalog import load_builtin_catalog, load_catalog
from ragspine.workflows.errors import (
    WorkflowCatalogError,
    WorkflowTemplateNotFoundError,
)


def _copy_catalog(tmp_path: Path) -> Path:
    source = files("ragspine.workflows.templates")
    destination = tmp_path / "catalog"
    destination.mkdir()
    for resource in source.iterdir():
        if resource.is_file() and resource.name != "__init__.py":
            destination.joinpath(resource.name).write_bytes(resource.read_bytes())
    return destination


def _read_manifest(directory: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(directory.joinpath("catalog.json").read_text(encoding="utf-8")),
    )


def _write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    directory.joinpath("catalog.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _replace_first_template(directory: Path, yaml_text: str) -> str:
    manifest = _read_manifest(directory)
    entry = cast(dict[str, Any], manifest["templates"][0])
    filename = cast(str, entry["yaml"])
    directory.joinpath(filename).write_text(yaml_text, encoding="utf-8")
    entry["sha256"] = hashlib.sha256(yaml_text.encode()).hexdigest()
    _write_manifest(directory, manifest)
    return filename


def test_builtin_catalog_contains_seven_integrity_checked_templates() -> None:
    catalog = load_builtin_catalog()
    templates = catalog.list()

    assert len(templates) == 7
    assert len({template.id for template in templates}) == 7
    assert len(catalog.runnable()) == 7
    for template in templates:
        assert hashlib.sha256(template.yaml.encode()).hexdigest() == template.sha256
        assert template.compatibility.format == "dify"
        assert template.compatibility.dsl_version == "0.6.0"
        assert template.compatibility.status == "runnable"


def test_catalog_attribution_is_reference_only_and_uses_canonical_urls() -> None:
    templates = load_builtin_catalog().list()
    sources = [template.source for template in templates]

    assert all(source is not None for source in sources)
    assert {source.provider for source in sources if source is not None} == {"dify", "n8n"}
    assert all(
        source.license_status == "unknown-not-redistributed"
        for source in sources
        if source is not None
    )
    for template_id in ("batch-content-processing", "parallel-perspective-analysis"):
        source = load_builtin_catalog().get(template_id).source
        assert source is not None
        assert source.upstream_url.startswith("https://marketplace.dify.ai/template/")
        assert "/templates/" not in source.upstream_url


def test_catalog_returns_defensive_workflow_copies() -> None:
    catalog = load_builtin_catalog()
    first = catalog.list()[0]
    app = cast(dict[str, object], first.workflow["app"])
    original_name = app["name"]
    app["name"] = "poisoned"

    fresh = catalog.get(first.id)

    assert cast(dict[str, object], fresh.workflow["app"])["name"] == original_name


def test_unknown_template_id_uses_domain_error() -> None:
    with pytest.raises(WorkflowTemplateNotFoundError) as error:
        load_builtin_catalog().get("missing-template")

    assert error.value.code == "workflow.template_not_found"


def test_template_hash_drift_is_rejected(tmp_path: Path) -> None:
    directory = _copy_catalog(tmp_path)
    manifest = _read_manifest(directory)
    entry = cast(dict[str, Any], manifest["templates"][0])
    path = directory / cast(str, entry["yaml"])
    path.write_text(path.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    with pytest.raises(WorkflowCatalogError, match="hash"):
        load_catalog(directory)


@pytest.mark.parametrize("unsafe_path", ["../outside.yml", "nested/flow.yml", "..\\flow.yml"])
def test_template_paths_cannot_escape_catalog(tmp_path: Path, unsafe_path: str) -> None:
    directory = _copy_catalog(tmp_path)
    manifest = _read_manifest(directory)
    cast(dict[str, Any], manifest["templates"][0])["yaml"] = unsafe_path
    _write_manifest(directory, manifest)

    with pytest.raises(WorkflowCatalogError, match="路径|根目录"):
        load_catalog(directory)


def test_template_symlink_is_rejected_even_when_content_hash_matches(tmp_path: Path) -> None:
    directory = _copy_catalog(tmp_path)
    manifest = _read_manifest(directory)
    entry = cast(dict[str, Any], manifest["templates"][0])
    path = directory / cast(str, entry["yaml"])
    external = tmp_path / "external.yml"
    external.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(external)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(WorkflowCatalogError, match="链接"):
        load_catalog(directory)


@pytest.mark.parametrize(
    "node_type",
    [
        '"code"',
        "http-request",
        "tool",
        "trigger-webhook",
        "trigger-plugin",
        "agent",
        "future-unknown-node",
    ],
)
def test_runnable_catalog_rejects_forbidden_nodes_in_any_yaml_style(
    tmp_path: Path, node_type: str
) -> None:
    directory = _copy_catalog(tmp_path)
    yaml_text = f"""app:
  mode: workflow
  name: unsafe
kind: app
version: "0.6.0"
workflow:
  graph:
    nodes:
      - id: unsafe
        data: {{type: {node_type}, title: Unsafe}}
    edges: []
"""
    _replace_first_template(directory, yaml_text)

    with pytest.raises(WorkflowCatalogError, match="禁止节点"):
        load_catalog(directory)


@pytest.mark.parametrize(
    "secret_field",
    [
        "api_key: sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "authorization: 'Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature'",
        "password: correct-horse-battery-staple",
        "credential: sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_catalog_rejects_structurally_nested_secrets_without_echoing_them(
    tmp_path: Path, secret_field: str
) -> None:
    directory = _copy_catalog(tmp_path)
    secret_value = secret_field.split(":", maxsplit=1)[1].strip(" '\"")
    yaml_text = f"""app:
  mode: workflow
  name: unsafe
kind: app
version: "0.6.0"
workflow:
  private:
    {secret_field}
  graph:
    nodes:
      - id: start
        data: {{type: start, title: Start, variables: []}}
    edges: []
"""
    _replace_first_template(directory, yaml_text)

    with pytest.raises(WorkflowCatalogError, match="凭据|敏感") as error:
        load_catalog(directory)

    assert secret_value not in str(error.value)


@pytest.mark.parametrize(
    "private_fields",
    [
        "api_key: correct-horse-battery-staple\n    api-key: ''",
        "safe_field:\n      password: correct-horse-battery-staple\n    safe-field: {}",
    ],
)
def test_catalog_rejects_normalized_key_collisions_that_hide_secrets(
    tmp_path: Path, private_fields: str
) -> None:
    directory = _copy_catalog(tmp_path)
    manifest = _read_manifest(directory)
    entry = cast(dict[str, Any], manifest["templates"][0])
    path = directory / cast(str, entry["yaml"])
    yaml_text = path.read_text(encoding="utf-8").replace(
        "workflow:\n",
        f"workflow:\n  private:\n    {private_fields}\n",
        1,
    )
    _replace_first_template(directory, yaml_text)

    with pytest.raises(WorkflowCatalogError, match="凭据|敏感|冲突"):
        load_catalog(directory)


def test_catalog_root_symlink_is_rejected(tmp_path: Path) -> None:
    directory = _copy_catalog(tmp_path)
    link = tmp_path / "catalog-link"
    try:
        link.symlink_to(directory, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(WorkflowCatalogError, match="符号链接"):
        load_catalog(link)


def test_runnable_catalog_rejects_nonempty_environment_variables(
    tmp_path: Path,
) -> None:
    directory = _copy_catalog(tmp_path)
    manifest = _read_manifest(directory)
    entry = cast(dict[str, Any], manifest["templates"][0])
    path = directory / cast(str, entry["yaml"])
    yaml_text = path.read_text(encoding="utf-8").replace(
        "  environment_variables: []",
        """  environment_variables:
    - id: catalog-test
      name: API_TOKEN
      value: ""
      value_type: secret""",
        1,
    )
    _replace_first_template(directory, yaml_text)

    with pytest.raises(WorkflowCatalogError, match="environment_variables|环境变量|敏感|凭据"):
        load_catalog(directory)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "langgenius/openai:0.3.8@"
            "592c8252795b5f75807de2d609a03196ed02596b409f7642b4a07548c7ff57ef",
            "untrusted/plugin:9.9.9@"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
        ("provider: langgenius/openai/openai", "provider: untrusted/plugin/provider"),
    ],
)
def test_runnable_catalog_rejects_unknown_marketplace_dependencies_and_providers(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    directory = _copy_catalog(tmp_path)
    manifest = _read_manifest(directory)
    entry = cast(dict[str, Any], manifest["templates"][0])
    path = directory / cast(str, entry["yaml"])
    yaml_text = path.read_text(encoding="utf-8").replace(old, new, 1)
    assert yaml_text != path.read_text(encoding="utf-8")
    _replace_first_template(directory, yaml_text)

    with pytest.raises(WorkflowCatalogError, match="marketplace|provider|依赖|提供商|模型插件"):
        load_catalog(directory)


def test_catalog_json_duplicate_key_is_rejected_without_echoing_value(
    tmp_path: Path,
) -> None:
    directory = _copy_catalog(tmp_path)
    secret_value = "sk-proj-catalog-duplicate-value-must-not-leak"
    path = directory / "catalog.json"
    raw = path.read_text(encoding="utf-8").replace(
        '"schema_version": 1,',
        f'"schema_version": 1,\n  "schema_version": "{secret_value}",',
        1,
    )
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(WorkflowCatalogError) as error:
        load_catalog(directory)

    assert secret_value not in str(error.value)


def test_catalog_json_nan_is_rejected_without_echoing_unrelated_values(
    tmp_path: Path,
) -> None:
    directory = _copy_catalog(tmp_path)
    secret_value = "sk-proj-catalog-nan-value-must-not-leak"
    path = directory / "catalog.json"
    raw = path.read_text(encoding="utf-8").replace(
        '"schema_version": 1,',
        f'"schema_version": NaN,\n  "diagnostic_sentinel": "{secret_value}",',
        1,
    )
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(WorkflowCatalogError) as error:
        load_catalog(directory)

    assert secret_value not in str(error.value)
