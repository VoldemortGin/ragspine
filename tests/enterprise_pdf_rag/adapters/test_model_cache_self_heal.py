"""ADR 0029: a model-cache entry lost by an asynchronous flush is re-called once, never stuck."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from ragspine.common.evidence.file_placement import recording_repairs
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
)
from ragspine.common.evidence.providers.providers import LLMConfig
from tests.enterprise_pdf_rag.adapters.model_cache_helpers import (
    claims,
    damage_record,
    damage_response,
    record_bytes,
    record_keys,
    response_bytes,
    response_digests,
)
from tests.enterprise_pdf_rag.adapters.no_hard_link_helpers import forbid_hard_links

pytestmark = pytest.mark.usefixtures("model_cache_backend")


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    answer: str


def _config() -> LLMConfig:
    return LLMConfig(
        api_key=SecretStr("offline"), base_url="https://example.invalid", model="test-model"
    )


class _Sender:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls = 0

    def __call__(self, url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return json.dumps(
            {
                "model": "m",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"answer": answer})},
                    }
                ],
            }
        ).encode()


def _ask(
    cache: Path, sender: _Sender, *, budget: int = 1, cache_only: bool = False
) -> tuple[str, JsonCompletionClient]:
    client = JsonCompletionClient(_config(), cache_dir=cache, max_live_calls=budget, sender=sender)
    result = client.complete_text_json(
        task="heal", prompt="question", response_model=_Answer, cache_only=cache_only
    )
    return result.parsed.answer, client


def _key(cache: Path) -> str:
    (key,) = record_keys(cache)
    return key


def _record(cache: Path) -> bytes:
    data = record_bytes(cache, _key(cache))
    assert data is not None
    return data


def _digest(cache: Path) -> str:
    (digest,) = response_digests(cache)
    return digest


@pytest.mark.parametrize("damage", ["response-missing", "response-truncated", "record-empty"])
@pytest.mark.parametrize("links", [True, False])
def test_a_damaged_entry_is_called_again_once_then_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, links: bool
) -> None:
    if not links:
        forbid_hard_links(monkeypatch)
    first = _Sender(["first"])
    assert _ask(tmp_path, first)[0] == "first"
    if damage == "response-missing":
        damage_response(tmp_path, _digest(tmp_path), None)
    elif damage == "response-truncated":
        digest = _digest(tmp_path)
        stored = response_bytes(tmp_path, digest)
        assert stored is not None
        damage_response(tmp_path, digest, stored[:9])
    else:
        damage_record(tmp_path, _key(tmp_path), b"")

    again = _Sender(["second"])
    with recording_repairs() as repairs:
        answer, client = _ask(tmp_path, again)
    assert (again.calls, client.live_call_count, client.repaired_count) == (1, 1, 1)
    assert answer == "second" and repairs == {"model_cache": 1}

    replay = _Sender(["never"])
    assert _ask(tmp_path, replay, budget=0)[0] == "second"
    assert replay.calls == 0
    assert claims(tmp_path) == []


def test_a_lost_response_reproduced_identically_keeps_the_record(tmp_path: Path) -> None:
    _ask(tmp_path, _Sender(["same"]))
    written = _record(tmp_path)
    damage_response(tmp_path, _digest(tmp_path), None)
    assert _ask(tmp_path, _Sender(["same"]))[0] == "same"
    assert _record(tmp_path) == written


def test_cache_only_and_no_budget_never_call_for_a_damaged_entry(tmp_path: Path) -> None:
    _ask(tmp_path, _Sender(["first"]))
    damage_response(tmp_path, _digest(tmp_path), None)
    sender = _Sender(["second"])
    with pytest.raises(JsonCompletionError, match="missing_cached_response"):
        _ask(tmp_path, sender, cache_only=True)
    with pytest.raises(JsonCompletionError, match="call_budget_exhausted"):
        _ask(tmp_path, sender, budget=0)
    assert sender.calls == 0


def test_a_record_bound_to_another_request_is_still_refused(tmp_path: Path) -> None:
    _ask(tmp_path, _Sender(["first"]))
    data = json.loads(_record(tmp_path))
    data["request_fingerprint"] = "0" * 64
    damage_record(tmp_path, _key(tmp_path), json.dumps(data).encode())
    sender = _Sender(["second"])
    with pytest.raises(JsonCompletionError, match="cache_binding_mismatch"):
        _ask(tmp_path, sender)
    assert sender.calls == 0
