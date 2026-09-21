"""A scripted transport for the answer chain; no model, no network, one call visible."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, SecretStr

from enterprise_pdf_rag.adapters.json_completion import JsonCompletionClient
from enterprise_pdf_rag.adapters.providers import LLMConfig
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim

Script = Callable[[str], ModelAnswer | str]
# The query-translation call (ADR 0018) has its own response schema, so it is scripted
# separately and stays out of ``prompts``, which keeps meaning "the synthesis prompts".
Translator = Callable[[str], BaseModel | str]
# The document-tree routing call (ADR 0019) likewise has its own response schema, so it is
# scripted separately and stays out of ``prompts`` too: an outline is a map, not a prompt.
Router = Callable[[str], BaseModel | str]


def llm_config() -> LLMConfig:
    return LLMConfig(
        api_key=SecretStr("offline-secret"),
        base_url="https://provider.invalid",
        model="offline-test",
    )


def scripted_client(
    cache_dir: Path,
    script: Script,
    *,
    max_live_calls: int = 1,
    translator: Translator | None = None,
    router: Router | None = None,
) -> tuple[JsonCompletionClient, list[str]]:
    """Return a bounded client whose transport replays ``script(prompt)``; prompts are recorded.

    A ``str`` script result is sent verbatim as the message content so tests can
    exercise malformed model output. ``translator`` answers the query-translation call and
    ``router`` the tree-routing one, each recognised by its own response schema; without the
    matching script an attempted call fails the test rather than silently returning the
    answer schema.
    """
    prompts: list[str] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        assert url == "https://provider.invalid/v1/chat/completions"
        request = json.loads(payload)
        assert request["response_format"]["json_schema"]["strict"] is True
        prompt = request["messages"][1]["content"]
        assert isinstance(prompt, str)  # text-only completion: no image part
        properties = request["response_format"]["json_schema"]["schema"]["properties"]
        if "english_query" in properties:
            assert translator is not None, "an unscripted query translation was requested"
            scripted = translator(prompt)
        elif "node_ids" in properties:
            assert router is not None, "an unscripted document-tree route was requested"
            scripted = router(prompt)
        else:
            prompts.append(prompt)
            scripted = script(prompt)
        content = scripted if isinstance(scripted, str) else scripted.model_dump_json()
        return json.dumps(
            {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        ).encode()

    client = JsonCompletionClient(
        llm_config(), cache_dir=cache_dir, max_live_calls=max_live_calls, sender=sender
    )
    return client, prompts


def chart_claim(member_id: str, point_id: str, text: str, *, claim_id: str = "c1") -> ModelClaim:
    return ModelClaim(
        claim_id=claim_id,
        member_id=member_id,
        kind="chart_value",
        field_path=f"points.{point_id}.value",
        text=text,
    )


def answered(answer: str, *claims: ModelClaim) -> ModelAnswer:
    return ModelAnswer(abstain=False, abstain_reason=None, answer=answer, claims=claims)


def declined(
    reason: Literal["not_in_context", "ambiguous", "needs_calculation"] = "not_in_context",
) -> ModelAnswer:
    return ModelAnswer(abstain=True, abstain_reason=reason, answer="", claims=())
