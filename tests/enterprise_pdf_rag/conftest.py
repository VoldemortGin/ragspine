"""Tests are local-only; the outbound-network block lives in the root tests/conftest.py (no_network)."""

from pathlib import Path

import pytest


def pytest_configure() -> None:
    if not (Path.cwd() / ".project-root").is_file():
        raise pytest.UsageError("Run pytest from the repository root")


@pytest.fixture
def onnx_runtime_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend onnxruntime / numpy / Pillow are importable, for tests that stub the model itself.

    Only the dependency probes (ADR 0030 / 0031 preflight) are replaced; nothing is imported or
    loaded, so a test using this must inject its own layout blocks / structure recognizer.
    """
    from enterprise_pdf_rag.adapters import onnx_partition, pdfspine_tsr

    monkeypatch.setattr(onnx_partition, "_find_spec", lambda _name: object())
    monkeypatch.setattr(pdfspine_tsr, "find_spec", lambda _name: object())
