"""Explicit provider environment and a bounded, opt-in connectivity smoke.

Settings come from an injected mapping or, by default, from ``configs.Settings``
(environment > project-root .env > yaml), read afresh on every call. This module does
not configure the offline runtime or qualify production chart understanding.
Embedding shares the LLM's OpenAI-compatible gateway unless ``APP_EMBEDDING_BASE_URL`` names a
separate loopback service; rerank never inherits LLM settings.
"""

import json
import math
import re
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from http.client import HTTPException, HTTPSConnection
from time import monotonic, time
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, TypeAdapter, ValidationError

from ragspine.common.evidence.configs import Settings


class ProviderConfigurationError(ValueError):
    """A named provider setting is missing or invalid; never include its value."""


class ProviderRequestError(ValueError):
    """Sanitized provider failure; no headers or key, and of a response body only ``param``.

    ``param`` / ``error_code`` are the OpenAI-style ``error.param`` / ``error.code`` of an
    HTTP 400 body, each charset- and length-checked; the provider's message and every other
    byte of the body are discarded unretained. Both are ``None`` for any other failure.
    ``retry_after`` is the wait (seconds) a non-200 response's ``Retry-After`` /
    ``retry-after-ms`` header asked for, else ``None`` (enterprise-pdf-rag ADR 0035).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        category: Literal[
            "http", "timeout", "connection", "response_limit", "invalid_response"
        ] = "invalid_response",
        exception_type: str | None = None,
        param: str | None = None,
        error_code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.category = category
        self.exception_type = exception_type
        self.param = param
        self.error_code = error_code
        self.retry_after = retry_after


# Greedy decoding. Two identical questions must produce one answer, and a cached answer must
# stay the answer the release was measured with; a provider default of 0.7 makes neither true.
# Sent on every completion unless configured otherwise, so it is part of the request fingerprint.
DETERMINISTIC_TEMPERATURE = 0.0
# The OPENAI_TEMPERATURE value that sends no temperature at all (the endpoint's own default).
TEMPERATURE_OMIT = "omit"


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    api_key: SecretStr
    base_url: str
    model: str
    # None sends no temperature field (``OPENAI_TEMPERATURE=omit``).
    temperature: float | None = DETERMINISTIC_TEMPERATURE

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


def _required(environment: Mapping[str, str], name: str, *, fallback: str | None = None) -> str:
    value = environment.get(name, "").strip()
    if not value or "\n" in value or "\r" in value:
        shown = name if fallback is None else f"{fallback} (or {name})"
        raise ProviderConfigurationError(f"Missing or invalid environment setting: {shown}")
    return value


_PLACEHOLDER_KEY = "..."  # the value .env.example ships for API-key lines


def _api_key(environment: Mapping[str, str], name: str, *, fallback: str | None = None) -> str:
    value = _required(environment, name, fallback=fallback)
    if value == _PLACEHOLDER_KEY:
        shown = name if fallback is None else f"{fallback} (or {name})"
        raise ProviderConfigurationError(
            f"Environment setting {shown} is still the .env.example placeholder; "
            "delete that line or set a real key"
        )
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


def _temperature(environment: Mapping[str, str]) -> float | None:
    """Unset / blank → greedy ``0.0``; ``omit`` → ``None`` (not sent); else a number in [0, 2]."""
    value = environment.get("APP_LLM_TEMPERATURE", "").strip()
    if not value:
        return DETERMINISTIC_TEMPERATURE
    if value.casefold() == TEMPERATURE_OMIT:
        return None
    try:
        number = float(value)
    except ValueError:
        number = math.nan
    if not 0.0 <= number <= 2.0:  # also rejects nan / inf
        raise ProviderConfigurationError(
            "Invalid environment setting: OPENAI_TEMPERATURE (or APP_LLM_TEMPERATURE); "
            f"use a number within [0, 2], or {TEMPERATURE_OMIT} to send no temperature"
        )
    return number


def load_llm_config(environment: Mapping[str, str] | None = None) -> LLMConfig:
    names = ("APP_LLM_API_KEY", "APP_LLM_BASE_URL", "APP_LLM_MODEL", "APP_LLM_TEMPERATURE")
    env = Settings().as_environment(names) if environment is None else environment
    # OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL are the preferred names; Settings already
    # folded them in under these APP_LLM_* keys (the child-process allowlist names). An injected
    # mapping is taken as given, so it only ever carries the APP_LLM_* keys.
    key = _api_key(env, "APP_LLM_API_KEY", fallback="OPENAI_API_KEY")
    base = _base_url(
        _required(env, "APP_LLM_BASE_URL", fallback="OPENAI_BASE_URL"),
        name="APP_LLM_BASE_URL",
        https_only=True,
    )
    model = _required(env, "APP_LLM_MODEL", fallback="OPENAI_MODEL")
    return LLMConfig(
        api_key=SecretStr(key), base_url=base, model=model, temperature=_temperature(env)
    )


_GATEWAY_MODEL_HINT = (
    "embedding shares the OPENAI_BASE_URL gateway; set OPENAI_EMBEDDING_MODEL, "
    "or set APP_EMBEDDING_* for a separate loopback service"
)


def _gateway_embedding_config(env: Mapping[str, str]) -> LocalModelConfig:
    model = env.get("APP_EMBEDDING_MODEL", "").strip()
    if not model or "\n" in model or "\r" in model:
        raise ProviderConfigurationError(
            f"Missing or invalid environment setting: OPENAI_EMBEDDING_MODEL ({_GATEWAY_MODEL_HINT})"
        )
    base = _base_url(
        _required(env, "APP_LLM_BASE_URL", fallback="OPENAI_BASE_URL"),
        name="OPENAI_BASE_URL (or APP_LLM_BASE_URL)",
        https_only=True,
    )
    key = (
        _api_key(env, "APP_EMBEDDING_API_KEY")
        if env.get("APP_EMBEDDING_API_KEY", "").strip()
        else _api_key(env, "APP_LLM_API_KEY", fallback="OPENAI_API_KEY")
    )
    return LocalModelConfig(purpose="embedding", api_key=SecretStr(key), base_url=base, model=model)


def load_local_model_config(
    purpose: Literal["embedding", "rerank"],
    environment: Mapping[str, str] | None = None,
) -> LocalModelConfig:
    """Embedding/rerank endpoint, key and model.

    Embedding without ``APP_EMBEDDING_BASE_URL`` (or with it set to the LLM gateway itself) uses
    the LLM gateway: ``APP_LLM_BASE_URL`` (https only), ``APP_EMBEDDING_API_KEY`` else
    ``APP_LLM_API_KEY``, and the required ``APP_EMBEDDING_MODEL`` (preferred name
    ``OPENAI_EMBEDDING_MODEL``). Any other ``APP_EMBEDDING_BASE_URL`` is a separate service that
    must be loopback with its own model and key. Rerank has no gateway fallback.
    """
    prefix = "APP_EMBEDDING" if purpose == "embedding" else "APP_RERANK"
    names: tuple[str, ...] = (f"{prefix}_BASE_URL", f"{prefix}_MODEL", f"{prefix}_API_KEY")
    if purpose == "embedding":
        names += ("APP_LLM_BASE_URL", "APP_LLM_API_KEY")
    env = Settings().as_environment(names) if environment is None else environment
    if purpose == "embedding":
        explicit = env.get("APP_EMBEDDING_BASE_URL", "").strip().rstrip("/")
        gateway = env.get("APP_LLM_BASE_URL", "").strip().rstrip("/")
        if not explicit or explicit == gateway:
            return _gateway_embedding_config(env)
    try:
        base = _loopback_base_url(_required(env, f"{prefix}_BASE_URL"), name=f"{prefix}_BASE_URL")
    except ProviderConfigurationError as error:
        if purpose == "rerank":
            raise ProviderConfigurationError(
                f"{error} (rerank has no gateway fallback; it needs a separate loopback service)"
            ) from None
        raise
    model = _required(env, f"{prefix}_MODEL")
    key = _api_key(env, f"{prefix}_API_KEY")
    return LocalModelConfig(purpose=purpose, api_key=SecretStr(key), base_url=base, model=model)


@runtime_checkable
class SmokeSender(Protocol):
    def __call__(self, url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes: ...


ERROR_BODY_LIMIT = 4096
_ERROR_FIELD = re.compile(r"[A-Za-z0-9_.\[\]-]{1,64}")


def _error_field(value: object) -> str | None:
    return value if isinstance(value, str) and _ERROR_FIELD.fullmatch(value) else None


@runtime_checkable
class _Readable(Protocol):
    def read(self, amount: int, /) -> bytes: ...


def _rejected_field(response: _Readable) -> tuple[str | None, str | None]:
    """``(error.param, error.code)`` of a bounded 400 body; nothing else is kept.

    The body is read once, at most ``ERROR_BODY_LIMIT`` bytes, and dropped here: an oversized,
    non-JSON or differently shaped body yields ``(None, None)``, as does a field outside the
    allowed charset / length. The provider's ``message`` is never looked at.
    """
    try:
        raw = response.read(ERROR_BODY_LIMIT + 1)
        if len(raw) > ERROR_BODY_LIMIT:
            return None, None
        body = json.loads(raw)
    except (OSError, HTTPException, ValueError, RecursionError):
        return None, None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None, None
    return _error_field(error.get("param")), _error_field(error.get("code"))


def retry_after_seconds(headers: Callable[[str], str | None]) -> float | None:
    """The wait a response asks for: ``retry-after-ms`` (Azure OpenAI), else ``Retry-After``
    as delta-seconds or an HTTP date. Malformed, negative or absent → ``None``. Only these two
    header values are read; nothing of the body."""
    milliseconds = (headers("retry-after-ms") or "").strip()
    if milliseconds:
        try:
            value = float(milliseconds) / 1000
        except ValueError:
            value = math.nan
        if math.isfinite(value) and value >= 0:
            return value
    raw = (headers("retry-after") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, when.timestamp() - time())


def _retry_after(response: object) -> float | None:
    """The ``Retry-After`` wait of a response, through its ``getheader`` when it has one."""
    getheader = getattr(response, "getheader", None)
    if not callable(getheader):
        return None

    def header(name: str) -> str | None:
        value = getheader(name)
        return value if isinstance(value, str) else None

    try:
        return retry_after_seconds(header)
    except (OSError, HTTPException, ValueError, TypeError):
        return None


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
            # Only a 400 names a request field; every other status stays unread. Of the
            # headers only the requested wait is read (ADR 0035).
            param, code = _rejected_field(response) if response.status == 400 else (None, None)
            raise ProviderRequestError(
                f"Provider returned HTTP {response.status}; no retry performed",
                status=response.status,
                category="http",
                param=param,
                error_code=code,
                retry_after=_retry_after(response),
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
