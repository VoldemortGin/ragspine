"""Deploy bundle assembly is deterministic and readiness-gated."""

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from ragspine.workflows.formats import load_workflow
from ragspine.workflows.packaging import (
    package_workflow_document,
    workflow_package_zip,
)

DIFY_FIXTURES = Path(__file__).resolve().parents[1] / "dify" / "fixtures"


def test_ready_document_builds_exact_deterministic_deploy_bundle() -> None:
    package = package_workflow_document(load_workflow(DIFY_FIXTURES / "seq.yml"))

    first = workflow_package_zip(package)
    second = workflow_package_zip(package)

    assert package.ready is True
    assert first == second
    with ZipFile(BytesIO(first)) as archive:
        assert archive.namelist() == [
            "workflow.yml",
            "compose.yaml",
            ".env.example",
            ".gitignore",
        ]
        assert archive.read("workflow.yml") == dict(package.files)["workflow.yml"]
        assert b"RAGSPINE_DIFY_PUBLIC_APPS" in archive.read("compose.yaml")
        assert archive.read(".gitignore") == b".env\n"


def test_blocked_document_never_builds_an_archive() -> None:
    package = package_workflow_document(load_workflow(DIFY_FIXTURES / "agent_tool.yml"))

    assert package.ready is False
    assert package.files == ()
    with pytest.raises(ValueError, match="readiness blocked"):
        workflow_package_zip(package)
