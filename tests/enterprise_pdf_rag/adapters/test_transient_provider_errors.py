"""Rate limits and server-side errors are retried, never cached as a permanent failure (ADR 0034).

Every wait goes through ``transient._sleep`` (stubbed: nothing here really sleeps) on the clock
``transient._clock``; ``transient._random`` is the jitter source.
"""

import json
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

import ragspine.common.evidence.providers.providers as provider_module
from ragspine.common.evidence.providers import json_completion, transient
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
    JsonCompletionResult,
)
from ragspine.common.evidence.providers.local_models import LocalEmbeddingAdapter
from ragspine.common.evidence.providers.providers import (
    LLMConfig,
    ProviderRequestError,
    load_local_model_config,
    retry_after_seconds,
)
from ragspine.common.evidence.providers.transient import (
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    TRANSIENT_FAILURE_CODES,
    TRANSIENT_HTTP_STATUSES,
    TRANSIENT_MAX_RETRIES,
    is_transient,
)

PNG = b"\x89PNG\r\n\x1a\nfixture-bytes"
KEY = "transient-test-key"


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    answer: str


def _config(**override: object) -> LLMConfig:
    fields: dict[str, object] = {
        "api_key": SecretStr(KEY),
        "base_url": "https://example.invalid",
        "model": "m",
    }
    fields.update(override)
    return LLMConfig(**fields)  # type: ignore[arg-type]


def _response(answer: str = "ok") -> bytes:
    return json.dumps(
        {
            "choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps({"answer": answer})}}
            ]
        }
    ).encode()


def _http(status: int, retry_after: float | None = None) -> ProviderRequestError:
    return ProviderRequestError(
        f"Provider returned HTTP {status}; no retry performed",
        status=status,
        category="http",
        retry_after=retry_after,
    )


Outcome = ProviderRequestError | OSError | bytes


class _Script:
    """A sender answering from a list of outcomes (the last one repeats)."""

    def __init__(self, *outcomes: Outcome, on_send: Callable[[], None] | None = None) -> None:
        self.outcomes = list(outcomes)
        self.sent = 0
        self._on_send = on_send

    def __call__(self, url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        if self._on_send is not None:
            self._on_send()
        outcome = self.outcomes[min(self.sent, len(self.outcomes) - 1)]
        self.sent += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _client(cache: Path, sender: Callable[..., bytes], *, budget: int = 10) -> JsonCompletionClient:
    return JsonCompletionClient(_config(), cache_dir=cache, max_live_calls=budget, sender=sender)


def _ask(
    client: JsonCompletionClient, *, cache_only: bool = False
) -> JsonCompletionResult[_Answer]:
    return client.complete_json(
        task="transient",
        prompt="same",
        image_png=PNG,
        response_model=_Answer,
        cache_only=cache_only,
    )


def _records(cache: Path) -> list[Path]:
    return sorted((cache / "requests").glob("*.json"))


class _Waits:
    """A fake clock that only moves when something sleeps; every sleep is recorded."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []
        self.during: list[Callable[[], None]] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.during:
            self.during.pop(0)()
        self.now += seconds


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Waits]:
    fake = _Waits()
    monkeypatch.setattr(transient, "_sleep", fake.sleep)
    monkeypatch.setattr(transient, "_clock", lambda: fake.now)
    yield fake


# ---- classification ----------------------------------------------------------------------


def test_the_transient_statuses_are_named_and_everything_else_is_permanent() -> None:
    assert frozenset({408, 429, 500, 502, 503, 504}) == TRANSIENT_HTTP_STATUSES
    for status in TRANSIENT_HTTP_STATUSES:
        assert is_transient(_http(status))
    for status in (400, 401, 403, 404, 409, 413, 422, 501):
        assert not is_transient(_http(status))
    assert is_transient(ProviderRequestError("t", category="timeout"))
    assert is_transient(ProviderRequestError("c", category="connection"))
    assert not is_transient(ProviderRequestError("big", category="response_limit"))
    assert not is_transient(ProviderRequestError("schema"))  # invalid_response
    assert is_transient(TimeoutError("raw"))
    assert is_transient(ConnectionRefusedError("raw"))
    assert (
        frozenset(
            {
                "provider_http_408",
                "provider_http_429",
                "provider_http_500",
                "provider_http_502",
                "provider_http_503",
                "provider_http_504",
                "provider_timeout",
                "provider_connection",
            }
        )
        == TRANSIENT_FAILURE_CODES
    )


def test_retry_after_is_read_as_milliseconds_seconds_or_a_date() -> None:
    def headers(**values: str) -> Callable[[str], str | None]:
        return lambda name: values.get(name.replace("-", "_"))

    assert retry_after_seconds(headers(retry_after="7")) == 7.0
    assert retry_after_seconds(headers(retry_after_ms="1500", retry_after="9")) == 1.5
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    parsed = retry_after_seconds(headers(retry_after=later))
    assert parsed is not None and 110 <= parsed <= 121
    for junk in ("", "soon", "-3", "1e9x"):
        assert retry_after_seconds(headers(retry_after=junk)) is None
    assert retry_after_seconds(headers(retry_after_ms="-5")) is None
    assert retry_after_seconds(headers()) is None


def test_the_backoff_doubles_from_one_second_is_capped_and_jittered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transient, "_random", lambda: 1.0)
    assert [transient.retry_delay(n, None) for n in range(7)] == [1, 2, 4, 8, 16, 30, 30]
    monkeypatch.setattr(transient, "_random", lambda: 0.0)
    assert [transient.retry_delay(n, None) for n in range(3)] == [0.5, 1, 2]
    # Retry-After wins over the backoff, capped, with up to one base delay of spread.
    assert transient.retry_delay(0, 7.0) == 7.0
    monkeypatch.setattr(transient, "_random", lambda: 0.5)
    assert transient.retry_delay(2, 120.0) == RETRY_MAX_DELAY + 0.5 * RETRY_BASE_DELAY


def test_the_real_transport_reads_retry_after_and_never_the_body_of_a_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waits: _Waits
) -> None:
    class Response:
        status = 429

        def read(self, amount: int) -> bytes:
            raise AssertionError("a 429 body must not be read")

        def getheader(self, name: str) -> str | None:
            return {"retry-after": "4"}.get(name.lower())

    class Connection:
        def __init__(self, host: str, *, timeout: float) -> None:
            pass

        def request(self, method: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(provider_module, "HTTPSConnection", Connection)
    with pytest.raises(ProviderRequestError) as raised:
        provider_module._send_once(
            "https://example.invalid/v1/x", api_key=KEY, payload=b"{}", timeout=1
        )
    assert (raised.value.status, raised.value.retry_after) == (429, 4.0)

    client = JsonCompletionClient(_config(), cache_dir=tmp_path, max_live_calls=2)
    with pytest.raises(JsonCompletionError, match="provider_http_429"):
        _ask(client)
    assert client.live_call_count == 2 and client.retry_count == 1
    assert 4.0 <= waits.slept[0] < 4.0 + RETRY_BASE_DELAY


# ---- model calls --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first",
    [
        pytest.param(_http(429), id="429"),
        pytest.param(_http(408), id="408"),
        pytest.param(_http(500), id="500"),
        pytest.param(_http(502), id="502"),
        pytest.param(_http(503), id="503"),
        pytest.param(_http(504), id="504"),
        pytest.param(ProviderRequestError("t", category="timeout"), id="timeout"),
        pytest.param(ProviderRequestError("c", category="connection"), id="connection"),
        pytest.param(TimeoutError("raw"), id="raw-timeout"),
    ],
)
def test_a_transient_failure_is_retried_within_the_call_and_each_retry_is_a_live_call(
    tmp_path: Path, waits: _Waits, first: Outcome
) -> None:
    script = _Script(first, _response())
    client = _client(tmp_path, script, budget=5)
    result = _ask(client)
    assert result.parsed.answer == "ok" and not result.cache_hit
    assert script.sent == 2
    assert (client.live_call_count, client.retry_count, client.transient_failure_count) == (2, 1, 0)
    assert len(waits.slept) == 1 and 0.5 <= waits.slept[0] <= 1.0
    (record,) = _records(tmp_path)
    stored = json.loads(record.read_bytes())
    assert stored["failure_code"] is None
    # A success after a retry is recorded exactly as a first-attempt success.
    assert set(stored["diagnostics"]) == {
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
    # The rerun replays it.
    assert _ask(_client(tmp_path, _Script(_http(500)))).cache_hit


def test_a_success_record_after_retries_is_byte_identical_to_a_first_attempt_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(json_completion, "monotonic", lambda: 5.0)  # elapsed_ms = 0 both times
    _ask(_client(tmp_path / "direct", _Script(_response())))
    _ask(_client(tmp_path / "retried", _Script(_http(429), _http(503), _response())))
    (direct,) = _records(tmp_path / "direct")
    (retried,) = _records(tmp_path / "retried")
    assert direct.name == retried.name
    assert direct.read_bytes() == retried.read_bytes()


def test_retry_after_is_honoured_and_capped(tmp_path: Path, waits: _Waits) -> None:
    script = _Script(_http(429, retry_after=7.0), _http(503, retry_after=600.0), _response())
    client = _client(tmp_path, script)
    _ask(client)
    assert client.retry_count == 2
    first, second = waits.slept
    assert 7.0 <= first < 7.0 + RETRY_BASE_DELAY
    assert RETRY_MAX_DELAY <= second <= RETRY_MAX_DELAY + RETRY_BASE_DELAY


def test_exhausted_retries_leave_no_record_and_the_next_run_calls_again(
    tmp_path: Path, waits: _Waits
) -> None:
    down = _Script(_http(503))
    client = _client(tmp_path, down)
    with pytest.raises(JsonCompletionError) as raised:
        _ask(client)
    assert raised.value.code == "provider_http_503"
    assert raised.value.diagnostics is not None and raised.value.diagnostics.http_status == 503
    assert down.sent == 1 + TRANSIENT_MAX_RETRIES
    assert (client.live_call_count, client.retry_count, client.transient_failure_count) == (4, 3, 1)
    # Backoff without Retry-After: ~1, ~2, ~4 seconds (equal jitter: half to all of it).
    assert [0.5 <= waits.slept[0] <= 1, 1 <= waits.slept[1] <= 2, 2 <= waits.slept[2] <= 4] == [
        True
    ] * 3
    assert not _records(tmp_path)
    assert not tuple((tmp_path / "requests").glob("*.claim*"))
    # cache_only sees nothing cached (not a replayed failure).
    with pytest.raises(JsonCompletionError, match="cache_miss"):
        _ask(_client(tmp_path, down), cache_only=True)

    healthy = _Script(_response())
    again = _client(tmp_path, healthy)
    assert _ask(again).parsed.answer == "ok"
    assert healthy.sent == 1 and again.retry_count == 0
    assert _ask(_client(tmp_path, healthy)).cache_hit and healthy.sent == 1


def test_retries_stop_when_the_budget_runs_out(tmp_path: Path) -> None:
    down = _Script(_http(429))
    client = _client(tmp_path, down, budget=2)
    with pytest.raises(JsonCompletionError, match="provider_http_429"):
        _ask(client)
    assert down.sent == 2
    assert (client.live_call_count, client.retry_count, client.transient_failure_count) == (2, 1, 1)
    assert not _records(tmp_path)
    with pytest.raises(JsonCompletionError, match="call_budget_exhausted"):
        _ask(client)
    assert down.sent == 2


def test_the_claim_is_held_through_every_retry_and_released_at_the_end(tmp_path: Path) -> None:
    seen: list[int] = []

    def claims() -> None:
        seen.append(len(tuple((tmp_path / "requests").glob("*.json.claim"))))

    script = _Script(_http(429), _http(502), _response(), on_send=claims)
    _ask(_client(tmp_path, script))
    assert seen == [1, 1, 1]
    assert not tuple((tmp_path / "requests").glob("*.claim*"))

    failing = tmp_path / "failing"
    seen.clear()

    def claims_failing() -> None:
        seen.append(len(tuple((failing / "requests").glob("*.json.claim"))))

    with pytest.raises(JsonCompletionError):
        _ask(_client(failing, _Script(_http(503), on_send=claims_failing)))
    assert seen == [1] * (1 + TRANSIENT_MAX_RETRIES)
    assert not tuple((failing / "requests").glob("*.claim*"))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_a_permanent_failure_is_sent_once_recorded_and_replayed(
    tmp_path: Path, status: int
) -> None:
    script = _Script(_http(status))
    client = _client(tmp_path, script)
    with pytest.raises(JsonCompletionError, match=f"provider_http_{status}"):
        _ask(client)
    assert script.sent == 1 and client.retry_count == 0 and client.transient_failure_count == 0
    (record,) = _records(tmp_path)
    assert json.loads(record.read_bytes())["failure_code"] == f"provider_http_{status}"
    with pytest.raises(JsonCompletionError, match=f"provider_http_{status}"):
        _ask(_client(tmp_path, script))
    assert script.sent == 1


def test_a_response_that_fails_validation_is_not_retried(tmp_path: Path) -> None:
    script = _Script(b"not json")
    with pytest.raises(JsonCompletionError, match="invalid_model_json"):
        _ask(_client(tmp_path, script))
    assert script.sent == 1 and len(_records(tmp_path)) == 1


# ---- records written before ADR 0034 ------------------------------------------------------


def _old_failure(cache: Path, code: str, *, status: int | None) -> Path:
    """What the old client left: a permanent-looking record of a transient failure."""
    with pytest.raises(JsonCompletionError) as missed:
        _ask(_client(cache, _Script(_response()), budget=0), cache_only=True)
    fingerprint = missed.value.request_fingerprint
    path = cache / "requests" / f"{fingerprint}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "request_fingerprint": fingerprint,
                "response_digest": None,
                "failure_code": code,
                "diagnostics": {
                    "endpoint_path": "/v1/chat/completions",
                    "request_bytes": 10,
                    "response_bytes": None,
                    "elapsed_ms": 3,
                    "http_status": status,
                    "exception_type": None,
                    "finish_category": "transport_error",
                    "attempt": 1,
                    "context_path": None,
                    "context_warning": None,
                },
            }
        )
    )
    return path


@pytest.mark.parametrize(
    ("code", "status"),
    [("provider_http_429", 429), ("provider_http_503", 503), ("provider_timeout", None)],
)
def test_an_old_transient_failure_record_is_called_again_and_overwritten(
    tmp_path: Path, code: str, status: int | None
) -> None:
    old = _old_failure(tmp_path, code, status=status)
    # cache_only still replays what is on disk; it never sends.
    with pytest.raises(JsonCompletionError, match=code):
        _ask(_client(tmp_path, _Script(_response()), budget=0), cache_only=True)

    healthy = _Script(_response())
    result = _ask(_client(tmp_path, healthy))
    assert result.parsed.answer == "ok" and not result.cache_hit and healthy.sent == 1
    assert json.loads(old.read_bytes())["failure_code"] is None
    assert not tuple((tmp_path / "requests").glob("*.claim*"))
    assert _ask(_client(tmp_path, healthy)).cache_hit and healthy.sent == 1


def test_an_old_transient_record_that_fails_transiently_again_stays_for_the_next_run(
    tmp_path: Path,
) -> None:
    old = _old_failure(tmp_path, "provider_http_429", status=429)
    before = old.read_bytes()
    down = _Script(_http(429))
    with pytest.raises(JsonCompletionError, match="provider_http_429"):
        _ask(_client(tmp_path, down))
    assert down.sent == 1 + TRANSIENT_MAX_RETRIES
    assert old.read_bytes() == before
    assert not tuple((tmp_path / "requests").glob("*.claim*"))


def test_an_old_transient_record_answered_by_a_permanent_failure_is_replaced_by_it(
    tmp_path: Path,
) -> None:
    old = _old_failure(tmp_path, "provider_connection", status=None)
    refused = _Script(_http(401))
    with pytest.raises(JsonCompletionError, match="provider_http_401"):
        _ask(_client(tmp_path, refused))
    assert json.loads(old.read_bytes())["failure_code"] == "provider_http_401"
    with pytest.raises(JsonCompletionError, match="provider_http_401"):
        _ask(_client(tmp_path, refused))
    assert refused.sent == 1


def test_an_old_transient_record_under_a_live_claim_is_blocked_not_looped(tmp_path: Path) -> None:
    old = _old_failure(tmp_path, "provider_http_429", status=429)
    claim = old.with_suffix(".json.claim")
    claim.write_text(
        json.dumps(
            {
                "claim": json_completion.CLAIM_FORMAT,
                "request_fingerprint": old.stem,
                "host": "another-host",
                "pid": 1,
                "process": "another-process",
                "created_at": json_completion._wall_clock(),
                "lease_seconds": 3600,
            }
        )
    )
    script = _Script(_response())
    client = _client(tmp_path, script)
    with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
        _ask(client)
    assert script.sent == 0 and client.claim_blocked_count == 1


def test_an_old_transient_record_beside_the_old_clients_leftover_claim_heals(
    tmp_path: Path,
) -> None:
    """Clients before ADR 0023 never released a claim: the user's records sit beside one."""
    old = _old_failure(tmp_path, "provider_http_429", status=429)
    claim = old.with_suffix(".json.claim")
    claim.write_text(old.stem)  # the legacy claim format: just the fingerprint
    stamp = json_completion._wall_clock() - json_completion.LEGACY_CLAIM_LEASE_SECONDS - 1
    os.utime(claim, (stamp, stamp))
    script = _Script(_response())
    client = _client(tmp_path, script)
    assert _ask(client).parsed.answer == "ok"
    assert script.sent == 1 and client.claims_taken_over == 1
    assert json.loads(old.read_bytes())["failure_code"] is None
    assert not tuple((tmp_path / "requests").glob("*.claim*"))


def test_an_old_transient_record_of_a_refused_temperature_goes_without_it(
    tmp_path: Path,
) -> None:
    """The memory already knows the endpoint refuses ``temperature`` (ADR 0021)."""
    _old_failure(tmp_path, "provider_http_503", status=503)
    refusal = ProviderRequestError(
        "Provider returned HTTP 400; no retry performed",
        status=400,
        category="http",
        param="temperature",
        error_code="unsupported_value",
    )
    learner = _Script(refusal, _response("learned"))
    assert _ask(_client(tmp_path / "other", learner)).parsed.answer == "learned"

    script = _Script(_response("dropped"))
    result = _ask(_client(tmp_path, script))
    assert result.parsed.answer == "dropped" and result.dropped_parameters == ("temperature",)
    assert script.sent == 1


# ---- interplay with ADR 0021 and ADR 0023 -------------------------------------------------


def test_a_refused_temperature_then_a_rate_limit_then_success(tmp_path: Path) -> None:
    refusal = ProviderRequestError(
        "Provider returned HTTP 400; no retry performed",
        status=400,
        category="http",
        param="temperature",
        error_code="unsupported_value",
    )
    script = _Script(refusal, _http(429), _response())
    client = _client(tmp_path, script)
    result = _ask(client)
    assert result.dropped_parameters == ("temperature",)
    assert script.sent == 3 and client.live_call_count == 3 and client.retry_count == 1
    codes = sorted(
        json.loads(path.read_bytes())["failure_code"] or "ok" for path in _records(tmp_path)
    )
    assert codes == ["ok", "provider_http_400"]


def test_a_lease_covers_one_pause_and_one_attempt_because_every_retry_renews_it() -> None:
    pause = RETRY_MAX_DELAY + RETRY_BASE_DELAY
    for timeout in (45.0, 180.0):
        assert json_completion._claim_lease(timeout) >= 4 * timeout + pause + 120
    assert json_completion._claim_lease(45.0) == 331
    assert json_completion._claim_lease(180.0) == 871
    assert json_completion._claim_lease(180.0) < json_completion.LEGACY_CLAIM_LEASE_SECONDS


def test_a_retry_renews_the_claim_with_the_next_generation(tmp_path: Path) -> None:
    holders: list[list[str]] = []

    def claims() -> None:
        found = sorted(path.name.split(".json.")[1] for path in (tmp_path / "requests").glob("*"))
        holders.append([name for name in found if name.startswith("claim")])

    _ask(_client(tmp_path, _Script(_http(429), _http(429), _response(), on_send=claims)))
    assert holders == [
        ["claim"],
        ["claim", "claim.takeover-1"],
        ["claim", "claim.takeover-1", "claim.takeover-2"],
    ]
    assert not tuple((tmp_path / "requests").glob("*.claim*"))
    # A record after renewals is an ordinary record, not a takeover.
    (record,) = _records(tmp_path)
    assert "claim_takeover" not in json.loads(record.read_bytes())["diagnostics"]


def test_a_retry_whose_claim_was_taken_over_meanwhile_is_not_sent_again(
    tmp_path: Path, waits: _Waits
) -> None:
    def contender() -> None:  # another process judged this call over during the backoff
        (claim,) = (tmp_path / "requests").glob("*.json.claim")
        claim.with_name(claim.name + ".takeover-1").write_text("{}")

    waits.during.append(contender)
    script = _Script(_http(503), _response())
    client = _client(tmp_path, script)
    with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
        _ask(client)
    assert script.sent == 1 and client.claim_blocked_count == 1
    assert not _records(tmp_path)
    # The new holder's claim is left alone.
    assert len(tuple((tmp_path / "requests").glob("*.claim*"))) == 2


# ---- shared cooldown ----------------------------------------------------------------------


def test_a_retry_after_is_shared_by_every_caller_of_the_endpoint(
    tmp_path: Path, waits: _Waits
) -> None:
    other = _Script(_response())

    def meanwhile() -> None:  # another worker calls the same endpoint during the cooldown
        assert _ask(_client(tmp_path / "b", other)).parsed.answer == "ok"

    waits.during.append(meanwhile)
    limited = _Script(_http(429, retry_after=10.0), _response())
    _ask(_client(tmp_path / "a", limited))
    own, theirs = waits.slept
    assert 10.0 <= own < 10.0 + RETRY_BASE_DELAY
    assert 10.0 <= theirs <= 10.0 + RETRY_BASE_DELAY  # waited before its first attempt
    assert other.sent == 1 and limited.sent == 2


def test_the_cooldown_is_per_endpoint_and_model(tmp_path: Path, waits: _Waits) -> None:
    def meanwhile() -> None:
        client = JsonCompletionClient(
            _config(model="another-model"),
            cache_dir=tmp_path / "b",
            max_live_calls=1,
            sender=_Script(_response()),
        )
        _ask(client)

    waits.during.append(meanwhile)
    _ask(_client(tmp_path / "a", _Script(_http(429, retry_after=10.0), _response())))
    assert len(waits.slept) == 1  # the other model never waited


def test_workers_failing_together_do_not_retry_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waits: _Waits
) -> None:
    jitter = iter([0.1, 0.9])
    monkeypatch.setattr(transient, "_random", lambda: next(jitter))
    _ask(_client(tmp_path / "a", _Script(_http(503), _response())))
    _ask(_client(tmp_path / "b", _Script(_http(503), _response())))
    first, second = waits.slept
    assert first != second and 0.5 <= first <= 1 and 0.5 <= second <= 1


# ---- embeddings ---------------------------------------------------------------------------

_EMBEDDING = {
    "APP_EMBEDDING_BASE_URL": "http://127.0.0.1:29002/v1/",
    "APP_EMBEDDING_MODEL": "embedding-model",
    "APP_EMBEDDING_API_KEY": "embedding-secret",
}


class _Embeddings:
    def __init__(self, fault: Callable[[int], ProviderRequestError | None]) -> None:
        self.sizes: list[int] = []
        self._fault = fault

    def __call__(self, url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        inputs = json.loads(payload)["input"]
        texts = inputs if isinstance(inputs, list) else [inputs]
        self.sizes.append(len(texts))
        fault = self._fault(len(self.sizes))
        if fault is not None:
            raise fault
        data = [{"index": index, "embedding": [1.0, float(index)]} for index in range(len(texts))]
        return json.dumps({"data": data}).encode()


def _embedder(endpoint: _Embeddings) -> LocalEmbeddingAdapter:
    return LocalEmbeddingAdapter(load_local_model_config("embedding", _EMBEDDING), sender=endpoint)


def _texts(count: int) -> list[str]:
    return [f"object {index}" for index in range(count)]


def test_a_rate_limited_batch_is_retried_whole_before_any_split(waits: _Waits) -> None:
    endpoint = _Embeddings(lambda n: _http(429, retry_after=3.0) if n == 1 else None)
    adapter = _embedder(endpoint)
    assert len(adapter.embed_descriptions(_texts(16))) == 16
    assert endpoint.sizes == [16, 16]
    assert adapter.request_count == 2 and adapter.retry_count == 1
    assert 3.0 <= waits.slept[0] < 3.0 + RETRY_BASE_DELAY


def test_a_batch_is_split_only_once_its_retries_are_spent(waits: _Waits) -> None:
    endpoint = _Embeddings(lambda n: _http(503) if n <= 1 + TRANSIENT_MAX_RETRIES else None)
    adapter = _embedder(endpoint)
    assert len(adapter.embed_descriptions(_texts(16))) == 16
    assert endpoint.sizes == [16] * (1 + TRANSIENT_MAX_RETRIES) + [8, 8]
    assert adapter.retry_count == TRANSIENT_MAX_RETRIES and adapter.transient_failure_count == 1


def test_a_single_embedding_retries_then_fails_as_before() -> None:
    endpoint = _Embeddings(lambda n: ProviderRequestError("t", category="timeout"))
    adapter = _embedder(endpoint)
    with pytest.raises(ProviderRequestError):
        adapter.embed_query("revenue")
    assert endpoint.sizes == [1] * (1 + TRANSIENT_MAX_RETRIES)
    assert adapter.transient_failure_count == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_permanent_embedding_failure_is_not_retried(status: int) -> None:
    endpoint = _Embeddings(lambda n: _http(status))
    with pytest.raises(ProviderRequestError):
        _embedder(endpoint).embed_query("revenue")
    assert endpoint.sizes == [1]


def test_embedding_and_chat_on_one_gateway_share_nothing_but_their_own_key(
    tmp_path: Path, waits: _Waits
) -> None:
    """A Retry-After on the chat model cools that (URL, model) only, not the embedding."""

    def meanwhile() -> None:
        _embedder(_Embeddings(lambda n: None)).embed_query("revenue")

    waits.during.append(meanwhile)
    _ask(_client(tmp_path, _Script(_http(429, retry_after=5.0), _response())))
    assert len(waits.slept) == 1
