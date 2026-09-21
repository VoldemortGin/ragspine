"""A question outside the index's language is restated in it by one bounded, cached call."""

import json
from pathlib import Path
from typing import Any, Never

import pytest
from pydantic import SecretStr

from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient
from enterprise_pdf_rag.adapters.providers import LLMConfig
from enterprise_pdf_rag.adapters.query_translation import (
    TRANSLATION_RULES,
    TRANSLATION_TASK,
    QueryTranslationDTO,
    is_foreign_script,
    translate_query,
)
from enterprise_pdf_rag.answers.models import TranslatedQuery

_QUESTION = "2026 上半年 分销渠道 占比"
_ENGLISH = "Distribution mix in 1H26"


def _config() -> LLMConfig:
    return LLMConfig(
        api_key=SecretStr("offline-secret"),
        base_url="https://provider.invalid",
        model="offline-test",
    )


def _client(
    cache_dir: Path, reply: str, *, max_live_calls: int = 2
) -> tuple[JsonCompletionClient, list[Any]]:
    seen: list[Any] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        request = json.loads(payload)
        seen.append(request)
        return json.dumps(
            {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]}
        ).encode()

    client = JsonCompletionClient(
        _config(), cache_dir=cache_dir, max_live_calls=max_live_calls, sender=sender
    )
    return client, seen


_REPLY = json.dumps({"english_query": _ENGLISH, "source_language": "Chinese"})


def test_a_foreign_question_is_translated_by_one_strict_bounded_call(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, _REPLY)
    translated = translate_query(_QUESTION, client)
    assert translated == TranslatedQuery(_ENGLISH, "Chinese", cache_hit=False)
    assert client.live_call_count == 1

    (request,) = seen
    assert request["messages"] == [
        {"role": "system", "content": TRANSLATION_RULES},
        {"role": "user", "content": request["messages"][1]["content"]},
    ]
    # The question reaches the model as data, verbatim.
    assert _QUESTION in str(request["messages"][1]["content"])
    schema = request["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert sorted(schema["schema"]["properties"]) == ["english_query", "source_language"]


def test_the_translation_replays_from_the_cache_without_a_second_call(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, _REPLY)
    first = translate_query(_QUESTION, client)
    again = translate_query(_QUESTION, client)
    assert first is not None and again is not None
    assert (first.cache_hit, again.cache_hit) == (False, True)
    assert again.english == first.english
    assert len(seen) == 1 and client.live_call_count == 1


def test_the_task_salt_keeps_translations_apart_from_answers(tmp_path: Path) -> None:
    assert TRANSLATION_TASK == "query-translation-v1"
    client, _ = _client(tmp_path, _REPLY)
    translate_query(_QUESTION, client)
    records = sorted(path.name for path in (tmp_path / "requests").glob("*.json"))
    assert len(records) == 1
    # A different task over the same text is a different cache entry.
    other = client.complete_text_json(
        task="rag-answer-v1",
        prompt=_QUESTION,
        response_model=QueryTranslationDTO,
        system=TRANSLATION_RULES,
    )
    assert other.request_fingerprint not in records[0]


def test_an_exhausted_budget_yields_no_translation_instead_of_failing(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, _REPLY, max_live_calls=0)
    assert translate_query(_QUESTION, client) is None
    assert seen == []


def test_a_transport_failure_yields_no_translation(tmp_path: Path) -> None:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> Never:
        raise TimeoutError("no route")

    client = JsonCompletionClient(_config(), cache_dir=tmp_path, max_live_calls=2, sender=sender)
    assert translate_query(_QUESTION, client) is None


@pytest.mark.parametrize(
    "reply",
    [
        json.dumps({"english_query": "   ", "source_language": "Chinese"}),
        json.dumps({"english_query": _ENGLISH, "source_language": ""}),
        "not json at all",
    ],
)
def test_an_unusable_model_reply_yields_no_translation(tmp_path: Path, reply: str) -> None:
    client, _ = _client(tmp_path, reply)
    assert translate_query(_QUESTION, client) is None


def test_a_blank_question_is_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _REPLY)
    with pytest.raises(ValueError):
        translate_query("   ", client)


@pytest.mark.parametrize(
    "question", ["2026 上半年 分销渠道 占比", "代理人科技投入", "泰国 1H26 VONB"]
)
def test_is_foreign_script_accepts_a_question_outside_the_latin_alphabet(question: str) -> None:
    assert is_foreign_script(question)


@pytest.mark.parametrize("question", ["What was VONB in 1H26?", "VONB 1H26", "1H26 / 2026", ""])
def test_is_foreign_script_rejects_an_ascii_question(question: str) -> None:
    assert not is_foreign_script(question)
