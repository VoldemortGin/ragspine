"""Tests are local-only; the outbound-network block lives in the root tests/conftest.py (no_network)."""

from collections.abc import Iterator
from pathlib import Path

import pytest


def pytest_configure() -> None:
    if not (Path.cwd() / ".project-root").is_file():
        raise pytest.UsageError("Run pytest from the repository root")


def _select_backend(monkeypatch: pytest.MonkeyPatch, kind: str) -> Iterator[str]:
    from ragspine.common.evidence.configs import get_settings

    monkeypatch.setenv("APP_OBJECT_STORE_BACKEND", kind)
    get_settings.cache_clear()
    yield kind
    get_settings.cache_clear()


@pytest.fixture(params=["files", "sqlite"], ids=["files-backend", "sqlite-backend"])
def object_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Run the test once per store backend (ADR 0036): APP_OBJECT_STORE_BACKEND=files / sqlite.

    The explicit ``sqlite`` mode refuses to fall back silently, so a test that passes under
    this fixture proves the wiring on both layouts. Tests that tamper with stored bytes use
    it together with backend-aware helpers (or skip the arm that has no file to tamper with).
    """
    yield from _select_backend(monkeypatch, str(request.param))


@pytest.fixture
def files_object_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Pin the file layout (tests that assert the exact on-disk file tree)."""
    yield from _select_backend(monkeypatch, "files")


@pytest.fixture
def sqlite_object_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Pin the sqlite backend explicitly (probe failure raises, never falls back)."""
    yield from _select_backend(monkeypatch, "sqlite")


@pytest.fixture
def onnx_runtime_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend onnxruntime / numpy / Pillow are importable, for tests that stub the model itself.

    Only the dependency probes (ADR 0030 / 0031 preflight) are replaced; nothing is imported or
    loaded, so a test using this must inject its own layout blocks / structure recognizer.
    """
    from enterprise_pdf_rag.adapters import onnx_partition, pdfspine_tsr

    monkeypatch.setattr(onnx_partition, "_find_spec", lambda _name: object())
    monkeypatch.setattr(pdfspine_tsr, "find_spec", lambda _name: object())
