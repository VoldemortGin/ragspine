"""A claim left by a dead attempt no longer blocks its request forever (ADR 00NN).

A ``.claim`` still means "this request may be in flight; do not send it twice" for as long as
its holder may run. Once the holder is certainly over — a process of this host that no longer
runs, or a lease that has run out — exactly one later caller takes the claim over and sends the
request once more; a legacy claim (no holder recorded) is judged by its mtime.
"""

import errno
import json
import os
import socket
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Barrier, Event

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from ragspine.common.evidence.providers import json_completion
from ragspine.common.evidence.providers.json_completion import (
    CLAIM_FORMAT,
    LEGACY_CLAIM_LEASE_SECONDS,
    JsonCompletionClient,
    JsonCompletionError,
    JsonCompletionResult,
    forget_unsupported_sampling_parameters,
)
from ragspine.common.evidence.providers.providers import LLMConfig, ProviderRequestError
from tests.enterprise_pdf_rag.adapters.no_hard_link_helpers import (
    fail_directory_fsync,
    forbid_hard_links,
)

PNG = b"\x89PNG\r\n\x1a\nfixture-bytes"
KEY = "claim-test-key"
_ROOT = Path(__file__).resolve().parents[3]
# The lease a client with the default 45 s timeout writes into its claim.
_LEASE = json_completion._claim_lease(45.0)

posix_only = pytest.mark.skipif(os.name != "posix", reason="pid liveness is probed on POSIX only")


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    answer: str


def _config() -> LLMConfig:
    return LLMConfig(api_key=SecretStr(KEY), base_url="https://example.invalid", model="m")


def _response() -> bytes:
    return json.dumps(
        {"choices": [{"finish_reason": "stop", "message": {"content": '{"answer":"ok"}'}}]}
    ).encode()


Sender = Callable[..., bytes]


def _counting(calls: list[str]) -> Sender:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        calls.append("sent")
        return _response()

    return sender


def _client(cache: Path, sender: Sender, *, budget: int = 1) -> JsonCompletionClient:
    return JsonCompletionClient(_config(), cache_dir=cache, max_live_calls=budget, sender=sender)


def _ask(client: JsonCompletionClient) -> JsonCompletionResult[_Answer]:
    return client.complete_json(task="claim", prompt="same", image_png=PNG, response_model=_Answer)


def _fingerprint(cache: Path) -> str:
    with pytest.raises(JsonCompletionError) as missed:
        _ask_cache_only(cache)
    return missed.value.request_fingerprint


def _ask_cache_only(cache: Path) -> JsonCompletionResult[_Answer]:
    return JsonCompletionClient(_config(), cache_dir=cache, max_live_calls=0).complete_json(
        task="claim", prompt="same", image_png=PNG, response_model=_Answer, cache_only=True
    )


def _finished_pid() -> int:
    """The pid of a process that has already exited (and been reaped)."""
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(done.stdout)


def _owner(fingerprint: str, **override: object) -> bytes:
    """A current-format claim of another process, made just now, whose holder still runs."""
    owner: dict[str, object] = {
        "claim": CLAIM_FORMAT,
        "request_fingerprint": fingerprint,
        "host": socket.gethostname(),
        "pid": os.getppid(),
        "process": "an-earlier-process",
        "created_at": json_completion._wall_clock(),
        "lease_seconds": _LEASE,
    }
    owner.update(override)
    return json.dumps(owner).encode()


def _place_claim(cache: Path, fingerprint: str, content: bytes, *, age: float = 0.0) -> Path:
    path = cache / "requests" / f"{fingerprint}.json.claim"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if age:
        stamp = json_completion._wall_clock() - age
        os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[float], None]]:
    """Move the claims' wall clock forward without sleeping."""
    offset = [0.0]
    real = json_completion._wall_clock
    monkeypatch.setattr(json_completion, "_wall_clock", lambda: real() + offset[0])

    def advance(seconds: float) -> None:
        offset[0] += seconds

    yield advance


def _record(cache: Path, fingerprint: str, suffix: str = ".json") -> dict[str, object]:
    loaded = json.loads((cache / "requests" / f"{fingerprint}{suffix}").read_bytes())
    assert isinstance(loaded, dict)
    return loaded


def _interrupted(cache: Path) -> str:
    """A call of this process interrupted inside the transport: a claim, no record."""

    def interrupted(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _ask(_client(cache, interrupted))
    (claim,) = (cache / "requests").glob("*.claim")
    assert not tuple((cache / "requests").glob("*.json"))
    return claim.name.removesuffix(".json.claim")


# ---- the claim itself ----------------------------------------------------------------------


def test_a_claim_names_its_holder_and_lease_and_is_released_once_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, object]] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        (claim,) = (tmp_path / "requests").glob("*.claim")
        raw = claim.read_bytes()
        assert KEY.encode() not in raw and b"same" not in raw
        seen.append(json.loads(raw))
        return _response()

    result = _ask(_client(tmp_path, sender))

    (owner,) = seen
    assert set(owner) == {
        "claim",
        "request_fingerprint",
        "host",
        "pid",
        "process",
        "created_at",
        "lease_seconds",
    }
    assert (owner["claim"], owner["host"], owner["pid"], owner["lease_seconds"]) == (
        CLAIM_FORMAT,
        socket.gethostname(),
        os.getpid(),
        _LEASE,
    )
    assert owner["request_fingerprint"] == result.request_fingerprint
    assert not tuple((tmp_path / "requests").glob("*.claim*"))
    # A record without a claim takeover keeps the old record bytes (no new key).
    diagnostics = _record(tmp_path, result.request_fingerprint)["diagnostics"]
    assert isinstance(diagnostics, dict) and "claim_takeover" not in diagnostics


def test_the_lease_covers_every_blocking_step_of_the_slowest_allowed_call() -> None:
    assert json_completion._claim_lease(45.0) == 300
    assert json_completion._claim_lease(180.0) == 840
    assert json_completion._claim_lease(180.0) + 60 <= LEGACY_CLAIM_LEASE_SECONDS


def test_old_data_with_a_success_record_and_its_claim_replays_untouched(tmp_path: Path) -> None:
    calls: list[str] = []
    first = _ask(_client(tmp_path, _counting(calls)))
    legacy = _place_claim(tmp_path, first.request_fingerprint, first.request_fingerprint.encode())

    replay = _ask(_client(tmp_path, _counting(calls), budget=0))

    assert replay.cache_hit and replay.request_fingerprint == first.request_fingerprint
    assert calls == ["sent"] and legacy.read_bytes() == first.request_fingerprint.encode()


# ---- a holder that may still run is never overtaken -----------------------------------------


def test_an_interrupted_call_stays_blocked_until_its_lease_runs_out(
    tmp_path: Path, clock: Callable[[float], None]
) -> None:
    fingerprint = _interrupted(tmp_path)
    calls: list[str] = []
    blocked = _client(tmp_path, _counting(calls))

    with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
        _ask(blocked)
    clock(_LEASE - 5)
    with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
        _ask(blocked)
    assert calls == [] and blocked.claim_blocked_count == 2 and blocked.claims_taken_over == 0
    assert blocked.live_call_count == 0

    clock(10)
    recovered = _client(tmp_path, _counting(calls))
    result = _ask(recovered)

    assert calls == ["sent"] and not result.cache_hit
    assert recovered.claims_taken_over == 1 and recovered.claim_blocked_count == 0
    assert recovered.live_call_count == 1  # the resend is one live call
    assert result.diagnostics is not None and result.diagnostics.claim_takeover == 1
    assert _record(tmp_path, fingerprint)["diagnostics"]["claim_takeover"] == 1  # type: ignore[index]
    assert not tuple((tmp_path / "requests").glob("*.claim*"))
    again = _ask(_client(tmp_path, _counting(calls), budget=0))
    assert again.cache_hit and calls == ["sent"]


def test_a_live_holder_in_this_process_is_never_overtaken(tmp_path: Path) -> None:
    entered, release = Event(), Event()
    calls: list[str] = []

    def held(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        calls.append("holder")
        entered.set()
        assert release.wait(5)
        return _response()

    other = _client(tmp_path, _counting(calls))
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(_ask, _client(tmp_path, held))
        assert entered.wait(5)
        try:
            with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
                _ask(other)
        finally:
            release.set()
        assert not pending.result(timeout=5).cache_hit
    assert calls == ["holder"] and other.claim_blocked_count == 1
    assert _ask(other).cache_hit and calls == ["holder"]


@pytest.mark.parametrize("case", ["alive-here", "other-host", "this-process"])
def test_a_current_format_claim_within_its_lease_stays_in_progress(
    tmp_path: Path, case: str
) -> None:
    fingerprint = _fingerprint(tmp_path)
    content = {
        "alive-here": _owner(fingerprint),
        # On another host the pid says nothing, dead or not: only the lease counts.
        "other-host": _owner(fingerprint, host="elsewhere", pid=_finished_pid()),
        "this-process": _owner(fingerprint, process=json_completion._PROCESS_TOKEN),
    }[case]
    claim = _place_claim(tmp_path, fingerprint, content)
    calls: list[str] = []
    client = _client(tmp_path, _counting(calls))

    with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
        _ask(client)

    assert calls == [] and client.claims_taken_over == 0
    assert claim.read_bytes() == content
    assert sorted(path.name for path in claim.parent.iterdir()) == [claim.name]


@pytest.mark.parametrize("case", ["alive-here", "other-host", "this-process"])
def test_a_current_format_claim_past_its_lease_is_taken_over(
    tmp_path: Path, clock: Callable[[float], None], case: str
) -> None:
    fingerprint = _fingerprint(tmp_path)
    content = {
        "alive-here": _owner(fingerprint),  # a pid reused by an unrelated process
        "other-host": _owner(fingerprint, host="elsewhere"),
        "this-process": _owner(fingerprint, process=json_completion._PROCESS_TOKEN),
    }[case]
    _place_claim(tmp_path, fingerprint, content)
    clock(_LEASE + 1)
    calls: list[str] = []
    client = _client(tmp_path, _counting(calls))

    assert not _ask(client).cache_hit
    assert calls == ["sent"] and client.claims_taken_over == 1


# ---- a dead holder ---------------------------------------------------------------------------


@posix_only
def test_a_claim_of_a_dead_process_of_this_host_is_taken_over_at_once(tmp_path: Path) -> None:
    fingerprint = _fingerprint(tmp_path)
    _place_claim(tmp_path, fingerprint, _owner(fingerprint, pid=_finished_pid()))
    calls: list[str] = []
    client = _client(tmp_path, _counting(calls))

    _ask(client)

    assert calls == ["sent"] and client.claims_taken_over == 1
    assert _record(tmp_path, fingerprint)["diagnostics"]["claim_takeover"] == 1  # type: ignore[index]
    assert sorted(path.name for path in (tmp_path / "requests").iterdir()) == [
        f"{fingerprint}.json"
    ]


@posix_only
def test_a_process_killed_mid_call_leaves_a_claim_the_next_process_recovers(
    tmp_path: Path,
) -> None:
    """A real child process claims, then dies inside the transport (no record is written)."""
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from pydantic import BaseModel, ConfigDict, SecretStr
        from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
        from ragspine.common.evidence.providers.providers import LLMConfig

        class _Answer(BaseModel):  # the same schema title, so the same fingerprint
            model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
            answer: str

        def killed(url, *, api_key, payload, timeout):
            os._exit(9)

        JsonCompletionClient(
            LLMConfig(api_key=SecretStr({KEY!r}), base_url="https://example.invalid", model="m"),
            cache_dir=Path({str(tmp_path)!r}),
            max_live_calls=1,
            sender=killed,
        ).complete_json(task="claim", prompt="same", image_png={PNG!r}, response_model=_Answer)
        """
    )
    child = subprocess.run([sys.executable, "-c", script], cwd=_ROOT, check=False)
    assert child.returncode == 9
    (claim,) = (tmp_path / "requests").glob("*.claim")
    assert not tuple((tmp_path / "requests").glob("*.json"))

    calls: list[str] = []
    client = _client(tmp_path, _counting(calls))
    _ask(client)

    assert calls == ["sent"] and client.claims_taken_over == 1
    assert not claim.exists()


@posix_only
def test_a_dead_takeover_is_itself_taken_over_by_the_next_generation(tmp_path: Path) -> None:
    fingerprint = _fingerprint(tmp_path)
    dead = _owner(fingerprint, pid=_finished_pid())
    _place_claim(tmp_path, fingerprint, dead)
    record = tmp_path / "requests" / f"{fingerprint}.json"
    (tmp_path / "requests" / f"{fingerprint}.json.claim.takeover-1").write_bytes(dead)

    assert json_completion._claim_request(record, fingerprint, _LEASE) == 2
    # The new holder is this (running) process: nobody may take it over now.
    with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
        json_completion._claim_request(record, fingerprint, _LEASE)
    json_completion._release_claims(record)
    assert not tuple((tmp_path / "requests").iterdir())


# ---- claims written by the old client (fingerprint only, or empty) --------------------------


@pytest.mark.parametrize("content", ["fingerprint", "empty", "garbage"])
def test_a_legacy_claim_is_taken_over_only_once_it_is_old_enough(
    tmp_path: Path, content: str
) -> None:
    fingerprint = _fingerprint(tmp_path)
    raw = {"fingerprint": fingerprint.encode(), "empty": b"", "garbage": b"{not json"}[content]
    claim = _place_claim(tmp_path, fingerprint, raw, age=LEGACY_CLAIM_LEASE_SECONDS - 30)
    calls: list[str] = []

    young = _client(tmp_path, _counting(calls))
    with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
        _ask(young)
    assert calls == [] and claim.read_bytes() == raw

    stamp = json_completion._wall_clock() - LEGACY_CLAIM_LEASE_SECONDS - 30
    os.utime(claim, (stamp, stamp))
    old = _client(tmp_path, _counting(calls))
    _ask(old)

    assert calls == ["sent"] and old.claims_taken_over == 1
    assert not claim.exists()
    assert _ask(_client(tmp_path, _counting(calls), budget=0)).cache_hit and calls == ["sent"]


def test_a_legacy_claim_does_not_count_as_old_because_the_clock_moved_within_its_lease(
    tmp_path: Path, clock: Callable[[float], None]
) -> None:
    fingerprint = _fingerprint(tmp_path)
    _place_claim(tmp_path, fingerprint, fingerprint.encode())
    clock(json_completion._claim_lease(180.0))  # past any current lease, not the legacy one
    calls: list[str] = []
    with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
        _ask(_client(tmp_path, _counting(calls)))
    assert calls == []


# ---- racing for one expired claim -----------------------------------------------------------


@pytest.mark.parametrize("hard_links", [True, False])
def test_two_clients_racing_for_one_expired_claim_send_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hard_links: bool
) -> None:
    if not hard_links:
        forbid_hard_links(monkeypatch)
        fail_directory_fsync(monkeypatch, errno.EINVAL)
    fingerprint = _fingerprint(tmp_path)
    _place_claim(tmp_path, fingerprint, b"", age=LEGACY_CLAIM_LEASE_SECONDS + 60)
    # Both decide the holder is dead before either creates the next generation.
    both_judged = Barrier(2, timeout=5)
    real_expired = json_completion._expired

    def judged_together(content: bytes, modified: float) -> bool:
        verdict = real_expired(content, modified)
        both_judged.wait()
        return verdict

    monkeypatch.setattr(json_completion, "_expired", judged_together)
    release = Event()
    calls: list[str] = []

    def winner_waits(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        calls.append("sent")
        assert release.wait(5)
        return _response()

    clients = [_client(tmp_path, winner_waits) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_ask, client) for client in clients]
        done, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
        (loser,) = done
        with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
            loser.result()
        release.set()
        (winner,) = [future for future in futures if future is not loser]
        assert not winner.result(timeout=5).cache_hit

    assert calls == ["sent"]
    assert sorted(client.claims_taken_over for client in clients) == [0, 1]
    assert sorted(client.claim_blocked_count for client in clients) == [0, 1]
    assert _record(tmp_path, fingerprint)["diagnostics"]["claim_takeover"] == 1  # type: ignore[index]


def test_a_holder_that_finishes_while_we_look_is_replayed_not_resent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_claim = json_completion._claim_request
    finished: list[bool] = []

    def holder_finishes_first(record_path: Path, fingerprint: str, lease: int) -> int:
        # Between our record lookup and our claim, another process sends, records and releases.
        if not finished:
            finished.append(True)
            monkeypatch.setattr(json_completion, "_claim_request", real_claim)
            _ask(_client(tmp_path, _counting(calls)))
            monkeypatch.setattr(json_completion, "_claim_request", holder_finishes_first)
        return real_claim(record_path, fingerprint, lease)

    monkeypatch.setattr(json_completion, "_claim_request", holder_finishes_first)
    late = _client(tmp_path, _counting(calls))

    result = _ask(late)

    assert result.cache_hit and calls == ["sent"] and late.live_call_count == 0
    assert not tuple((tmp_path / "requests").glob("*.claim*"))


# ---- budget and the sampling fallback (ADR 0021) --------------------------------------------


def test_a_takeover_needs_budget_and_leaves_the_claim_when_there_is_none(
    tmp_path: Path, clock: Callable[[float], None]
) -> None:
    _interrupted(tmp_path)
    clock(_LEASE + 1)
    calls: list[str] = []
    starved = _client(tmp_path, _counting(calls), budget=0)
    with pytest.raises(JsonCompletionError, match="call_budget_exhausted"):
        _ask(starved)
    assert calls == [] and starved.claims_taken_over == 0
    assert len(tuple((tmp_path / "requests").glob("*.claim"))) == 1


def _refuses_temperature(calls: list[str]) -> Sender:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        if "temperature" in json.loads(payload):
            calls.append("with-temperature")
            raise ProviderRequestError(
                "Provider returned HTTP 400; no retry performed",
                status=400,
                category="http",
                param="temperature",
                error_code="unsupported_value",
            )
        calls.append("without")
        return _response()

    return sender


def test_a_taken_over_claim_on_a_refusing_endpoint_probes_once_then_drops(
    tmp_path: Path, clock: Callable[[float], None]
) -> None:
    _interrupted(tmp_path)  # stuck at the fingerprint that carries temperature
    clock(_LEASE + 1)
    forget_unsupported_sampling_parameters()  # a new kernel
    calls: list[str] = []
    client = _client(tmp_path, _refuses_temperature(calls), budget=2)

    result = _ask(client)

    assert result.dropped_parameters == ("temperature",)
    assert calls == ["with-temperature", "without"] and client.claims_taken_over == 1
    assert not tuple((tmp_path / "requests").glob("*.claim*"))
    forget_unsupported_sampling_parameters()
    again = _ask(_client(tmp_path, _refuses_temperature(calls), budget=0))
    assert again.cache_hit and len(calls) == 2


def test_an_old_reprobe_claim_left_by_a_killed_kernel_is_taken_over_once_old_enough(
    tmp_path: Path,
) -> None:
    """The pre-ADR-0021 400 record is re-probed at ``.retry-1.json``; a kernel killed during that
    re-probe left a legacy claim there, which no longer blocks the re-probe forever."""
    fingerprint = _fingerprint(tmp_path)
    requests = tmp_path / "requests"
    requests.mkdir()
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
                    "context_path": None,
                    "context_warning": None,
                },
            }
        )
    )
    (requests / f"{fingerprint}.json.claim").write_text(fingerprint)
    retry_claim = requests / f"{fingerprint}.retry-1.json.claim"
    retry_claim.write_text(fingerprint)
    calls: list[str] = []
    with pytest.raises(JsonCompletionError, match="request_in_progress_or_uncertain"):
        _ask(_client(tmp_path, _refuses_temperature(calls), budget=2))
    assert calls == []

    stamp = json_completion._wall_clock() - LEGACY_CLAIM_LEASE_SECONDS - 1
    os.utime(retry_claim, (stamp, stamp))
    forget_unsupported_sampling_parameters()
    result = _ask(_client(tmp_path, _refuses_temperature(calls), budget=2))

    assert result.dropped_parameters == ("temperature",)
    assert calls == ["with-temperature", "without"]
    retry = _record(tmp_path, fingerprint, ".retry-1.json")
    assert retry["diagnostics"]["claim_takeover"] == 1  # type: ignore[index]
    assert not retry_claim.exists()
    assert (requests / f"{fingerprint}.json.claim").exists()  # old data left as it was
