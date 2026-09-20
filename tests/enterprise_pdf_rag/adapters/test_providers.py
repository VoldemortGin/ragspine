"""Provider configuration is explicit; unit tests never call a live endpoint."""

import json

import pytest

from enterprise_pdf_rag.adapters.providers import (
    OpenAICompatibleSmoke,
    ProviderConfigurationError,
    load_llm_config,
    load_local_model_config,
)


def test_missing_model_is_an_error_and_secrets_are_redacted() -> None:
    environment = {
        "OPENAI_API_KEY": "test-secret",
        "OPENAI_BASE_URL": "https://provider.example",
    }
    with pytest.raises(ProviderConfigurationError, match="OPENAI_MODEL"):
        load_llm_config(environment)
    config = load_llm_config({**environment, "OPENAI_MODEL": "configured-model"})
    assert "test-secret" not in repr(config)
    assert config.chat_completions_url == "https://provider.example/v1/chat/completions"
    assert (
        load_llm_config(
            {
                **environment,
                "OPENAI_BASE_URL": "https://provider.example/v1/",
                "OPENAI_MODEL": "configured-model",
            }
        ).chat_completions_url
        == config.chat_completions_url
    )


def test_local_models_never_inherit_the_cloud_llm_configuration() -> None:
    with pytest.raises(ProviderConfigurationError, match="EMBEDDING_BASE_URL"):
        load_local_model_config(
            "embedding",
            {"OPENAI_BASE_URL": "https://cloud.example", "OPENAI_MODEL": "cloud-model"},
        )
    with pytest.raises(ProviderConfigurationError, match="RERANK_BASE_URL"):
        load_local_model_config("rerank", {})


def test_local_models_require_independent_redacted_api_keys() -> None:
    environment = {
        "EMBEDDING_BASE_URL": "http://127.0.0.1:28002",
        "EMBEDDING_MODEL": "embedding-model",
        "EMBEDDING_API_KEY": "embedding-secret",
        "RERANK_BASE_URL": "http://127.0.0.1:28001",
        "RERANK_MODEL": "rerank-model",
        "RERANK_API_KEY": "rerank-secret",
        "OPENAI_API_KEY": "cloud-secret",
    }

    embedding = load_local_model_config("embedding", environment)
    rerank = load_local_model_config("rerank", environment)

    assert embedding.api_key.get_secret_value() == "embedding-secret"
    assert rerank.api_key.get_secret_value() == "rerank-secret"
    assert "embedding-secret" not in repr(embedding)
    assert "rerank-secret" not in repr(rerank)
    with pytest.raises(ProviderConfigurationError, match="EMBEDDING_API_KEY"):
        load_local_model_config("embedding", environment | {"EMBEDDING_API_KEY": ""})


def test_local_model_http_endpoints_must_be_loopback() -> None:
    with pytest.raises(ProviderConfigurationError, match="EMBEDDING_BASE_URL"):
        load_local_model_config(
            "embedding",
            {
                "EMBEDDING_BASE_URL": "http://service.internal:28002",
                "EMBEDDING_MODEL": "embedding-model",
                "EMBEDDING_API_KEY": "embedding-secret",
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
            "OPENAI_API_KEY": "test-secret",
            "OPENAI_BASE_URL": "https://provider.example/v1",
            "OPENAI_MODEL": "configured-model",
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
