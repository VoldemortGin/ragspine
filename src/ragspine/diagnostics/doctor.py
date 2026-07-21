"""Offline-only installation and runtime configuration checks."""

import importlib.util
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ragspine.diagnostics.config import EffectiveConfig, load_effective_config

Severity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class DoctorFinding:
    """One stable, machine-readable diagnostic result."""

    code: str
    severity: Severity
    check: str
    message: str
    remediation: str | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "check": self.check,
            "message": self.message,
            "remediation": self.remediation,
            "path": self.path,
        }


@dataclass(frozen=True)
class DoctorReport:
    """Complete local diagnostic report."""

    config: EffectiveConfig
    findings: tuple[DoctorFinding, ...]

    @property
    def ok(self) -> bool:
        return all(finding.severity != "error" for finding in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "config": self.config.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _dependency(name: str, extra: str, *, required: bool = True) -> DoctorFinding:
    available = importlib.util.find_spec(name) is not None
    if available:
        return DoctorFinding(
            f"dependency.{name}.available", "info", "dependency", f"{name} is available"
        )
    severity: Severity = "error" if required else "warning"
    return DoctorFinding(
        f"dependency.{name}.missing",
        severity,
        "dependency",
        f"optional dependency {name} is not installed",
        f"install rag-spine[{extra}]",
    )


def _path_finding(label: str, raw_path: str, *, directory: bool = False) -> DoctorFinding:
    path = Path(raw_path).expanduser()
    if path.exists():
        correct_type = path.is_dir() if directory else path.is_file()
        if not correct_type:
            return DoctorFinding(
                f"path.{label}.wrong_type",
                "error",
                "path",
                f"{label} has the wrong filesystem type",
                path=str(path),
            )
        if not os.access(path, os.R_OK):
            return DoctorFinding(
                f"path.{label}.unreadable",
                "error",
                "path",
                f"{label} is not readable",
                path=str(path),
            )
        return DoctorFinding(
            f"path.{label}.ready", "info", "path", f"{label} is accessible", path=str(path)
        )
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        return DoctorFinding(
            f"path.{label}.parent_missing",
            "error",
            "path",
            f"parent directory for {label} does not exist",
            "create the parent directory",
            str(path),
        )
    if not os.access(parent, os.W_OK):
        return DoctorFinding(
            f"path.{label}.parent_unwritable",
            "error",
            "path",
            f"parent directory for {label} is not writable",
            path=str(path),
        )
    return DoctorFinding(
        f"path.{label}.creatable", "info", "path", f"{label} can be created", path=str(path)
    )


def run_doctor(
    config: EffectiveConfig | None = None,
    *,
    config_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> DoctorReport:
    """Run deterministic filesystem/package/key checks without network access."""
    environment = os.environ if env is None else env
    effective = config or load_effective_config(config_path, env=environment)
    runtime = effective.runtime
    findings: list[DoctorFinding] = []

    if runtime.provider_type == "anthropic":
        findings.append(_dependency("anthropic", "llm"))
        if not runtime.model.strip():
            findings.append(
                DoctorFinding(
                    "model.anthropic.empty",
                    "error",
                    "model",
                    "Anthropic model is empty",
                    "set RAGSPINE_MODEL",
                )
            )
        if not environment.get("ANTHROPIC_API_KEY"):
            findings.append(
                DoctorFinding(
                    "key.anthropic.missing", "error", "key", "ANTHROPIC_API_KEY is not configured"
                )
            )
    elif runtime.provider_type != "mock":
        findings.append(
            DoctorFinding(
                "provider.unknown",
                "error",
                "provider",
                f"unknown provider_type: {runtime.provider_type}",
            )
        )

    if runtime.embedding == "openai":
        findings.append(_dependency("openai", "llm"))
        if not environment.get("OPENAI_API_KEY"):
            findings.append(
                DoctorFinding(
                    "key.openai.missing", "error", "key", "OPENAI_API_KEY is not configured"
                )
            )
    elif runtime.embedding == "onnx":
        findings.append(_dependency("fastembed", "embed-onnx"))
    elif runtime.embedding == "auto":
        findings.append(_dependency("fastembed", "embed-onnx", required=False))

    if runtime.reranker == "cross_encoder":
        findings.append(_dependency("fastembed", "rerank"))

    if runtime.vector_store == "sqlite_vec":
        findings.append(_dependency("sqlite_vec", "vector"))

    db_values = {
        "db": runtime.db_path,
        "chunk_db": runtime.chunk_db_path,
        "mapping_db": runtime.mapping_db_path,
        "queue_db": runtime.queue_db_path,
        "manifest_db": runtime.manifest_db_path,
    }
    for label, value in db_values.items():
        if value and value != ":memory:":
            findings.append(_path_finding(label, value))
    if runtime.allowed_upload_root:
        findings.append(
            _path_finding("allowed_upload_root", runtime.allowed_upload_root, directory=True)
        )
    if runtime.studio_dir:
        findings.append(_path_finding("studio_dir", runtime.studio_dir, directory=True))
    findings.append(_path_finding("n8n_store", runtime.n8n_store_path, directory=True))
    return DoctorReport(effective, tuple(findings))
