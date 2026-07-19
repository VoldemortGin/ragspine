"""Side-effect-free workflow readiness preflight over the real compiler and L0 gate."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ragspine.dify.api import compile_dify_yaml
from ragspine.dify.errors import DifyCompileError
from ragspine.service.dify.safety import DifyUnsafeError, assert_runnable
from ragspine.workflows.errors import WorkflowFormatError
from ragspine.workflows.formats import dump_dify_yaml, load_workflow

READINESS_SCHEMA_VERSION = 1
_SECRET_SHAPED = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9_])SG\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}={0,2}"),
)


@dataclass(frozen=True)
class WorkflowReadiness:
    """Public JSON report plus the normalized YAML consumed by packaging."""

    report: dict[str, object]
    workflow_yaml: str | None


def _safe_warnings(warnings: tuple[str, ...]) -> list[str]:
    safe: list[str] = []
    for warning in warnings:
        for pattern in _SECRET_SHAPED:
            warning = pattern.sub("[REDACTED]", warning)
        safe.append(warning)
    return safe


def _start_inputs(workflow: dict[str, object]) -> list[dict[str, object]]:
    raw_workflow = workflow.get("workflow")
    if not isinstance(raw_workflow, dict):
        return []
    graph = raw_workflow.get("graph")
    if not isinstance(graph, dict):
        return []
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return []
    inputs: list[dict[str, object]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        data = node.get("data")
        if not isinstance(data, dict) or data.get("type") != "start":
            continue
        variables = data.get("variables")
        if not isinstance(variables, list):
            continue
        for variable in variables:
            if not isinstance(variable, dict):
                continue
            name = variable.get("variable")
            if not isinstance(name, str) or not name:
                continue
            label = variable.get("label")
            input_type = variable.get("type")
            inputs.append(
                {
                    "name": name,
                    "label": label if isinstance(label, str) else name,
                    "type": input_type if isinstance(input_type, str) else "text-input",
                    "required": variable.get("required") is True,
                }
            )
    return inputs


def _requirements(workflow: dict[str, object]) -> list[dict[str, object]]:
    raw_workflow = workflow.get("workflow")
    graph = raw_workflow.get("graph") if isinstance(raw_workflow, dict) else None
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    providers: set[str] = set()
    if isinstance(nodes, list):
        for node in nodes:
            data = node.get("data") if isinstance(node, dict) else None
            if not isinstance(data, dict) or data.get("type") != "llm":
                continue
            model = data.get("model")
            provider = model.get("provider") if isinstance(model, dict) else None
            providers.add(provider if isinstance(provider, str) and provider else "llm")
    return [
        {"kind": "llm_provider", "name": provider, "required": True}
        for provider in sorted(providers)
    ]


def check_workflow(path: str | Path) -> WorkflowReadiness:
    """Normalize, compile, and L0-check one existing workflow without executing it."""

    try:
        workflow = load_workflow(path)
    except WorkflowFormatError as exc:
        return WorkflowReadiness(
            report={
                "schema_version": READINESS_SCHEMA_VERSION,
                "status": "blocked",
                "checks": {
                    "format": {"status": "blocked", "code": exc.code},
                    "compile": {"status": "not_run"},
                    "runnable": {"status": "not_run"},
                },
                "start_inputs": [],
                "warnings": [],
                "requirements": [],
            },
            workflow_yaml=None,
        )
    workflow_yaml = dump_dify_yaml(workflow)
    try:
        compiled = compile_dify_yaml(workflow_yaml, analyze=False)
    except DifyCompileError as exc:
        return WorkflowReadiness(
            report={
                "schema_version": READINESS_SCHEMA_VERSION,
                "status": "blocked",
                "checks": {
                    "format": {"status": "passed"},
                    "compile": {"status": "blocked", "code": exc.code},
                    "runnable": {"status": "not_run"},
                },
                "start_inputs": _start_inputs(workflow),
                "warnings": [],
                "requirements": _requirements(workflow),
            },
            workflow_yaml=workflow_yaml,
        )
    try:
        assert_runnable(compiled.code)
    except DifyUnsafeError as exc:
        blocked_report: dict[str, Any] = {
            "schema_version": READINESS_SCHEMA_VERSION,
            "status": "blocked",
            "checks": {
                "format": {"status": "passed"},
                "compile": {"status": "passed"},
                "runnable": {"status": "blocked", "code": exc.code},
            },
            "start_inputs": _start_inputs(workflow),
            "warnings": _safe_warnings(compiled.code.warnings),
            "requirements": _requirements(workflow),
        }
        return WorkflowReadiness(report=blocked_report, workflow_yaml=workflow_yaml)
    ready_report: dict[str, Any] = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "status": "ready",
        "checks": {
            "format": {"status": "passed"},
            "compile": {"status": "passed"},
            "runnable": {"status": "passed"},
        },
        "start_inputs": _start_inputs(workflow),
        "warnings": _safe_warnings(compiled.code.warnings),
        "requirements": _requirements(workflow),
    }
    return WorkflowReadiness(report=ready_report, workflow_yaml=workflow_yaml)


__all__ = ["READINESS_SCHEMA_VERSION", "WorkflowReadiness", "check_workflow"]
