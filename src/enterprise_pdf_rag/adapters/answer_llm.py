"""The answer model client every in-process entry point builds the same way."""

from pathlib import Path

from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.common.evidence.providers.providers import load_llm_config


def make_answer_llm(
    *, cache_dir: Path | None = None, max_live_calls: int | None = None
) -> JsonCompletionClient:
    """A ``JsonCompletionClient`` for the configured LLM (``APP_LLM_*``, else ``OPENAI_*``).

    ``cache_dir`` defaults to ``<ingestion_root>/model-cache`` and ``max_live_calls`` to
    ``APP_ANSWER_MAX_LIVE_CALLS``; the per-call timeout and sampling seed always come from
    ``APP_ANSWER_TIMEOUT_SECONDS`` / ``APP_ANSWER_SEED``. Raises ``ProviderConfigurationError``
    (a ``ValueError``) when the LLM is not configured; no call is made here.
    """
    settings = get_settings()
    return JsonCompletionClient(
        load_llm_config(),
        cache_dir=settings.ingestion_root / "model-cache" if cache_dir is None else cache_dir,
        max_live_calls=(
            settings.answer_max_live_calls if max_live_calls is None else max_live_calls
        ),
        timeout=settings.answer_timeout_seconds,
        seed=settings.answer_seed,
    )
