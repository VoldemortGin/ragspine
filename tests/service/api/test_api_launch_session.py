"""launch-session 只读端点：token → {id,name,yaml}；未知/畸形 id 一律同形 404、不回显。"""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from ragspine.agent.llm_provider import MockProvider
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.studio.launch import LaunchSessionRegistry
from ragspine.service.tasks.task_queue import FakeQueue

_YAML = "app:\n  name: demo\nkind: app\n"

_NOT_FOUND_BODY = {
    "error": {
        "type": "LaunchSessionNotFound",
        "message": "launch session not found",
        "request_id": None,
    }
}


def make_client(tmp_path: Path, *, registry: LaunchSessionRegistry | None = None) -> TestClient:
    config = ServiceConfig(db_path=str(tmp_path / "fact.db"))
    kwargs = {} if registry is None else {"launch_sessions": registry}
    app = create_app(
        config, provider=MockProvider(), queue=FakeQueue(), faq_cache=FAQCache.empty(), **kwargs
    )
    return TestClient(app)


def test_registered_session_returns_exact_contract_shape(tmp_path: Path) -> None:
    registry = LaunchSessionRegistry()
    session = registry.register(name="demo", yaml=_YAML)
    client = make_client(tmp_path, registry=registry)

    resp = client.get(f"/v1/launch-sessions/{session.session_id}")

    assert resp.status_code == 200
    # 契约冻结：恰好 {id, name, yaml} 三个键，yaml 为 Dify DSL YAML 文本。
    assert resp.json() == {"id": session.session_id, "name": "demo", "yaml": _YAML}


def test_unknown_session_id_is_404_with_frozen_error_shape(tmp_path: Path) -> None:
    client = make_client(tmp_path, registry=LaunchSessionRegistry())

    resp = client.get("/v1/launch-sessions/AAAAAAAAAAAAAAAAAAAAAA")

    assert resp.status_code == 404
    assert resp.json() == _NOT_FOUND_BODY


def test_overlong_session_id_is_404_and_never_echoed(tmp_path: Path) -> None:
    client = make_client(tmp_path, registry=LaunchSessionRegistry())
    overlong = "a" * 65

    resp = client.get(f"/v1/launch-sessions/{overlong}")

    assert resp.status_code == 404
    assert resp.json() == _NOT_FOUND_BODY
    assert overlong not in resp.text


def test_non_url_safe_token_characters_are_404(tmp_path: Path) -> None:
    client = make_client(tmp_path, registry=LaunchSessionRegistry())

    resp = client.get("/v1/launch-sessions/bad.token!")

    assert resp.status_code == 404
    assert resp.json() == _NOT_FOUND_BODY


def test_default_app_without_registered_sessions_is_404(tmp_path: Path) -> None:
    client = make_client(tmp_path)  # create_app 默认装配一个空 registry

    resp = client.get("/v1/launch-sessions/AAAAAAAAAAAAAAAAAAAAAA")

    assert resp.status_code == 404
    assert resp.json() == _NOT_FOUND_BODY


def test_registry_evicts_oldest_beyond_capacity_of_eight() -> None:
    registry = LaunchSessionRegistry()

    sessions = [registry.register(name=f"s{i}", yaml=_YAML) for i in range(9)]

    assert registry.get(sessions[0].session_id) is None  # FIFO：最旧被淘汰
    for session in sessions[1:]:
        assert registry.get(session.session_id) == session


def test_registry_tokens_are_unique_url_safe_and_bounded() -> None:
    registry = LaunchSessionRegistry()

    ids = [registry.register(name="s", yaml=_YAML).session_id for _ in range(32)]

    assert len(set(ids)) == len(ids)
    for session_id in ids:
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id)
