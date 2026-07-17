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


def test_curated_catalog_contains_seven_integrity_checked_templates(tmp_path: Path) -> None:
    catalog = load_catalog(_copy_catalog(tmp_path))
    templates = catalog.list()

    assert len(templates) == 7
    assert len({template.id for template in templates}) == 7
    assert len(catalog.runnable()) == 7
    for template in templates:
        assert hashlib.sha256(template.yaml.encode()).hexdigest() == template.sha256
        assert template.compatibility.format == "dify"
        assert template.compatibility.dsl_version == "0.6.0"
        assert template.compatibility.status == "runnable"


@pytest.mark.parametrize(
    "source_updates",
    [
        {"provider": "evil-provider"},
        {
            "upstream_url": (
                "https://n8n.io.evil.example/workflows/"
                "2165-chat-with-pdf-docs-using-ai-quoting-sources/"
            )
        },
        {
            "upstream_url": (
                "https://n8n.io:443/workflows/2165-chat-with-pdf-docs-using-ai-quoting-sources/"
            )
        },
        {"upstream_url": "https://n8n.io/workflows/9999-wrong-identity/"},
        {"upstream_id": "not-numeric"},
        {"observed_at": "2026-07-14T17:24:00"},
        {"license_status": "copied-without-review"},
        {"observed_metric": "usage_count"},
    ],
    ids=(
        "provider",
        "evil-host",
        "explicit-port",
        "bad-path",
        "bad-upstream-id",
        "naive-date",
        "bad-license",
        "unbound-metric",
    ),
)
def test_curated_catalog_rejects_source_outside_shared_reference_policy(
    tmp_path: Path, source_updates: dict[str, object]
) -> None:
    directory = _copy_catalog(tmp_path)
    manifest = _read_manifest(directory)
    entry = cast(dict[str, Any], manifest["templates"][0])
    source = cast(dict[str, Any], entry["source"])
    source.update(source_updates)
    _write_manifest(directory, manifest)

    with pytest.raises(WorkflowCatalogError, match="source reference"):
        load_catalog(directory)


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


def test_unknown_secret_shaped_template_id_is_not_retained_or_reflected() -> None:
    secret_id = "SG." + "A" * 22 + "." + "B" * 43

    with pytest.raises(WorkflowTemplateNotFoundError) as error:
        load_builtin_catalog().get(secret_id)

    assert secret_id not in str(error.value)
    assert secret_id not in str(error.value.to_dict())
    assert error.value.context == {}
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


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
        "auth_token: correct-horse-battery-staple",
        "refresh_token: correct-horse-battery-staple",
        "webhook_secret: correct-horse-battery-staple",
        "secret_key: correct-horse-battery-staple",
        "private_key: correct-horse-battery-staple",
        "aws_access_key_id: correct-horse-battery-staple",
        "aws_secret_access_key: correct-horse-battery-staple",
        "aws_security_token: correct-horse-battery-staple",
        "aws_session_token: correct-horse-battery-staple",
        "password: correct-horse-battery-staple",
        "credential: sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        "external_reference: ASIAAAAAAAAAAAAAAAAA",
        "external_reference: whsec_AAAAAAAAAAAAAAAA",
        "external_reference: AIzaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "external_reference: SG.AAAAAAAAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "external_reference: " + "SK" + "0" * 32,
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
    ("key", "placeholder"),
    [
        ("api_key", "${API_KEY}"),
        ("auth_token", "{{ auth_token }}"),
        ("refresh_token", "your_refresh_token"),
        ("webhook_secret", "change_me_before_use"),
        ("secret_key", "<secret_key>"),
        ("private_key", "redacted"),
        ("aws_access_key_id", "${AWS_ACCESS_KEY_ID}"),
        ("aws_secret_access_key", "unset"),
        ("aws_security_token", "<aws_security_token>"),
        ("aws_session_token", "null"),
    ],
)
def test_catalog_accepts_only_complete_sensitive_value_placeholders(
    tmp_path: Path,
    key: str,
    placeholder: str,
) -> None:
    directory = _copy_catalog(tmp_path)
    manifest = _read_manifest(directory)
    entry = cast(dict[str, Any], manifest["templates"][0])
    path = directory / cast(str, entry["yaml"])
    yaml_text = path.read_text(encoding="utf-8").replace(
        "workflow:\n",
        f'workflow:\n  private:\n    {key}: "{placeholder}"\n',
        1,
    )
    _replace_first_template(directory, yaml_text)

    load_catalog(directory)


@pytest.mark.parametrize(
    "value",
    [
        "${API_KEY}actual-secret",
        "{{ api_key }}actual-secret",
        "your_api_key_actual_secret",
        "change_me_before_use_actual_secret",
        "<api_key>actual-secret",
    ],
)
def test_catalog_rejects_placeholder_prefix_followed_by_a_secret_value(
    tmp_path: Path,
    value: str,
) -> None:
    directory = _copy_catalog(tmp_path)
    manifest = _read_manifest(directory)
    entry = cast(dict[str, Any], manifest["templates"][0])
    path = directory / cast(str, entry["yaml"])
    yaml_text = path.read_text(encoding="utf-8").replace(
        "workflow:\n",
        f'workflow:\n  private:\n    api_key: "{value}"\n',
        1,
    )
    _replace_first_template(directory, yaml_text)

    with pytest.raises(WorkflowCatalogError, match="凭据|敏感"):
        load_catalog(directory)


def test_catalog_rejects_placeholder_prefix_in_named_sensitive_variable(
    tmp_path: Path,
) -> None:
    directory = _copy_catalog(tmp_path)
    manifest = _read_manifest(directory)
    entry = cast(dict[str, Any], manifest["templates"][0])
    path = directory / cast(str, entry["yaml"])
    yaml_text = path.read_text(encoding="utf-8").replace(
        "workflow:\n",
        """workflow:
  private:
    - variable: refresh_token
      value: "${REFRESH_TOKEN}actual-secret"
""",
        1,
    )
    _replace_first_template(directory, yaml_text)

    with pytest.raises(WorkflowCatalogError, match="凭据|敏感"):
        load_catalog(directory)


def test_catalog_secret_shaped_template_id_is_never_reflected(tmp_path: Path) -> None:
    directory = _copy_catalog(tmp_path)
    manifest = _read_manifest(directory)
    secret_id = "sk-proj-catalogidmustneverbereflected123456789"
    cast(dict[str, Any], manifest["templates"][0])["id"] = secret_id
    _write_manifest(directory, manifest)

    with pytest.raises(WorkflowCatalogError) as error:
        load_catalog(directory)

    assert secret_id not in str(error.value)


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
