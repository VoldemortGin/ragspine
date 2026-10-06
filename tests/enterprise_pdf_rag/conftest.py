"""Tests are local-only; the ASGI transport uses no listening socket."""

import socket
from pathlib import Path
from typing import Never

import pytest


def pytest_configure() -> None:
    if not (Path.cwd() / ".project-root").is_file():
        raise pytest.UsageError("Run pytest from the repository root")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("Network calls are forbidden in the offline test gate")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def onnx_runtime_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend onnxruntime / numpy / Pillow are importable, for tests that stub the model itself.

    Only the dependency probes (ADR 0030 / 0031 preflight) are replaced; nothing is imported or
    loaded, so a test using this must inject its own layout blocks / structure recognizer.
    """
    from enterprise_pdf_rag.adapters import onnx_partition, pdfspine_tsr

    monkeypatch.setattr(onnx_partition, "_find_spec", lambda _name: object())
    monkeypatch.setattr(pdfspine_tsr, "find_spec", lambda _name: object())
