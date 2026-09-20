"""Explicit provider environment and a bounded, opt-in connectivity smoke.

This module does not read .env files, configure the offline runtime, or qualify
production chart understanding. Embedding/rerank never inherit LLM settings.
"""

import json
import os
from collections.abc import Mapping
from http.client import HTTPException, HTTPSConnection
from time import monotonic
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, TypeAdapter, ValidationError


class ProviderConfigurationError(ValueError):
    """A named provider setting is missing or invalid; never include its value."""


class ProviderRequestError(ValueError):
    """Sanitized provider failure; no response body, headers or key is retained."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        category: Literal[
            "http", "timeout", "connection", "response_limit", "invalid_response"
        ] = "invalid_response",
        exception_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.category = category
        self.exception_type = exception_type


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    api_key: SecretStr
    base_url: str
    model: str

    @property
    def chat_completions_url(self) -> str:
        base = self.base_url.rstrip("/")
        return base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")


class LocalModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    purpose: Literal["embedding", "rerank"]
    api_key: SecretStr
    base_url: str
    model: str


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value or "\n" in value or "\r" in value:
        raise ProviderConfigurationError(f"Missing or invalid environment setting: {name}")
    return value


def _base_url(value: str, *, name: str, https_only: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in ({"https"} if https_only else {"http", "https"})
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ProviderConfigurationError(f"Invalid service base URL: {name}")
    return value.rstrip("/")


def _loopback_base_url(value: str, *, name: str) -> str:
    base = _base_url(value, name=name)
    if urlsplit(base).hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ProviderConfigurationError(f"Invalid loopback service base URL: {name}")
    return base


def load_llm_config(environment: Mapping[str, str] | None = None) -> LLMConfig:
    env = os.environ if environment is None else environment
    key = _required(env, "OPENAI_API_KEY")
    base = _base_url(_required(env, "OPENAI_BASE_URL"), name="OPENAI_BASE_URL", https_only=True)
    model = _required(env, "OPENAI_MODEL")
    return LLMConfig(api_key=SecretStr(key), base_url=base, model=model)


def load_local_model_config(
    purpose: Literal["embedding", "rerank"],
    environment: Mapping[str, str] | None = None,
) -> LocalModelConfig:
    env = os.environ if environment is None else environment
    prefix = "EMBEDDING" if purpose == "embedding" else "RERANK"
    base = _loopback_base_url(_required(env, f"{prefix}_BASE_URL"), name=f"{prefix}_BASE_URL")
    model = _required(env, f"{prefix}_MODEL")
    key = _required(env, f"{prefix}_API_KEY")
    return LocalModelConfig(purpose=purpose, api_key=SecretStr(key), base_url=base, model=model)


@runtime_checkable
class SmokeSender(Protocol):
    def __call__(self, url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes: ...


def _send_once(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
    parsed = urlsplit(url)
    connection = HTTPSConnection(parsed.netloc, timeout=timeout)
    try:
        connection.request(
            "POST",
            parsed.path,
            body=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ProviderRequestError(
                f"Provider returned HTTP {response.status}; no retry performed",
                status=response.status,
                category="http",
            )
        body = response.read(1_048_577)
        if len(body) > 1_048_576:
            raise ProviderRequestError(
                "Provider response exceeded the size limit", category="response_limit"
            )
        return body
    except TimeoutError:
        raise ProviderRequestError(
            "Provider connection timed out; no retry performed",
            category="timeout",
            exception_type="TimeoutError",
        ) from None
    except (OSError, HTTPException) as error:
        exception_type = type(error).__name__
        if exception_type not in {
            "SSLError",
            "SSLCertVerificationError",
            "ConnectionRefusedError",
            "ConnectionResetError",
            "RemoteDisconnected",
            "gaierror",
            "OSError",
            "HTTPException",
        }:
            exception_type = "HTTPException" if isinstance(error, HTTPException) else "OSError"
        raise ProviderRequestError(
            "Provider connection failed; no retry performed",
            category="connection",
            exception_type=exception_type,
        ) from None
    finally:
        connection.close()


class SmokeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    execution_mode: Literal["live-smoke"] = "live-smoke"
    ok: bool
    model: str
    endpoint: str
    elapsed_ms: int


_OBJECT = TypeAdapter(dict[str, object])
_CHOICES = TypeAdapter(list[dict[str, object]])


class OpenAICompatibleSmoke:
    def __init__(self, config: LLMConfig, *, sender: SmokeSender | None = None) -> None:
        self._config = config
        self._sender = sender if sender is not None else _send_once

    def run(self) -> SmokeResult:
        payload = json.dumps(
            {
                "model": self._config.model,
                "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                "max_completion_tokens": 16,
                "stream": False,
            }
        ).encode()
        started = monotonic()
        raw = self._sender(
            self._config.chat_completions_url,
            api_key=self._config.api_key.get_secret_value(),
            payload=payload,
            timeout=30.0,
        )
        try:
            response = _OBJECT.validate_json(raw)
            choices = _CHOICES.validate_python(response.get("choices"))
            if len(choices) != 1:
                raise ProviderRequestError("Provider returned an unexpected choice count")
            message = _OBJECT.validate_python(choices[0].get("message"))
            text = message.get("content")
            if (
                not isinstance(text, str)
                or text.strip() != "OK"
                or choices[0].get("finish_reason") != "stop"
            ):
                raise ProviderRequestError(
                    "Provider responded but did not satisfy the bounded smoke contract"
                )
        except ValidationError:
            raise ProviderRequestError("Provider returned an invalid completion schema") from None
        return SmokeResult(
            ok=True,
            model=self._config.model,
            endpoint=self._config.chat_completions_url,
            elapsed_ms=round((monotonic() - started) * 1000),
        )
