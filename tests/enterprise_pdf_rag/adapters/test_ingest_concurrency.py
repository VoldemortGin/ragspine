"""Ingest concurrency (ADR 0039): model calls overlap, embeddings share a semaphore, HTTP keeps alive.

No real network: model and embedding senders are fakes; the keep-alive tests talk to a loopback
HTTP/1.1 server started here (``tests/conftest.py`` blocks every non-loopback socket).
"""

import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

import ragspine.common.evidence.providers.providers as provider_module
from enterprise_pdf_rag.adapters.folder_pipeline import _document_embedder, _embedding_gate
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.providers import transient
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
)
from ragspine.common.evidence.providers.local_models import (
    LocalEmbeddingAdapter,
    _send_local_once,
)
from ragspine.common.evidence.providers.providers import (
    LLMConfig,
    ProviderRequestError,
    _send_once,
    forget_connections,
    load_local_model_config,
)

PNG = b"\x89PNG\r\n\x1a\nfixture-bytes"
_WAIT = 5.0


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    answer: str


def _config() -> LLMConfig:
    return LLMConfig(api_key=SecretStr("k"), base_url="https://example.invalid", model="m")


def _prompt(payload: bytes) -> str:
    return str(json.loads(payload)["messages"][1]["content"][0]["text"])


def _reply(answer: str) -> bytes:
    return json.dumps(
        {
            "choices": [
                {"message": {"content": json.dumps({"answer": answer})}, "finish_reason": "stop"}
            ],
            "model": "m",
        }
    ).encode()


def _ask(client: JsonCompletionClient, prompt: str) -> str:
    result = client.complete_json(
        task="concurrency", prompt=prompt, image_png=PNG, response_model=_Answer
    )
    return result.parsed.answer


# ---- 1. JsonCompletionClient: the lock no longer spans the network call --------------------------


@pytest.mark.usefixtures("model_cache_backend")
def test_concurrent_calls_of_one_client_overlap_on_the_network(tmp_path: Path) -> None:
    # Both requests must be in flight together to pass the barrier; under a lock held across
    # the transport the second would wait for the first, which would wait for it forever.
    barrier = threading.Barrier(2, timeout=_WAIT)

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        barrier.wait()
        return _reply(_prompt(payload))

    client = JsonCompletionClient(_config(), cache_dir=tmp_path, max_live_calls=2, sender=sender)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda prompt: _ask(client, prompt), ["a", "b"]))
    assert answers == ["a", "b"]
    assert (client.live_call_count, client.cache_hit_count, client.claim_blocked_count) == (2, 0, 0)


@pytest.mark.usefixtures("model_cache_backend")
def test_concurrent_calls_of_one_fingerprint_send_it_once(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()
    sent: list[str] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        sent.append(_prompt(payload))
        entered.set()
        assert release.wait(_WAIT)
        return _reply("same")

    client = JsonCompletionClient(_config(), cache_dir=tmp_path, max_live_calls=4, sender=sender)
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(_ask, client, "same")
        assert entered.wait(_WAIT)
        rest = [pool.submit(_ask, client, "same") for _ in range(3)]
        time.sleep(0.2)  # the followers reach the in-flight call and wait for it
        release.set()
        answers = [first.result(), *(future.result() for future in rest)]
    assert answers == ["same"] * 4
    assert sent == ["same"]
    assert (client.live_call_count, client.cache_hit_count, client.claim_blocked_count) == (1, 3, 0)


@pytest.mark.usefixtures("model_cache_backend")
def test_concurrent_calls_never_overspend_the_budget(tmp_path: Path) -> None:
    barrier = threading.Barrier(2, timeout=_WAIT)

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        barrier.wait()
        return _reply(_prompt(payload))

    client = JsonCompletionClient(_config(), cache_dir=tmp_path, max_live_calls=2, sender=sender)
    outcomes: list[str] = []

    def call(prompt: str) -> None:
        try:
            outcomes.append(_ask(client, prompt))
        except JsonCompletionError as error:
            outcomes.append(error.code)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(call, ["a", "b", "c", "d"]))
    assert sorted(outcomes).count("call_budget_exhausted") == 2
    assert client.live_call_count == 2


@pytest.mark.usefixtures("model_cache_backend")
def test_a_rate_limit_cooldown_is_shared_by_concurrent_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limited = threading.Event()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        limited.set()

    monkeypatch.setattr(transient, "_sleep", sleep)
    monkeypatch.setattr(transient, "_clock", lambda: 100.0)
    monkeypatch.setattr(transient, "_random", lambda: 0.0)
    attempts: list[str] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        prompt = _prompt(payload)
        attempts.append(prompt)
        if attempts == ["a"]:
            raise ProviderRequestError(
                "Provider returned HTTP 429; no retry performed",
                status=429,
                category="http",
                retry_after=7.0,
            )
        return _reply(prompt)

    client = JsonCompletionClient(_config(), cache_dir=tmp_path, max_live_calls=4, sender=sender)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_ask, client, "a")
        assert limited.wait(_WAIT)
        second = pool.submit(_ask, client, "b")
        assert (first.result(), second.result()) == ("a", "b")
    # The 429's Retry-After holds back the retry and the other thread's first attempt alike.
    assert sleeps == [7.0, 7.0]
    assert (client.retry_count, client.live_call_count) == (1, 3)


# ---- 2. embeddings: a semaphore for thread-safe embedders, a lock for the rest -------------------

_EMBEDDING_CONFIG = {
    "APP_EMBEDDING_BASE_URL": "http://127.0.0.1:29002/v1/",
    "APP_EMBEDDING_MODEL": "embedding-model",
    "APP_EMBEDDING_API_KEY": "embedding-secret",
}


def _texts(prefix: str, count: int) -> list[str]:
    return [f"{prefix} object {index} revenue" for index in range(count)]


def _vector(text: str) -> tuple[float, ...]:
    return OfflineDescriptionEmbedder._vector(text)


def test_documents_embed_at_once_through_the_gate_with_their_own_counts() -> None:
    barrier = threading.Barrier(2, timeout=_WAIT)

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        inputs = json.loads(payload)["input"]
        if isinstance(inputs, list) and len(inputs) == 4:
            barrier.wait()  # both documents' first batches are in flight at once
            raise ProviderRequestError("Local model returned HTTP 413", status=413, category="http")
        texts = inputs if isinstance(inputs, list) else [inputs]
        data = [{"index": i, "embedding": list(_vector(text))} for i, text in enumerate(texts)]
        return json.dumps({"data": data}).encode()

    adapter = LocalEmbeddingAdapter(
        load_local_model_config("embedding", _EMBEDDING_CONFIG), sender=sender
    )
    gate = _embedding_gate(adapter, 2)
    views = [_document_embedder(adapter, gate) for _ in range(2)]
    batches = [_texts("left", 4), _texts("right", 4)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        vectors = list(pool.map(lambda view, texts: view.embed_descriptions(texts), views, batches))
    # ADR 0026 still halves a failed batch: one failed 4-batch, then two pairs, per document.
    assert vectors == [tuple(_vector(text) for text in texts) for texts in batches]
    assert [getattr(view, "request_count", None) for view in views] == [3, 3]
    assert adapter.request_count == 6


def test_an_embedder_without_per_thread_counts_is_still_called_one_at_a_time() -> None:
    gate = _embedding_gate(OfflineDescriptionEmbedder(), 4)
    assert gate.acquire(blocking=False)
    assert not gate.acquire(blocking=False)
    gate.release()


def test_embedding_concurrency_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    assert get_settings().embedding_max_concurrency == 4
    monkeypatch.setenv("APP_EMBEDDING_MAX_CONCURRENCY", "2")
    get_settings.cache_clear()
    assert get_settings().embedding_max_concurrency == 2
    monkeypatch.setenv("APP_EMBEDDING_MAX_CONCURRENCY", "0")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()
    get_settings.cache_clear()


# ---- 3. HTTP keep-alive: one connection per thread, reused, renewed when the server drops it ----


class _Server:
    def __init__(self) -> None:
        self.connections = 0
        self.requests = 0
        self.drop_after_reply = False
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                server.connections += 1

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                server.requests += 1
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                # Closing without announcing it: what an idle-timeout looks like to the client.
                self.close_connection = server.drop_after_reply

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]


@pytest.fixture
def http_server() -> Iterator[_Server]:
    server = _Server()
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        forget_connections()
        server.httpd.shutdown()
        server.httpd.server_close()
        thread.join(_WAIT)


def _post_local(server: _Server) -> bytes:
    return _send_local_once(
        f"http://127.0.0.1:{server.port}/v1/embeddings", api_key="k", payload=b"{}", timeout=5.0
    )


def test_local_model_requests_reuse_one_connection(http_server: _Server) -> None:
    for _ in range(3):
        assert _post_local(http_server) == b'{"ok":true}'
    assert (http_server.connections, http_server.requests) == (1, 3)


def test_model_calls_reuse_one_connection(
    http_server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real _send_once over plain HTTP: TLS is the connection class's business, not reuse's.
    monkeypatch.setattr(provider_module, "HTTPSConnection", HTTPConnection)
    url = f"https://127.0.0.1:{http_server.port}/v1/chat/completions"
    for _ in range(3):
        assert _send_once(url, api_key="k", payload=b"{}", timeout=5.0) == b'{"ok":true}'
    assert (http_server.connections, http_server.requests) == (1, 3)


def test_each_thread_keeps_its_own_connection(http_server: _Server) -> None:
    barrier = threading.Barrier(2, timeout=_WAIT)

    def work() -> None:
        barrier.wait()
        for _ in range(2):
            _post_local(http_server)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_WAIT)
    assert (http_server.connections, http_server.requests) == (2, 4)


def test_a_connection_the_server_dropped_is_replaced_without_an_error(
    http_server: _Server,
) -> None:
    http_server.drop_after_reply = True
    for _ in range(3):
        assert _post_local(http_server) == b'{"ok":true}'
    assert (http_server.connections, http_server.requests) == (3, 3)
