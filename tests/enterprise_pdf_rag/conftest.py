"""Tests are local-only; the outbound-network block lives in the root tests/conftest.py (no_network)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.object_backend.probe import clear_probe_cache


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


@pytest.fixture(params=["files", "sqlite"])
def model_cache_backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """Run a test once per model-cache backend (sqlite object store PR-3): every
    ``JsonCompletionClient`` built without an explicit ``backend`` opens its cache as
    ``files`` (the flat ``requests`` / ``responses`` / ``contexts`` layout) or ``sqlite``
    (``model-cache.sqlite``, explicit: a failed probe raises instead of falling back)."""
    kind = str(request.param)
    monkeypatch.setenv("APP_OBJECT_STORE_BACKEND", kind)
    get_settings.cache_clear()
    clear_probe_cache()
    yield kind
    get_settings.cache_clear()
