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
