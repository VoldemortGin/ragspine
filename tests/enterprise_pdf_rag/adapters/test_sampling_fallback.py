"""An endpoint that refuses a sampling parameter: read only error.param / error.code, drop it, resend.

The real ``_send_once`` runs against a scripted ``HTTPSConnection``; no socket is opened.
"""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

import ragspine.common.evidence.providers.providers as provider_module
from ragspine.common.evidence.providers.json_completion import (
    DEGRADABLE_SAMPLING_PARAMETERS,
    JsonCompletionClient,
    JsonCompletionError,
    JsonCompletionResult,
    forget_unsupported_sampling_parameters,
    unsupported_sampling_parameters,
)
from ragspine.common.evidence.providers.providers import LLMConfig, ProviderRequestError

PNG = b"\x89PNG\r\n\x1a\nfixture-bytes"
KEY = "test-secret-never-written"
# Never allowed anywhere on disk, in an exception or in a diagnostic: the provider's own words.
MARKER = "PROVIDER-MESSAGE-MARKER-5c1f"
AZURE_TEMPERATURE_400 = json.dumps(
    {
        "error": {
            "message": f"Unsupported value: 'temperature' does not support 0.0 with this model. "
            f"Only the default (1) value is supported. {MARKER}",
            "type": "invalid_request_error",
            "param": "temperature",
            "code": "unsupported_value",
        }
    }
).encode()


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    answer: str


def _config(model: str = "test-model", **update: object) -> LLMConfig:
    config = LLMConfig(api_key=SecretStr(KEY), base_url="https://example.invalid", model=model)
    return config.model_copy(update=update) if update else config


def _ok(answer: str = "observed") -> bytes:
    return json.dumps(
        {
            "model": "reported-model",
            "choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps({"answer": answer})}}
            ],
        }
    ).encode()


def _error(param: object, code: object = "unsupported_value", **extra: object) -> bytes:
    error: dict[str, object] = {"message": f"refused {MARKER}", "type": "invalid_request_error"}
    if param is not None:
        error["param"] = param
    if code is not None:
        error["code"] = code
    return json.dumps({"error": {**error, **extra}}).encode()


Rule = Callable[[dict[str, Any]], tuple[int, bytes]]


def _refuses(*names: str) -> Rule:
    """400 the first listed parameter still present, Azure-style; otherwise a valid answer."""

    def rule(body: dict[str, Any]) -> tuple[int, bytes]:
        for name in names:
            if name in body:
                return 400, _error(name) if name != "temperature" else AZURE_TEMPERATURE_400
        return 200, _ok()

    return rule


class _Endpoint:
    """A scripted HTTPSConnection: records every body, answers ``rule(body)``, tracks reads."""

    def __init__(self, rule: Rule) -> None:
        self.rule = rule
        self.sent: list[dict[str, Any]] = []
        self.reads: list[tuple[int, int]] = []

    def connection(self) -> type:
        endpoint = self

        class Response:
            def __init__(self, status: int, body: bytes) -> None:
                self.status = status
                self._body = body

            def read(self, amount: int = -1) -> bytes:
                endpoint.reads.append((self.status, amount))
                if self.status not in (200, 400):
                    raise AssertionError("Only a 400 error body may be read")
                return self._body if amount < 0 else self._body[:amount]

        class Connection:
            def __init__(self, host: str, *, timeout: float) -> None:
                self._body: dict[str, Any] = {}

            def request(
                self, method: str, path: str, *, body: bytes, headers: dict[str, str]
            ) -> None:
                assert (method, path) == ("POST", "/v1/chat/completions")
                self._body = json.loads(body)
                endpoint.sent.append(self._body)

            def getresponse(self) -> Response:
                return Response(*endpoint.rule(self._body))

            def close(self) -> None:
                pass

        return Connection

    def with_temperature(self) -> int:
        return sum("temperature" in body for body in self.sent)


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> Callable[[Rule], _Endpoint]:
    def install(rule: Rule) -> _Endpoint:
        scripted = _Endpoint(rule)
        monkeypatch.setattr(provider_module, "HTTPSConnection", scripted.connection())
        return scripted

    return install


def _client(
    cache: Path, *, budget: int = 10, seed: int | None = None, **config: object
) -> JsonCompletionClient:
    return JsonCompletionClient(
        _config(**config), cache_dir=cache, max_live_calls=budget, seed=seed
    )


def _ask(
    client: JsonCompletionClient, prompt: str = "read", *, vision: bool = False
) -> JsonCompletionResult[_Answer]:
    if vision:
        return client.complete_json(task="t", prompt=prompt, image_png=PNG, response_model=_Answer)
    return client.complete_text_json(task="t", prompt=prompt, response_model=_Answer)


def _files(cache: Path) -> str:
    return "".join(path.read_text(errors="replace") for path in cache.rglob("*") if path.is_file())


def _records(cache: Path) -> dict[str, dict[str, Any]]:
    return {path.name: json.loads(path.read_text()) for path in (cache / "requests").glob("*.json")}


# ---- 0. the constant the notebook shares -----------------------------------------------------


def test_only_sampling_parameters_may_ever_be_dropped() -> None:
    assert frozenset({"temperature", "seed"}) == DEGRADABLE_SAMPLING_PARAMETERS


def test_the_registry_starts_empty_in_every_test() -> None:
    assert unsupported_sampling_parameters(_config()) == ()


# ---- 1. a supporting endpoint is byte-for-byte unchanged ------------------------------------

# Fingerprints and payload digests the client produced before the fallback existed.
_PINNED = {
    (None, False): (
        "314120774b734f2c31be63d2ad60933595c8d711a01d58d89c303ae9e1a95527",
        "9c100b52b1b7b3291daa68e128bbb7ebe2cf6272fdf99722abc6fccc01acf2df",
    ),
    (None, True): (
        "dda143a12767de55029f0c1716e73f8cec8e0d38b6e60429b888d5394a460c48",
        "fae97af9d436c231599846671566768fd32bfbfbce4fc15723deea7c2c374531",
    ),
    (0, False): (
        "9b570c810294d1fee6eb2dd8b29ee679b5002e01fe3db1d026389ec82faf9ca3",
        "d2b9552f7f8c5cacbed235c9f00a2f04e6db2696dbd3257c2013c932419cbaa3",
    ),
    (0, True): (
        "5d0a768a2b527dd5fab6062898ef31e69274eb8b824a6d0ee5293eaef36ed8f6",
        "87e3127341d3628e192196934b65c152bba250ce711fba24d53eaf5b0c3c7041",
    ),
}


@pytest.mark.parametrize(("seed", "vision"), list(_PINNED))
def test_the_default_request_body_fingerprint_and_record_are_unchanged(
    tmp_path: Path, seed: int | None, vision: bool
) -> None:
    sent: list[bytes] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        sent.append(payload)
        return _ok()

    client = JsonCompletionClient(
        LLMConfig(api_key=SecretStr("k"), base_url="https://example.invalid", model="test-model"),
        cache_dir=tmp_path,
        max_live_calls=1,
        sender=sender,
        seed=seed,
    )
    if vision:
        result = client.complete_json(
            task="fingerprint-regression",
            prompt="fixed prompt",
            image_png=PNG,
            response_model=_Answer,
        )
    else:
        result = client.complete_text_json(
            task="fingerprint-regression", prompt="fixed prompt", response_model=_Answer
        )
    fingerprint, payload_digest = _PINNED[(seed, vision)]
    assert result.request_fingerprint == fingerprint
    assert hashlib.sha256(sent[0]).hexdigest() == payload_digest
    assert result.dropped_parameters == ()
    (record,) = _records(tmp_path).values()
    assert set(record["diagnostics"]) == {
        "endpoint_path",
        "request_bytes",
        "response_bytes",
        "elapsed_ms",
        "http_status",
        "exception_type",
        "finish_category",
        "attempt",
        "context_path",
        "context_warning",
    }


def test_a_supporting_endpoint_sees_one_request_with_the_temperature(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    scripted = endpoint(lambda body: (200, _ok()))
    assert _ask(_client(tmp_path)).parsed == _Answer(answer="observed")
    assert len(scripted.sent) == 1 and scripted.sent[0]["temperature"] == 0.0
    assert scripted.reads == [(200, 1_048_577)]
    assert unsupported_sampling_parameters(_config()) == ()


# ---- 2. the configured temperature ------------------------------------------------------------


def test_a_configured_temperature_is_sent_and_omit_sends_none_with_zero_probes(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    scripted = endpoint(_refuses("temperature"))
    warm = _ask(_client(tmp_path / "a", temperature=None))
    assert "temperature" not in scripted.sent[0] and len(scripted.sent) == 1
    assert warm.dropped_parameters == ()
    assert unsupported_sampling_parameters(_config()) == ()

    accepting = endpoint(lambda body: (200, _ok()))
    _ask(_client(tmp_path / "b", temperature=0.7))
    assert accepting.sent[0]["temperature"] == 0.7


def test_omit_and_an_automatic_drop_produce_the_same_cached_request(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    scripted = endpoint(_refuses("temperature"))
    dropped = _ask(_client(tmp_path))
    assert scripted.with_temperature() == 1 and len(scripted.sent) == 2
    forget_unsupported_sampling_parameters()
    omitted = _ask(_client(tmp_path, budget=0, temperature=None))
    assert omitted.cache_hit and omitted.request_fingerprint == dropped.request_fingerprint


# ---- 3. the transport reads only error.param / error.code of a 400 ---------------------------


@pytest.mark.parametrize("vision", [False, True])
def test_a_refused_temperature_is_dropped_and_the_request_resent_once(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint], vision: bool
) -> None:
    scripted = endpoint(_refuses("temperature"))
    client = _client(tmp_path)

    result = _ask(client, vision=vision)

    assert result.parsed == _Answer(answer="observed") and not result.cache_hit
    assert result.dropped_parameters == ("temperature",)
    first, second = scripted.sent
    assert first["temperature"] == 0.0 and "temperature" not in second
    assert {k: v for k, v in first.items() if k != "temperature"} == second
    assert (400, 4097) in scripted.reads
    assert client.live_call_count == 2
    assert client.dropped_parameters == ("temperature",)
    assert unsupported_sampling_parameters(_config()) == ("temperature",)
    refused = next(r for r in _records(tmp_path).values() if r["failure_code"])
    assert refused["failure_code"] == "provider_http_400"
    assert refused["diagnostics"]["provider_error_param"] == "temperature"
    assert refused["diagnostics"]["provider_error_code"] == "unsupported_value"
    assert refused["diagnostics"]["http_status"] == 400
    assert MARKER not in _files(tmp_path) and KEY not in _files(tmp_path)


def test_the_provider_message_never_reaches_an_exception_or_a_diagnostic(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    endpoint(lambda body: (400, AZURE_TEMPERATURE_400))  # refuses even without temperature
    with pytest.raises(JsonCompletionError) as raised:
        _ask(_client(tmp_path))
    assert raised.value.code == "provider_http_400"
    assert MARKER not in str(raised.value) and MARKER not in repr(raised.value.args)
    assert raised.value.diagnostics is not None
    assert MARKER not in raised.value.diagnostics.model_dump_json()
    assert MARKER not in _files(tmp_path)


def test_the_transport_error_keeps_its_old_message_and_carries_only_the_two_fields(
    endpoint: Callable[[Rule], _Endpoint],
) -> None:
    endpoint(lambda body: (400, AZURE_TEMPERATURE_400))
    with pytest.raises(ProviderRequestError) as raised:
        provider_module._send_once(
            "https://example.invalid/v1/chat/completions", api_key=KEY, payload=b"{}", timeout=1
        )
    error = raised.value
    assert str(error) == "Provider returned HTTP 400; no retry performed"
    assert (error.status, error.category) == (400, "http")
    assert (error.param, error.error_code) == ("temperature", "unsupported_value")
    assert MARKER not in repr(vars(error))


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json at all", id="not-json"),
        pytest.param(_error("temperature") + b" " * 4096, id="over-4kb"),
        pytest.param(_error(None), id="no-param"),
        pytest.param(json.dumps({"error": "temperature unsupported"}).encode(), id="error-is-text"),
        pytest.param(json.dumps(["temperature"]).encode(), id="not-an-object"),
        pytest.param(_error("temperature", code="invalid_value"), id="other-code"),
        pytest.param(_error("temperature", code=None), id="no-code"),
        pytest.param(_error("temp erature"), id="param-charset"),
        pytest.param(_error("t" * 65), id="param-length"),
        pytest.param(_error(["temperature"]), id="param-not-a-string"),
        pytest.param(b"[" * 4000, id="deeply-nested"),
        pytest.param(b"\xff\xfe\x00", id="not-utf8"),
    ],
)
def test_an_unusable_400_body_keeps_the_old_failure_and_sends_nothing_more(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint], body: bytes
) -> None:
    scripted = endpoint(lambda request: (400, body))
    client = _client(tmp_path)
    with pytest.raises(JsonCompletionError) as raised:
        _ask(client)
    assert raised.value.code == "provider_http_400"
    assert len(scripted.sent) == 1 and client.live_call_count == 1
    assert unsupported_sampling_parameters(_config()) == ()
    assert MARKER not in _files(tmp_path) and MARKER not in str(raised.value)
    # The negative cache is unchanged: a rerun replays the failure without a request.
    with pytest.raises(JsonCompletionError, match="provider_http_400"):
        _ask(_client(tmp_path))
    assert len(scripted.sent) == 1


@pytest.mark.parametrize(
    "param", ["response_format", "max_completion_tokens", "messages", "model", "stream"]
)
def test_a_refused_parameter_outside_the_allowlist_is_recorded_but_never_dropped(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint], param: str
) -> None:
    scripted = endpoint(lambda body: (400, _error(param, code="unsupported_parameter")))
    with pytest.raises(JsonCompletionError, match="provider_http_400") as raised:
        _ask(_client(tmp_path))
    assert len(scripted.sent) == 1
    assert raised.value.diagnostics is not None
    assert raised.value.diagnostics.provider_error_param == param
    assert raised.value.diagnostics.provider_error_code == "unsupported_parameter"


def test_a_refused_temperature_that_was_never_sent_is_not_retried(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    scripted = endpoint(lambda body: (400, AZURE_TEMPERATURE_400))
    with pytest.raises(JsonCompletionError, match="provider_http_400"):
        _ask(_client(tmp_path, temperature=None))
    assert len(scripted.sent) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_other_statuses_are_never_read_and_never_resent(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint], status: int
) -> None:
    scripted = endpoint(lambda body: (status, AZURE_TEMPERATURE_400))
    with pytest.raises(JsonCompletionError) as raised:
        _ask(_client(tmp_path))
    assert raised.value.code == f"provider_http_{status}"
    assert len(scripted.sent) == 1 and scripted.reads == []
    diagnostics = raised.value.diagnostics
    assert diagnostics is not None and diagnostics.provider_error_param is None
    (record,) = _records(tmp_path).values()
    assert "provider_error_param" not in record["diagnostics"]


def test_a_timeout_is_unchanged_and_never_resent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    class Connection:
        def __init__(self, host: str, *, timeout: float) -> None:
            pass

        def request(self, method: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
            sent.append(path)
            raise TimeoutError(MARKER)

        def close(self) -> None:
            pass

    monkeypatch.setattr(provider_module, "HTTPSConnection", Connection)
    with pytest.raises(JsonCompletionError, match="provider_timeout"):
        _ask(_client(tmp_path))
    assert sent == ["/v1/chat/completions"]
    assert MARKER not in _files(tmp_path)


# ---- 4. seed, and more than one refused parameter --------------------------------------------


def test_a_refused_seed_is_dropped_and_the_temperature_kept(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    scripted = endpoint(_refuses("seed"))
    result = _ask(_client(tmp_path, seed=0))
    assert result.dropped_parameters == ("seed",)
    assert scripted.sent[0]["seed"] == 0 and "seed" not in scripted.sent[1]
    assert scripted.sent[1]["temperature"] == 0.0


def test_two_refused_parameters_cost_one_probe_each_and_no_more(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    scripted = endpoint(_refuses("temperature", "seed"))
    client = _client(tmp_path, seed=0)
    first = _ask(client, "one")
    assert first.dropped_parameters == ("temperature", "seed")
    assert len(scripted.sent) == 3 and client.live_call_count == 3
    assert unsupported_sampling_parameters(_config()) == ("seed", "temperature")
    second = _ask(client, "two")
    # Remembered drops are applied in name order, without a request.
    assert second.dropped_parameters == ("seed", "temperature") and len(scripted.sent) == 4
    assert "seed" not in scripted.sent[-1] and "temperature" not in scripted.sent[-1]


def test_an_endpoint_that_refuses_whatever_is_left_stops_after_the_allowlist(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    def rule(body: dict[str, Any]) -> tuple[int, bytes]:
        left = [name for name in ("temperature", "seed") if name in body]
        return 400, _error(left[0] if left else "temperature")

    scripted = endpoint(rule)
    with pytest.raises(JsonCompletionError, match="provider_http_400"):
        _ask(_client(tmp_path, seed=0))
    assert len(scripted.sent) == 3 <= len(DEGRADABLE_SAMPLING_PARAMETERS) + 1


# ---- 5. in-process memory ---------------------------------------------------------------------


def test_later_calls_in_the_same_run_skip_the_refused_parameter_without_a_probe(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    scripted = endpoint(_refuses("temperature"))
    client = _client(tmp_path)
    for prompt in ("one", "two", "three"):
        assert _ask(client, prompt).dropped_parameters == ("temperature",)
    assert scripted.with_temperature() == 1 and len(scripted.sent) == 4
    # Another client of the same endpoint and model (another stage, another PDF) shares it.
    other = _client(tmp_path / "other-pdf")
    _ask(other, "four")
    _ask(other, "five", vision=True)
    assert scripted.with_temperature() == 1 and len(scripted.sent) == 6
    assert other.live_call_count == 2


def test_another_model_or_endpoint_is_probed_on_its_own(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    scripted = endpoint(_refuses("temperature"))
    _ask(_client(tmp_path / "a"))
    _ask(_client(tmp_path / "b", model="other-model"))
    _ask(_client(tmp_path / "c", base_url="https://elsewhere.invalid"))
    assert scripted.with_temperature() == 3
    assert unsupported_sampling_parameters(_config(model="other-model")) == ("temperature",)
    assert unsupported_sampling_parameters(_config(model="never-used")) == ()


def test_forgetting_clears_the_memory(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    endpoint(_refuses("temperature"))
    _ask(_client(tmp_path))
    forget_unsupported_sampling_parameters()
    assert unsupported_sampling_parameters(_config()) == ()


# ---- 6. persisted: a new process needs no memory and no request ------------------------------


def test_a_rerun_in_a_new_process_replays_every_call_with_zero_requests(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    scripted = endpoint(_refuses("temperature"))
    client = _client(tmp_path)
    first = [_ask(client, prompt) for prompt in ("probed", "remembered", "also-remembered")]
    assert len(scripted.sent) == 4

    forget_unsupported_sampling_parameters()  # a new kernel: no memory
    forbidden = endpoint(lambda body: pytest.fail("a rerun must not reach the endpoint"))
    # Ask in another order: the remembered calls must not need the probed one first.
    for prompt, before in zip(
        ("also-remembered", "remembered", "probed"), reversed(first), strict=True
    ):
        again = _ask(_client(tmp_path, budget=0), prompt)
        assert again.cache_hit and again.parsed == before.parsed
        assert again.request_fingerprint == before.request_fingerprint
        assert again.dropped_parameters == ("temperature",)
    assert forbidden.sent == []
    assert unsupported_sampling_parameters(_config()) == ("temperature",)


def test_an_existing_success_with_the_temperature_keeps_replaying(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    endpoint(lambda body: (200, _ok("cached-before")))
    cached = _ask(_client(tmp_path), "old")
    refusing = endpoint(_refuses("temperature"))
    _ask(_client(tmp_path), "new")  # learns that the endpoint now refuses it
    again = _ask(_client(tmp_path, budget=0), "old")
    assert again.cache_hit and again.parsed == _Answer(answer="cached-before")
    assert again.request_fingerprint == cached.request_fingerprint
    assert again.dropped_parameters == ()
    assert refusing.with_temperature() == 1


def test_a_cache_only_replay_follows_a_recorded_refusal(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    endpoint(_refuses("temperature"))
    _ask(_client(tmp_path))
    forget_unsupported_sampling_parameters()
    replayed = _client(tmp_path, budget=5).complete_text_json(
        task="t", prompt="read", response_model=_Answer, cache_only=True
    )
    assert replayed.cache_hit and replayed.dropped_parameters == ("temperature",)


# ---- 7. failure records written before the fallback existed ---------------------------------


def _legacy_failure(cache: Path, prompt: str) -> str:
    """Write what the old client left for a 400: a param-less record plus its claim."""

    def capture(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        raise ProviderRequestError("captured")

    probe = JsonCompletionClient(
        _config(), cache_dir=cache / "capture", max_live_calls=1, sender=capture
    )
    with pytest.raises(JsonCompletionError) as raised:
        probe.complete_text_json(task="t", prompt=prompt, response_model=_Answer)
    fingerprint = raised.value.request_fingerprint
    requests = cache / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    (requests / f"{fingerprint}.json").write_text(
        json.dumps(
            {
                "request_fingerprint": fingerprint,
                "response_digest": None,
                "failure_code": "provider_http_400",
                "diagnostics": {
                    "endpoint_path": "/v1/chat/completions",
                    "request_bytes": 600,
                    "response_bytes": None,
                    "elapsed_ms": 812,
                    "http_status": 400,
                    "exception_type": None,
                    "finish_category": "transport_error",
                    "attempt": 1,
                    "context_path": f"contexts/{fingerprint}.json",
                    "context_warning": None,
                },
            },
            separators=(",", ":"),
        )
    )
    (requests / f"{fingerprint}.json.claim").write_text(fingerprint)
    return fingerprint


def test_old_param_less_400_records_are_probed_once_then_bypassed_without_requests(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    cache = tmp_path / "model-cache"
    old = [_legacy_failure(cache, prompt) for prompt in ("a", "b", "c")]
    scripted = endpoint(_refuses("temperature"))
    client = _client(cache, budget=10)

    results = [_ask(client, prompt) for prompt in ("a", "b", "c")]

    assert all(result.parsed == _Answer(answer="observed") for result in results)
    # One re-probe of the first old record (it learns why), then the drop for all three.
    assert scripted.with_temperature() == 1 and len(scripted.sent) == 4
    assert client.live_call_count == 4
    records = _records(cache)
    assert records[f"{old[0]}.retry-1.json"]["diagnostics"]["provider_error_param"] == "temperature"
    for fingerprint in old[1:]:
        redirect = records[f"{fingerprint}.retry-1.json"]
        assert redirect["failure_code"] == "sampling_parameter_unsupported"
        assert redirect["diagnostics"]["http_status"] is None
    # The old records and their claims are left exactly as they were.
    assert all((cache / "requests" / f"{fingerprint}.json.claim").exists() for fingerprint in old)

    forget_unsupported_sampling_parameters()
    forbidden = endpoint(lambda body: pytest.fail("a rerun must not reach the endpoint"))
    for prompt in ("c", "a", "b"):
        assert _ask(_client(cache, budget=0), prompt).cache_hit
    assert forbidden.sent == []


def test_an_old_record_is_bypassed_without_a_probe_once_the_memory_knows(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    cache = tmp_path / "model-cache"
    _legacy_failure(cache, "old")
    scripted = endpoint(_refuses("temperature"))
    _ask(_client(tmp_path / "elsewhere"), "learn")
    assert scripted.with_temperature() == 1

    result = _ask(_client(cache), "old")
    assert result.dropped_parameters == ("temperature",)
    assert scripted.with_temperature() == 1 and len(scripted.sent) == 3


def test_an_old_record_stays_a_failure_where_nothing_could_be_dropped(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    cache = tmp_path / "model-cache"
    fingerprint = _legacy_failure(cache, "old")
    scripted = endpoint(lambda body: pytest.fail("nothing may be sent"))
    # Cache-only replay never probes; the old failure is replayed as before.
    with pytest.raises(JsonCompletionError, match="provider_http_400") as raised:
        _client(cache).complete_text_json(
            task="t", prompt="old", response_model=_Answer, cache_only=True
        )
    assert raised.value.request_fingerprint == fingerprint
    assert scripted.sent == []


def test_a_new_param_less_400_record_is_not_re_probed(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    scripted = endpoint(lambda body: (400, _error(None)))
    with pytest.raises(JsonCompletionError, match="provider_http_400"):
        _ask(_client(tmp_path))
    with pytest.raises(JsonCompletionError, match="provider_http_400"):
        _ask(_client(tmp_path))
    assert len(scripted.sent) == 1


# ---- 8. budget and claims ---------------------------------------------------------------------


def test_the_probe_and_the_resend_each_spend_one_live_call(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    scripted = endpoint(_refuses("temperature"))
    starved = _client(tmp_path, budget=1)
    with pytest.raises(JsonCompletionError, match="call_budget_exhausted"):
        _ask(starved)
    assert starved.live_call_count == 1 and len(scripted.sent) == 1

    forget_unsupported_sampling_parameters()
    resumed = _client(tmp_path, budget=1)
    assert _ask(resumed).dropped_parameters == ("temperature",)
    assert resumed.live_call_count == 1 and scripted.with_temperature() == 1


def test_a_claim_left_by_a_killed_kernel_is_bypassed_once_the_memory_knows(
    tmp_path: Path, endpoint: Callable[[Rule], _Endpoint]
) -> None:
    def killed(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        raise RuntimeError("kernel killed mid-call")

    stuck = JsonCompletionClient(_config(), cache_dir=tmp_path, max_live_calls=1, sender=killed)
    with pytest.raises(RuntimeError):
        stuck.complete_text_json(task="t", prompt="stuck", response_model=_Answer)
    (claim,) = (tmp_path / "requests").glob("*.claim")

    scripted = endpoint(_refuses("temperature"))
    _ask(_client(tmp_path / "elsewhere"), "learn")
    result = _ask(_client(tmp_path), "stuck")
    assert result.dropped_parameters == ("temperature",)
    assert scripted.with_temperature() == 1
    # No record is written under a claim another process may still hold.
    assert not (tmp_path / "requests" / claim.name.removesuffix(".claim")).exists()
