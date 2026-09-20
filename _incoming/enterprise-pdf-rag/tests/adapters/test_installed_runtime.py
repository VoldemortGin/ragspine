"""Installed runtime behavior must not depend on checkout-relative resources."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from enterprise_pdf_rag.cli import main
from enterprise_pdf_rag.core.settings import get_settings


def test_region_catalog_survives_an_install_without_the_source_checkout(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    shutil.copytree(
        Path("src/enterprise_pdf_rag"),
        installed / "enterprise_pdf_rag",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    root = tmp_path / "runtime"
    root.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
from enterprise_pdf_rag.adapters.aia_candidates import candidates_for
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar
from enterprise_pdf_rag.processing.models import PageInput

source = 'df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e'
page = PageInput('a' * 64, source, 0, 960.0, 540.0,
    AssetRef('b' * 64, 'image/svg+xml', 1), TextSidecar('source-text-v1', source, 0, ()))
print(json.dumps({'candidates': len(candidates_for(page))}))
""",
        ],
        cwd=root,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(installed),
            "APP_ROOT_DIR": str(root),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"candidates": 0}


@pytest.mark.parametrize("configured", ["persistent-data", "/tmp/rag-deployment-data"])
def test_deployment_data_directory_controls_the_actual_aia_stores(
    tmp_path: Path, configured: str
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
from enterprise_pdf_rag.adapters.aia_ingestion import AIA_INPUT, AIA_OUTPUT
from enterprise_pdf_rag.adapters.processing_runtime import PROCESSING_OUTPUT
print(json.dumps([str(AIA_INPUT), str(AIA_OUTPUT), str(PROCESSING_OUTPUT)]))
""",
        ],
        cwd=elsewhere,
        env={
            "PATH": os.defpath,
            "APP_ROOT_DIR": str(runtime),
            "APP_DATA_DIR": configured,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = (runtime / configured).resolve()
    expected_output = data / "output/aia-2026-interim"
    assert json.loads(result.stdout) == [
        str(data / "samples/aia-group-2026-interim-results-presentation.pdf"),
        str(expected_output),
        str(expected_output / "pages-001-020"),
    ]


def test_installed_serve_command_requires_explicit_mode_before_listening(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_EXECUTION_MODE", "unconfigured")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as failure:
            main(["serve", "--port", "8769"])
        assert failure.value.code == 2
        assert "APP_EXECUTION_MODE" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()
