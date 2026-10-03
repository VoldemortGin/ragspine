"""Provider configuration is explicit; unit tests never call a live endpoint."""

import json

import pytest

from ragspine.common.evidence.providers.providers import (
    OpenAICompatibleSmoke,
    ProviderConfigurationError,
    load_llm_config,
    load_local_model_config,
)


def test_missing_model_is_an_error_and_secrets_are_redacted() -> None:
    environment = {
        "APP_LLM_API_KEY": "test-secret",
        "APP_LLM_BASE_URL": "https://provider.example",
    }
    with pytest.raises(ProviderConfigurationError, match="APP_LLM_MODEL"):
        load_llm_config(environment)
    config = load_llm_config({**environment, "APP_LLM_MODEL": "configured-model"})
    assert "test-secret" not in repr(config)
    assert config.chat_completions_url == "https://provider.example/v1/chat/completions"
    assert (
        load_llm_config(
            {
                **environment,
                "APP_LLM_BASE_URL": "https://provider.example/v1/",
                "APP_LLM_MODEL": "configured-model",
            }
        ).chat_completions_url
        == config.chat_completions_url
    )


def test_embedding_never_inherits_the_llm_model_and_rerank_has_no_gateway_fallback() -> None:
    with pytest.raises(ProviderConfigurationError, match="OPENAI_EMBEDDING_MODEL"):
        load_local_model_config(
            "embedding",
            {
                "APP_LLM_API_KEY": "cloud-secret",
                "APP_LLM_BASE_URL": "https://cloud.example",
                "APP_LLM_MODEL": "cloud-model",
            },
        )
    with pytest.raises(ProviderConfigurationError, match=r"APP_RERANK_BASE_URL.*no gateway"):
        load_local_model_config(
            "rerank",
            {"APP_LLM_API_KEY": "cloud-secret", "APP_LLM_BASE_URL": "https://cloud.example"},
        )


_GATEWAY = {
    "APP_LLM_API_KEY": "cloud-secret",
    "APP_LLM_BASE_URL": "https://gateway.example/v1/",
    "APP_EMBEDDING_MODEL": "gateway-embedding",
}


def test_embedding_without_a_base_url_shares_the_llm_gateway_and_key() -> None:
    config = load_local_model_config("embedding", _GATEWAY)
    assert (config.base_url, config.model) == ("https://gateway.example/v1", "gateway-embedding")
    assert config.api_key.get_secret_value() == "cloud-secret"
    assert "cloud-secret" not in repr(config)
    own_key = load_local_model_config("embedding", _GATEWAY | {"APP_EMBEDDING_API_KEY": "own"})
    assert own_key.api_key.get_secret_value() == "own"
    # The resolved form a child process receives: the base spelled out equals the gateway.
    spelled = _GATEWAY | {"APP_EMBEDDING_BASE_URL": "https://gateway.example/v1"}
    assert load_local_model_config("embedding", spelled).base_url == "https://gateway.example/v1"


def test_the_shared_gateway_is_still_https_only_and_needs_a_key() -> None:
    with pytest.raises(ProviderConfigurationError, match="Invalid service base URL"):
        load_local_model_config("embedding", _GATEWAY | {"APP_LLM_BASE_URL": "http://gw.example"})
    with pytest.raises(ProviderConfigurationError, match="OPENAI_API_KEY"):
        load_local_model_config("embedding", {**_GATEWAY, "APP_LLM_API_KEY": ""})
    with pytest.raises(ProviderConfigurationError, match="OPENAI_BASE_URL"):
        load_local_model_config("embedding", {"APP_EMBEDDING_MODEL": "m", "APP_LLM_API_KEY": "k"})


def test_a_separate_embedding_base_url_needs_its_own_model_and_key() -> None:
    separate = {"APP_EMBEDDING_BASE_URL": "http://127.0.0.1:28002", **_GATEWAY}
    with pytest.raises(ProviderConfigurationError, match="APP_EMBEDDING_API_KEY"):
        load_local_model_config("embedding", separate)
    with pytest.raises(ProviderConfigurationError, match="APP_EMBEDDING_MODEL"):
        load_local_model_config(
            "embedding",
            {"APP_EMBEDDING_BASE_URL": "http://127.0.0.1:28002", "APP_EMBEDDING_API_KEY": "k"},
        )
    with pytest.raises(ProviderConfigurationError, match="loopback"):
        load_local_model_config(
            "embedding",
            separate
            | {"APP_EMBEDDING_BASE_URL": "https://other.example", "APP_EMBEDDING_API_KEY": "k"},
        )


def test_the_template_placeholder_key_is_rejected_by_name_before_any_request() -> None:
    separate = {
        "APP_EMBEDDING_BASE_URL": "http://127.0.0.1:28002",
        "APP_EMBEDDING_MODEL": "m",
        "APP_RERANK_BASE_URL": "http://127.0.0.1:28001",
        "APP_RERANK_MODEL": "m",
    }
    cases = (
        ("embedding", _GATEWAY | {"APP_EMBEDDING_API_KEY": " ... "}, "APP_EMBEDDING_API_KEY"),
        ("embedding", separate | {"APP_EMBEDDING_API_KEY": "..."}, "APP_EMBEDDING_API_KEY"),
        ("embedding", _GATEWAY | {"APP_LLM_API_KEY": "..."}, "OPENAI_API_KEY"),
        ("rerank", separate | {"APP_RERANK_API_KEY": "..."}, "APP_RERANK_API_KEY"),
    )
    for purpose, environment, name in cases:
        with pytest.raises(ProviderConfigurationError, match=name) as raised:
            load_local_model_config(purpose, environment)  # type: ignore[arg-type]
        assert "placeholder" in str(raised.value)
    llm = {"APP_LLM_BASE_URL": "https://gateway.example/v1", "APP_LLM_MODEL": "m"}
    with pytest.raises(ProviderConfigurationError, match=r"OPENAI_API_KEY.*placeholder"):
        load_llm_config(llm | {"APP_LLM_API_KEY": "..."})


def test_real_looking_keys_and_an_unset_key_are_not_mistaken_for_the_placeholder() -> None:
    for real in ("sk.abc.def", "k", "..a", "sk-..."):
        config = load_local_model_config("embedding", _GATEWAY | {"APP_EMBEDDING_API_KEY": real})
        assert config.api_key.get_secret_value() == real
    # Unset (or blank) still falls back to the LLM key on the gateway.
    for unset in ({}, {"APP_EMBEDDING_API_KEY": ""}):
        fallback = load_local_model_config("embedding", _GATEWAY | unset)
        assert fallback.api_key.get_secret_value() == "cloud-secret"


def test_local_models_require_independent_redacted_api_keys() -> None:
    environment = {
        "APP_EMBEDDING_BASE_URL": "http://127.0.0.1:28002",
        "APP_EMBEDDING_MODEL": "embedding-model",
        "APP_EMBEDDING_API_KEY": "embedding-secret",
        "APP_RERANK_BASE_URL": "http://127.0.0.1:28001",
        "APP_RERANK_MODEL": "rerank-model",
        "APP_RERANK_API_KEY": "rerank-secret",
        "APP_LLM_API_KEY": "cloud-secret",
    }

    embedding = load_local_model_config("embedding", environment)
    rerank = load_local_model_config("rerank", environment)

    assert embedding.api_key.get_secret_value() == "embedding-secret"
    assert rerank.api_key.get_secret_value() == "rerank-secret"
    assert "embedding-secret" not in repr(embedding)
    assert "rerank-secret" not in repr(rerank)
    with pytest.raises(ProviderConfigurationError, match="APP_EMBEDDING_API_KEY"):
        load_local_model_config("embedding", environment | {"APP_EMBEDDING_API_KEY": ""})


def test_local_model_http_endpoints_must_be_loopback() -> None:
    with pytest.raises(ProviderConfigurationError, match="APP_EMBEDDING_BASE_URL"):
        load_local_model_config(
            "embedding",
            {
                "APP_EMBEDDING_BASE_URL": "http://service.internal:28002",
                "APP_EMBEDDING_MODEL": "embedding-model",
                "APP_EMBEDDING_API_KEY": "embedding-secret",
            },
        )


def test_smoke_sends_one_bounded_request_and_returns_no_body_or_secret() -> None:
    requests: list[dict[str, object]] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        requests.append({"url": url, "payload": json.loads(payload), "timeout": timeout})
        assert api_key == "test-secret"
        return b'{"model":"configured-model","choices":[{"message":{"role":"assistant","content":"OK"},"finish_reason":"stop"}]}'

    config = load_llm_config(
        {
            "APP_LLM_API_KEY": "test-secret",
            "APP_LLM_BASE_URL": "https://provider.example/v1",
            "APP_LLM_MODEL": "configured-model",
        }
    )
    result = OpenAICompatibleSmoke(config, sender=sender).run()
    assert result.ok
    assert len(requests) == 1
    assert requests[0]["timeout"] == 30.0
    assert requests[0]["payload"] == {
        "model": "configured-model",
        "messages": [{"role": "user", "content": "Reply with exactly OK."}],
        "max_completion_tokens": 16,
        "stream": False,
    }
    assert "test-secret" not in result.model_dump_json()
    assert "choices" not in result.model_dump_json()
