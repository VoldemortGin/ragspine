"""Local loopback service runner for a durable RAGSpine workspace.

This module is package-owned (unlike repository ``scripts/``), but remains a
thin assembly edge: it maps the high-level workspace layout into
``ServiceConfig``, injects the synchronous ``FakeQueue``, mounts the packaged
Studio through ``create_app``, and delegates the blocking server loop to an
injectable runner.

The service dependency is loaded lazily so importing :mod:`ragspine` and using
the base offline API never requires FastAPI or Uvicorn.
"""

from __future__ import annotations

import socket
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ragspine.agent.llm_provider import MockProvider
from ragspine.facade import RAGSpine

_LOOPBACK_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_SERVICE_EXTRA_HINT = 'pip install "rag-spine[service]"'


class LocalServeError(RuntimeError):
    """A local server could not be assembled or started safely."""


class LocalServeDependencyError(LocalServeError):
    """The optional service dependencies are unavailable."""


@dataclass(frozen=True)
class _ServiceComponents:
    create_app: Callable[..., object]
    config_type: type[Any]
    queue_type: type[Any]
    faq_cache_type: type[Any]


ServerRunner = Callable[[object, str, int], None]
BrowserOpener = Callable[[str], object]
BrowserScheduler = Callable[[str, str, int, BrowserOpener], None]
ComponentLoader = Callable[[], _ServiceComponents]


def _load_service_components() -> _ServiceComponents:
    """Load the optional HTTP edge only when local serving is requested."""

    try:
        from ragspine.service.api.app import create_app
        from ragspine.service.config import ServiceConfig
        from ragspine.service.faq.faq_cache import FAQCache
        from ragspine.service.tasks.task_queue import FakeQueue
    except ImportError as exc:
        raise LocalServeDependencyError(
            f"local serve requires the optional [service] extra; install it with: "
            f"{_SERVICE_EXTRA_HINT}"
        ) from exc
    return _ServiceComponents(create_app, ServiceConfig, FakeQueue, FAQCache)


def _run_uvicorn(app: object, host: str, port: int) -> None:
    """Run the production local server; imported lazily with the service extra."""

    try:
        import uvicorn
    except ImportError as exc:
        raise LocalServeDependencyError(
            f"local serve requires the optional [service] extra; install it with: "
            f"{_SERVICE_EXTRA_HINT}"
        ) from exc
    uvicorn.run(cast(Any, app), host=host, port=port)


def _open_when_ready(
    url: str,
    host: str,
    port: int,
    browser: BrowserOpener,
    *,
    timeout_s: float = 10.0,
) -> None:
    """Open one browser tab after the loopback listener becomes reachable."""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                browser(url)
                return
        except OSError:
            time.sleep(0.05)


def _schedule_browser(url: str, host: str, port: int, browser: BrowserOpener) -> None:
    """Schedule browser launch without delaying the blocking server runner."""

    threading.Thread(
        target=_open_when_ready,
        args=(url, host, port, browser),
        daemon=True,
        name="ragspine-local-browser",
    ).start()


def serve_local(
    workspace: str | Path = ".ragspine",
    *,
    port: int = _DEFAULT_PORT,
    open_browser: bool = False,
    runner: ServerRunner | None = None,
    browser: BrowserOpener | None = None,
    browser_scheduler: BrowserScheduler | None = None,
    component_loader: ComponentLoader | None = None,
) -> str:
    """Serve one workspace through the API and packaged Studio on loopback.

    ``runner`` and ``browser_scheduler`` are explicit seams for tests and
    embedders; the defaults call Uvicorn and wait for readiness in a daemon
    thread. The host is intentionally not configurable.

    Returns the Studio URL after the runner exits (normally only in tests or
    during a graceful server shutdown).
    """

    if not 1 <= port <= 65_535:
        raise ValueError("port must be between 1 and 65535")

    root = Path(workspace).expanduser().resolve()
    # Reuse the facade's canonical workspace initialization rather than
    # duplicating schema ownership in the HTTP adapter.
    with RAGSpine.local(root):
        pass

    load = component_loader or _load_service_components
    components = load()
    config = components.config_type(
        db_path=str(root / "knowledge.db"),
        chunk_db_path=str(root / "knowledge.db"),
        mapping_db_path=str(root / "mapping.db"),
        queue_db_path=str(root / "review.db"),
        manifest_db_path=str(root / "manifest.db"),
        provider_type="mock",
        embedding="none",
        workflow_matcher="none",
        reranker="none",
        vector_store="none",
        allowed_upload_root=str(root),
        n8n_store_path=str(root / "n8n_store"),
    )
    app = components.create_app(
        config,
        provider=MockProvider(),
        queue=components.queue_type(),
        faq_cache=components.faq_cache_type.empty(),
    )

    url = f"http://{_LOOPBACK_HOST}:{port}/studio/"
    if open_browser:
        schedule = browser_scheduler or _schedule_browser
        schedule(url, _LOOPBACK_HOST, port, browser or webbrowser.open)

    (runner or _run_uvicorn)(app, _LOOPBACK_HOST, port)
    return url


__all__ = [
    "BrowserOpener",
    "BrowserScheduler",
    "LocalServeDependencyError",
    "LocalServeError",
    "ServerRunner",
    "serve_local",
]
