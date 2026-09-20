"""Bounded vision/JSON calls are explicit and never part of ordinary networking."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

import enterprise_pdf_rag.adapters.providers as provider_module
from enterprise_pdf_rag.adapters.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
)
from enterprise_pdf_rag.adapters.providers import LLMConfig, ProviderRequestError

PNG = b"\x89PNG\r\n\x1a\nfixture-bytes"
KEY = "test-secret-never-written"


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    answer: str


class _BoundsAnswer(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    bbox: tuple[float, float, float, float]


def test_two_clients_cannot_both_issue_the_one_explicit_retry(tmp_path: Path) -> None:
    def fail_once(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        raise ProviderRequestError("Provider timed out", category="timeout")

    original = JsonCompletionClient(
        _config(), cache_dir=tmp_path, max_live_calls=1, sender=fail_once
    )
    with pytest.raises(JsonCompletionError, match="provider_timeout"):
        original.complete_json(
            task="parallel", prompt="same", image_png=PNG, response_model=_Answer
        )
    (record,) = tuple((tmp_path / "requests").glob("*.json"))
    original_bytes = record.read_bytes()
    entered, release = Event(), Event()
    calls: list[str] = []

    def controlled(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        calls.append("first")
        entered.set()
        assert release.wait(5), "Test coordinator must release the first request"
        return _response()

    def forbidden_duplicate(
        url: str, *, api_key: str, payload: bytes, timeout: float
    ) -> bytes:
        calls.append("duplicate")
        return _response()

    first = JsonCompletionClient(
        _config(),
        cache_dir=tmp_path,
        max_live_calls=1,
        retry_failed=True,
        sender=controlled,
    )
    second = JsonCompletionClient(
        _config(),
        cache_dir=tmp_path,
        max_live_calls=1,
        retry_failed=True,
        sender=forbidden_duplicate,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            first.complete_json,
            task="parallel",
            prompt="same",
            image_png=PNG,
            response_model=_Answer,
        )
        assert entered.wait(5)
        try:
            with pytest.raises(
                JsonCompletionError, match="request_in_progress_or_uncertain"
            ):
                second.complete_json(
                    task="parallel",
                    prompt="same",
                    image_png=PNG,
                    response_model=_Answer,
                )
        finally:
            release.set()
        completed = pending.result(timeout=5)
    assert calls == ["first"]
    assert completed.diagnostics is not None and completed.diagnostics.attempt == 2
    assert record.read_bytes() == original_bytes
    cached = second.complete_json(
        task="parallel", prompt="same", image_png=PNG, response_model=_Answer
    )
    assert cached.cache_hit and cached.parsed == completed.parsed


def test_uncertain_inflight_claim_survives_and_cannot_be_silently_retried(
    tmp_path: Path,
) -> None:
    calls = 0

    def interrupted(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated worker interruption after transport began")

    first = JsonCompletionClient(
        _config(), cache_dir=tmp_path, max_live_calls=1, sender=interrupted
    )
    with pytest.raises(RuntimeError, match="simulated worker interruption"):
        first.complete_json(
            task="uncertain", prompt="same", image_png=PNG, response_model=_Answer
        )
    assert not tuple((tmp_path / "requests").glob("*.json"))
    (claim,) = tuple((tmp_path / "requests").glob("*.claim"))
    content = claim.read_bytes()
    assert KEY.encode() not in content
    second = JsonCompletionClient(
        _config(),
        cache_dir=tmp_path,
        max_live_calls=1,
        retry_failed=True,
        sender=interrupted,
    )
    with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
        second.complete_json(
            task="uncertain", prompt="same", image_png=PNG, response_model=_Answer
        )
    assert calls == 1
    assert claim.read_bytes() == content


def _config() -> LLMConfig:
    return LLMConfig(
        api_key=SecretStr(KEY), base_url="https://example.invalid", model="test-model"
    )


def _response(content: str = '{"answer":"observed"}', finish: str = "stop") -> bytes:
    return json.dumps(
        {
            "model": "reported-model",
            "choices": [{"finish_reason": finish, "message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    ).encode()


def test_strict_json_call_uses_one_image_and_reuses_persistent_cache(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        assert url == "https://example.invalid/v1/chat/completions"
        assert api_key == KEY and timeout == 45.0
        calls.append(json.loads(payload))
        return _response()

    first = JsonCompletionClient(
        _config(), cache_dir=tmp_path, max_live_calls=1, sender=sender
    ).complete_json(
        task="chart-pilot-v1",
        prompt="Read the source image.",
        image_png=PNG,
        response_model=_Answer,
    )
    second = JsonCompletionClient(
        _config(), cache_dir=tmp_path, max_live_calls=0, sender=sender
    ).complete_json(
        task="chart-pilot-v1",
        prompt="Read the source image.",
        image_png=PNG,
        response_model=_Answer,
    )
    assert first.parsed.answer == "observed" and second.parsed == first.parsed
    assert first.cache_hit is False and second.cache_hit is True
    assert first.request_fingerprint == second.request_fingerprint
    assert first.output_digest == second.output_digest
    assert first.reported_model == "reported-model"
    assert first.input_tokens == 10 and first.output_tokens == 5
    assert len(calls) == 1
    serialized = json.dumps(calls[0])
    assert '"image_url"' in serialized and "data:image/png;base64," in serialized
    assert '"json_schema"' in serialized
    assert all(
        KEY.encode() not in path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


def test_live_call_count_excludes_cached_replay(tmp_path: Path) -> None:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        return _response()

    client = JsonCompletionClient(
        _config(), cache_dir=tmp_path, max_live_calls=2, sender=sender
    )
    assert client.live_call_count == 0
    for expected_cache in (False, True):
        result = client.complete_json(
            task="count", prompt="same", image_png=PNG, response_model=_Answer
        )
        assert result.cache_hit is expected_cache
        assert client.live_call_count == 1
    fresh = JsonCompletionClient(
        _config(), cache_dir=tmp_path, max_live_calls=0, sender=sender
    )
    fresh.complete_json(
        task="count", prompt="same", image_png=PNG, response_model=_Answer
    )
    assert fresh.live_call_count == 0


def test_cache_only_never_uses_available_live_budget(tmp_path: Path) -> None:
    def forbidden(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        raise AssertionError("An explicit cache read cannot dispatch transport")

    client = JsonCompletionClient(
        _config(),
        cache_dir=tmp_path,
        max_live_calls=1,
        sender=forbidden,
        retry_failed=True,
    )
    with pytest.raises(JsonCompletionError, match="cache_miss"):
        client.complete_json(
            task="absent",
            prompt="same",
            image_png=PNG,
            response_model=_Answer,
            cache_only=True,
        )
    assert client.live_call_count == 0
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("content", "finish", "code"),
    [
        ('{"answer":"cut', "length", "truncated_response"),
        ('```json\n{"answer":"bad"}\n```', "stop", "invalid_model_json"),
        ('{"answer":42}', "stop", "invalid_model_json"),
        ('{"answer":"ok","verified":true}', "stop", "invalid_model_json"),
    ],
)
def test_failed_model_result_is_cached_without_retry(
    tmp_path: Path, content: str, finish: str, code: str
) -> None:
    calls = 0

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        return _response(content, finish)

    client = JsonCompletionClient(
        _config(), cache_dir=tmp_path, max_live_calls=2, sender=sender
    )
    for _ in range(2):
        with pytest.raises(JsonCompletionError) as raised:
            client.complete_json(
                task="bounded", prompt="Read it", image_png=PNG, response_model=_Answer
            )
        assert raised.value.code == code
        assert KEY not in str(raised.value)
    assert calls == 1


def test_input_and_call_budgets_fail_before_transport(tmp_path: Path) -> None:
    def forbidden(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        pytest.fail("No request is authorized")

    client = JsonCompletionClient(
        _config(), cache_dir=tmp_path, max_live_calls=0, sender=forbidden
    )
    with pytest.raises(JsonCompletionError, match="call_budget_exhausted"):
        client.complete_json(
            task="pilot", prompt="Read", image_png=PNG, response_model=_Answer
        )
    with pytest.raises(JsonCompletionError, match="input_budget_exceeded"):
        client.complete_json(
            task="pilot", prompt="x" * 24_001, image_png=PNG, response_model=_Answer
        )


def test_provider_fingerprint_excludes_credentials_but_changes_with_endpoint(
    tmp_path: Path,
) -> None:
    first = JsonCompletionClient(_config(), cache_dir=tmp_path, max_live_calls=0)
    rotated = JsonCompletionClient(
        _config().model_copy(update={"api_key": SecretStr("rotated-secret")}),
        cache_dir=tmp_path,
        max_live_calls=0,
    )
    other = JsonCompletionClient(
        _config().model_copy(update={"base_url": "https://other.invalid"}),
        cache_dir=tmp_path,
        max_live_calls=0,
    )
    assert first.fingerprint == rotated.fingerprint
    assert first.fingerprint != other.fingerprint
    assert KEY not in first.fingerprint and "example.invalid" not in first.fingerprint


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Provider returned HTTP 400; no retry performed", "provider_http_400"),
        (f"secret: {KEY}; HTTP 400", "provider_request_failed"),
    ],
)
def test_provider_failure_retains_only_a_trusted_status_and_never_error_text(
    tmp_path: Path, message: str, expected: str
) -> None:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        raise ProviderRequestError(message)

    client = JsonCompletionClient(
        _config(), cache_dir=tmp_path, max_live_calls=1, sender=sender
    )
    with pytest.raises(JsonCompletionError) as raised:
        client.complete_json(
            task="diagnostic", prompt="read", image_png=PNG, response_model=_Answer
        )
    assert raised.value.code == expected
    assert KEY not in str(raised.value)
    assert all(KEY not in path.read_text() for path in tmp_path.rglob("*.json"))


def test_model_schema_uses_homogeneous_arrays_and_keeps_local_tuple_length_validation(
    tmp_path: Path,
) -> None:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        request = json.loads(payload)
        bbox = request["response_format"]["json_schema"]["schema"]["properties"]["bbox"]
        assert bbox["items"] == {"type": "number"}
        assert bbox["minItems"] == bbox["maxItems"] == 4
        assert "prefixItems" not in bbox
        return _response('{"bbox":[1.0,2.0,3.0,4.0]}')

    client = JsonCompletionClient(
        _config(), cache_dir=tmp_path, max_live_calls=1, sender=sender
    )
    result = client.complete_json(
        task="bounds", prompt="read", image_png=PNG, response_model=_BoundsAnswer
    )
    assert result.parsed.bbox == (1.0, 2.0, 3.0, 4.0)
    with pytest.raises(ValueError):
        _BoundsAnswer.model_validate_json('{"bbox":[1.0,2.0,3.0]}')


@pytest.mark.parametrize(
    ("status", "timeout", "code"),
    [
        (401, False, "provider_http_401"),
        (429, False, "provider_http_429"),
        (400, False, "provider_http_400"),
        (200, True, "provider_timeout"),
    ],
)
def test_real_transport_classifies_http_and_timeout_without_response_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    timeout: bool,
    code: str,
) -> None:
    requests = []

    class Response:
        def __init__(self) -> None:
            self.status = status

        def read(self, amount: int) -> bytes:
            raise AssertionError("Error response bodies must not be retained")

    class Connection:
        def __init__(self, host: str, *, timeout: float) -> None:
            pass

        def request(
            self, method: str, path: str, *, body: bytes, headers: dict[str, str]
        ) -> None:
            requests.append(path)
            if timeout:
                raise TimeoutError(KEY)

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(provider_module, "HTTPSConnection", Connection)
    client = JsonCompletionClient(_config(), cache_dir=tmp_path, max_live_calls=1)
    for _ in range(2):
        with pytest.raises(JsonCompletionError) as raised:
            client.complete_json(
                task="safe-cause", prompt="read", image_png=PNG, response_model=_Answer
            )
        assert raised.value.code == code
        diagnostic = raised.value.diagnostics
        assert diagnostic is not None
        assert diagnostic.endpoint_path == "/v1/chat/completions"
        assert diagnostic.request_bytes > 0 and diagnostic.elapsed_ms >= 0
        assert diagnostic.http_status == (None if timeout else status)
        assert diagnostic.exception_type == ("TimeoutError" if timeout else None)
        assert KEY not in diagnostic.model_dump_json()
    assert requests == ["/v1/chat/completions"]


@pytest.mark.parametrize("retry_succeeds", [True, False])
def test_explicit_failed_retry_preserves_first_attempt_and_is_limited_to_one(
    tmp_path: Path, retry_succeeds: bool
) -> None:
    calls: list[bytes] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        calls.append(payload)
        if len(calls) == 1 or not retry_succeeds:
            raise ProviderRequestError(
                "timeout", category="timeout", exception_type="TimeoutError"
            )
        assert timeout == 180.0
        return _response()

    def invoke(client: JsonCompletionClient) -> str:
        try:
            return client.complete_json(
                task="slow-layout",
                prompt="unchanged",
                image_png=PNG,
                response_model=_Answer,
            ).parsed.answer
        except JsonCompletionError as error:
            return error.code

    assert (
        invoke(
            JsonCompletionClient(
                _config(), cache_dir=tmp_path, max_live_calls=1, sender=sender
            )
        )
        == "provider_timeout"
    )
    (original,) = tuple((tmp_path / "requests").glob("*.json"))
    original_bytes = original.read_bytes()
    assert (
        invoke(
            JsonCompletionClient(
                _config(),
                cache_dir=tmp_path,
                max_live_calls=0,
                retry_failed=True,
                timeout=180.0,
                sender=sender,
            )
        )
        == "call_budget_exhausted"
    )
    expected = "observed" if retry_succeeds else "provider_timeout"
    assert (
        invoke(
            JsonCompletionClient(
                _config(),
                cache_dir=tmp_path,
                max_live_calls=1,
                retry_failed=True,
                timeout=180.0,
                sender=sender,
            )
        )
        == expected
    )
    assert (
        invoke(
            JsonCompletionClient(
                _config(),
                cache_dir=tmp_path,
                max_live_calls=1,
                retry_failed=True,
                timeout=180.0,
                sender=sender,
            )
        )
        == expected
    )
    assert original.read_bytes() == original_bytes
    assert original.with_name(original.stem + ".retry-1.json").exists()
    assert len(calls) == 2 and calls[0] == calls[1]
