"""Package-owned local service runner contract (no real listener is started)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from ragspine.cli import serve as local_serve


@dataclass
class _FakeConfig:
    db_path: str
    chunk_db_path: str | None = None
    mapping_db_path: str | None = None
    queue_db_path: str | None = None
    manifest_db_path: str | None = None
    provider_type: str = ""
    embedding: str = ""
    workflow_matcher: str = ""
    reranker: str = ""
    vector_store: str = ""
    allowed_upload_root: str | None = None
    n8n_store_path: str = ""


class _FakeQueue:
    pass


class _FakeFaqCache:
    @classmethod
    def empty(cls) -> _FakeFaqCache:
        return cls()


def _components(captured: dict[str, object]) -> local_serve._ServiceComponents:
    def create_app(config: object, **kwargs: object) -> object:
        app = object()
        captured["config"] = config
        captured["app_kwargs"] = kwargs
        captured["app"] = app
        return app

    return local_serve._ServiceComponents(
        create_app=create_app,
        config_type=_FakeConfig,
        queue_type=_FakeQueue,
        faq_cache_type=_FakeFaqCache,
    )


def test_serve_local_maps_workspace_and_uses_inline_queue(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def runner(app: object, host: str, port: int) -> None:
        captured["runner"] = (app, host, port)

    workspace = tmp_path / "kb"
    url = local_serve.serve_local(
        workspace,
        port=8123,
        runner=runner,
        component_loader=lambda: _components(captured),
    )

    config = captured["config"]
    assert isinstance(config, _FakeConfig)
    assert config.db_path == str(workspace / "knowledge.db")
    assert config.chunk_db_path == config.db_path
    assert config.mapping_db_path == str(workspace / "mapping.db")
    assert config.queue_db_path == str(workspace / "review.db")
    assert config.provider_type == "mock"
    assert config.embedding == "none"
    assert config.allowed_upload_root == str(workspace)
    assert config.n8n_store_path == str(workspace / "n8n_store")
    assert isinstance(captured["app_kwargs"], dict)
    assert isinstance(captured["app_kwargs"]["queue"], _FakeQueue)
    assert captured["runner"] == (captured["app"], "127.0.0.1", 8123)
    assert url == "http://127.0.0.1:8123/studio/"
    assert (workspace / "knowledge.db").is_file()


def test_open_uses_injected_browser_scheduler_once(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    scheduled: list[tuple[str, str, int, object]] = []

    def browser(url: str) -> bool:
        del url
        return True

    def schedule(url: str, host: str, port: int, opener: local_serve.BrowserOpener) -> None:
        scheduled.append((url, host, port, opener))

    local_serve.serve_local(
        tmp_path / "kb",
        port=9001,
        open_browser=True,
        runner=lambda app, host, port: None,
        browser=browser,
        browser_scheduler=schedule,
        component_loader=lambda: _components(captured),
    )

    assert scheduled == [("http://127.0.0.1:9001/studio/", "127.0.0.1", 9001, browser)]


def test_open_false_never_schedules_browser(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    scheduled: list[str] = []

    local_serve.serve_local(
        tmp_path / "kb",
        runner=lambda app, host, port: None,
        browser_scheduler=lambda url, host, port, browser: scheduled.append(url),
        component_loader=lambda: _components(captured),
    )

    assert scheduled == []


def test_real_app_factory_receives_fake_queue_and_mounts_studio(tmp_path: Path) -> None:
    """The default component path reuses create_app without constructing Redis."""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    captured: dict[str, object] = {}

    def runner(app: object, host: str, port: int) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    local_serve.serve_local(tmp_path / "kb", runner=runner)

    app = captured["app"]
    assert app.state.queue.__class__.__name__ == "FakeQueue"
    assert app.state.config.redis_url == "redis://localhost:6379/0"
    assert captured["host"] == "127.0.0.1"
    client = fastapi_testclient.TestClient(app)
    assert client.get("/healthz").status_code == 200
    # Packaged releases include Studio. A source tree without built assets may
    # honestly return 404, but the route must never redirect to an external UI.
    assert client.get("/studio/").status_code in {200, 404}


def test_missing_service_extra_has_exact_install_hint(tmp_path: Path) -> None:
    def missing() -> local_serve._ServiceComponents:
        raise local_serve.LocalServeDependencyError(
            "local serve requires the optional [service] extra; install it with: "
            'pip install "rag-spine[service]"'
        )

    with pytest.raises(local_serve.LocalServeDependencyError) as exc:
        local_serve.serve_local(
            tmp_path / "kb",
            runner=lambda app, host, port: None,
            component_loader=missing,
        )

    assert str(exc.value) == (
        "local serve requires the optional [service] extra; install it with: "
        'pip install "rag-spine[service]"'
    )


@pytest.mark.parametrize("port", [0, -1, 65_536])
def test_invalid_port_rejected_before_workspace_creation(tmp_path: Path, port: int) -> None:
    workspace = tmp_path / "kb"
    with pytest.raises(ValueError, match="between 1 and 65535"):
        local_serve.serve_local(workspace, port=port)
    assert not workspace.exists()
