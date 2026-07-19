"""Shared in-memory deploy bundle assembly for CLI and HTTP adapters."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from ragspine.workflows.readiness import WorkflowReadiness, check_workflow_document

PACKAGE_COMPOSE = """services:
  app:
    image: ghcr.io/voldemortgin/ragspine:${RAGSPINE_TAG:?}
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      RAGSPINE_PROVIDER: mock
      RAGSPINE_DIFY_RUN_ENABLED: "true"
      RAGSPINE_DIFY_PUBLIC_APPS: "${RAGSPINE_APP_KEY:?}=/app/workflows/workflow.yml"
    volumes:
      - ./workflow.yml:/app/workflows/workflow.yml:ro
"""
PACKAGE_ENV_EXAMPLE = """RAGSPINE_TAG=latest
RAGSPINE_APP_KEY=
"""
MAX_PACKAGE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class WorkflowPackage:
    readiness: WorkflowReadiness
    files: tuple[tuple[str, bytes], ...]

    @property
    def ready(self) -> bool:
        return self.readiness.report["status"] == "ready"


def package_workflow_document(workflow: dict[str, object]) -> WorkflowPackage:
    """Return the exact deploy files for a ready canonical workflow."""

    return package_workflow_readiness(check_workflow_document(workflow))


def package_workflow_readiness(readiness: WorkflowReadiness) -> WorkflowPackage:
    """Assemble files from a readiness result shared by CLI and HTTP."""

    if readiness.report["status"] != "ready" or readiness.workflow_yaml is None:
        return WorkflowPackage(readiness=readiness, files=())
    return WorkflowPackage(
        readiness=readiness,
        files=(
            ("workflow.yml", readiness.workflow_yaml.encode("utf-8")),
            ("compose.yaml", PACKAGE_COMPOSE.encode("utf-8")),
            (".env.example", PACKAGE_ENV_EXAMPLE.encode("utf-8")),
            (".gitignore", b".env\n"),
        ),
    )


def workflow_package_zip(package: WorkflowPackage) -> bytes:
    """Build one deterministic bounded ZIP; blocked packages are rejected."""

    if not package.ready or not package.files:
        raise ValueError("workflow readiness blocked")
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in package.files:
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    payload = buffer.getvalue()
    if len(payload) > MAX_PACKAGE_BYTES:
        raise ValueError("workflow package exceeds output limit")
    return payload


__all__ = [
    "MAX_PACKAGE_BYTES",
    "PACKAGE_COMPOSE",
    "PACKAGE_ENV_EXAMPLE",
    "WorkflowPackage",
    "package_workflow_document",
    "package_workflow_readiness",
    "workflow_package_zip",
]
