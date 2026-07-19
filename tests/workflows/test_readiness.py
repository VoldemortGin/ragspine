"""Workflow readiness reports reuse the real compiler and L0 safety gate."""

import json
from pathlib import Path

from ragspine.workflows.readiness import check_workflow

DIFY_FIXTURES = Path(__file__).resolve().parents[1] / "dify" / "fixtures"


def test_ready_workflow_reports_inputs_provider_and_stable_checks() -> None:
    result = check_workflow(DIFY_FIXTURES / "seq.yml")

    assert result.report == {
        "schema_version": 1,
        "status": "ready",
        "checks": {
            "format": {"status": "passed"},
            "compile": {"status": "passed"},
            "runnable": {"status": "passed"},
        },
        "start_inputs": [
            {
                "name": "question",
                "label": "问题",
                "type": "text-input",
                "required": True,
            }
        ],
        "warnings": [],
        "requirements": [
            {
                "kind": "llm_provider",
                "name": "anthropic",
                "required": True,
            }
        ],
    }
    assert result.workflow_yaml is not None


def test_unsupported_workflow_is_blocked_with_compiler_warnings() -> None:
    result = check_workflow(DIFY_FIXTURES / "agent_tool.yml")

    assert result.report["status"] == "blocked"
    assert result.report["checks"] == {
        "format": {"status": "passed"},
        "compile": {"status": "passed"},
        "runnable": {"status": "blocked", "code": "dify.unsafe"},
    }
    assert result.report["warnings"]
    assert result.workflow_yaml is not None


def test_http_workflow_is_blocked_while_http_gate_is_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("RAGSPINE_DIFY_HTTP_ENABLED", raising=False)
    source = tmp_path / "http.yml"
    source.write_text(
        """
app: {mode: workflow, name: http-gate}
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, variables: [{variable: q, type: text-input}]}
      - id: http_1
        data: {type: http-request, method: get, url: 'http://example.com'}
      - id: end_1
        data:
          type: end
          outputs: [{variable: body, value_selector: [http_1, body]}]
    edges:
      - {source: start_1, target: http_1}
      - {source: http_1, target: end_1}
""",
        encoding="utf-8",
    )

    result = check_workflow(source)

    assert result.report["status"] == "blocked"
    assert result.report["checks"]["runnable"] == {
        "status": "blocked",
        "code": "dify.unsafe",
    }


def test_malformed_workflow_has_opaque_blocked_format_report(tmp_path: Path) -> None:
    secret = "sk-super-secret-value-123456"
    source = tmp_path / "broken.yml"
    source.write_text(f"app: [{secret}\n", encoding="utf-8")

    result = check_workflow(source)

    assert result.report == {
        "schema_version": 1,
        "status": "blocked",
        "checks": {
            "format": {"status": "blocked", "code": "workflow.format"},
            "compile": {"status": "not_run"},
            "runnable": {"status": "not_run"},
        },
        "start_inputs": [],
        "warnings": [],
        "requirements": [],
    }
    assert result.workflow_yaml is None
    assert secret not in json.dumps(result.report)


def test_valid_document_with_unsupported_mode_is_compile_blocked(tmp_path: Path) -> None:
    source = tmp_path / "unsupported.yml"
    source.write_text(
        "app:\n  mode: chat\nworkflow:\n  graph:\n    nodes: []\n    edges: []\n",
        encoding="utf-8",
    )

    result = check_workflow(source)

    assert result.report["status"] == "blocked"
    assert result.report["checks"] == {
        "format": {"status": "passed"},
        "compile": {"status": "blocked", "code": "dify.unsupported_app_mode"},
        "runnable": {"status": "not_run"},
    }


def test_blocking_warnings_redact_secret_shaped_node_ids(tmp_path: Path) -> None:
    secret = "sk-super-secret-value-123456"
    source = tmp_path / "unsafe.yml"
    source.write_text(
        (DIFY_FIXTURES / "agent_tool.yml")
        .read_text(encoding="utf-8")
        .replace("tool_1", secret),
        encoding="utf-8",
    )

    result = check_workflow(source)

    encoded = json.dumps(result.report)
    assert result.report["status"] == "blocked"
    assert result.report["warnings"]
    assert secret not in encoded
    assert "[REDACTED]" in encoded
