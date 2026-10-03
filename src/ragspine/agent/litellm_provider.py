"""LiteLLMProvider：经 litellm 接入 OpenAI 兼容的各家模型（不自写 HTTP 层）。

实现 corespine `LLMProvider.chat(messages, *, tools=None) -> ChatCompletion`（OpenAI 形状）与可选的
`StreamingProvider.chat_stream`。模型名用 litellm 写法：`deepseek/deepseek-chat`（默认，评测求快求省）、
`openai/<model>`（配 api_base 接任意 OpenAI 兼容网关 / vLLM）、`azure/<deployment>`、`ollama/<model>`。
key 由 litellm 按厂商读环境变量（DEEPSEEK_API_KEY / OPENAI_API_KEY / AZURE_API_KEY …），也可显式传 api_key。

- tool calling：litellm 原生 OpenAI 格式，请求 tools 原样透传，响应的 tool_calls 映射成 corespine ToolCall
  （与 AnthropicProvider / MockProvider 的输出同形）。
- 图片：`supports_image_input` 由构造参数显式声明（默认 False），**不**用 `litellm.supports_vision` 自动判断——
  它依赖 litellm 的模型表，`openai/<自部署模型>` 这类查不到的一律报不支持、表过期时还会误判，且判断要先 import
  litellm（破坏惰性加载）。显式开关确定、可复现。开启时图片部件转成 OpenAI `image_url`（base64 data URL），
  图前插一个写着文件名的文本部件，对上 prompt 里的 `图：pN.png`。
- 惰性：import 本模块不加载 litellm，首次调用时才 import；未安装时报 ImportError 并提示装 `ragspine[litellm]`。
- 韧性：超时 `timeout`、重试交给 litellm 自带 `num_retries`（不自己再包一层）；litellm 的网络 / API / 超时异常
  （`LITELLM_EXCEPTION_TYPES`）归一到 ProviderError，程序错误照常抛出；`max_concurrency` 限制同时在途请求数。
- 截断：`finish_reason="length"`（文本或 tool_call 被截断）时经 `agent/truncation.retry_on_truncation` 把
  `max_tokens` 翻倍重试（初始预算取构造参数 `max_tokens`，未设则取本次 `usage.completion_tokens`），重试请求带
  `reasoning_effort="none"` + `drop_params=True` 关掉思考（litellm 的通用写法，不支持的模型由 litellm 丢弃该参数）；
  网关仍以 BadRequest 拒绝时去掉这两个参数再发一次，并记住本实例不再带。仍截断则抛 `TruncatedOutputError`。
  `chat_stream` 不重试（delta 已经发出，无法撤回）。
- 隐私：加载时关掉 litellm 遥测、清空全部回调、`turn_off_message_logging`、`suppress_debug_info`，并用本地模型
  价格表（`LITELLM_LOCAL_MODEL_COST_MAP=True`，import 不联网）；本 provider 自身不打日志、不写 trace——请求 trace
  由编排层记计数与耗时。
"""

import base64
import importlib
import mimetypes
import os
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from corespine import (
    ChatCompletion,
    Choice,
    FunctionCall,
    ProviderError,
    ResponseMessage,
    ToolCall,
    Usage,
)

from ragspine.agent.llm_provider import IMAGE_PART_TYPE, split_message_content
from ragspine.agent.truncation import TruncationPolicy, retry_on_truncation
from ragspine.common.observability.llm_calls import (
    instrument_llm_call,
    note_attempt,
    note_reasoning_disabled,
)

DEFAULT_LITELLM_MODEL = "deepseek/deepseek-chat"
DEFAULT_LITELLM_TIMEOUT_S = 120.0
DEFAULT_LITELLM_RETRIES = 2
DEFAULT_LITELLM_CONCURRENCY = 8

# 与 ServiceConfig 字段 litellm_model / litellm_api_base / litellm_image_input 的 env 键同名。
ENV_MODEL = "RAGSPINE_LITELLM_MODEL"
ENV_API_BASE = "RAGSPINE_LITELLM_API_BASE"
ENV_IMAGE_INPUT = "RAGSPINE_LITELLM_IMAGE_INPUT"

_TRUE = {"1", "true", "yes", "on"}

# 截断重试时关掉思考：litellm 的通用参数值（deepseek → thinking disabled，OpenAI 系 → reasoning_effort=none）。
_REASONING_OFF = {"reasoning_effort": "none", "drop_params": True}

_litellm: Any = None
_load_lock = threading.Lock()


def _load_litellm() -> Any:
    """首次调用时 import litellm 并关掉它的遥测 / 回调 / 消息日志；之后复用同一模块。"""
    global _litellm
    with _load_lock:
        if _litellm is None:
            # 用随包的模型价格表，import 时不去 GitHub 拉取（离线、确定）。
            os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
            try:
                module = importlib.import_module("litellm")
            except ImportError as exc:
                raise ImportError(
                    "未安装 litellm：pip install 'ragspine[litellm]'；离线场景请用 --provider mock。"
                ) from exc
            _silence(module)
            _litellm = module
    return _litellm


def _silence(module: Any) -> None:
    """隐私：请求 / 响应内容不进 litellm 的遥测、回调与日志。"""
    module.telemetry = False
    module.turn_off_message_logging = True
    module.suppress_debug_info = True
    module.log_raw_request_response = False
    for name in (
        "callbacks",
        "input_callback",
        "success_callback",
        "failure_callback",
        "service_callback",
        "audit_log_callbacks",
        "_async_success_callback",
        "_async_failure_callback",
    ):
        if hasattr(module, name):
            setattr(module, name, [])


def _image_url_part(part: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(str(part.get("path") or ""))
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def _openai_content(content: object, *, images: bool) -> object:
    """部件列表 → OpenAI content。读图时图片转 image_url；不读图时拼成纯文本。字符串 / None 原样。"""
    if not isinstance(content, list):
        return content
    if not images:
        return split_message_content(content)[0]
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == IMAGE_PART_TYPE:
            if part.get("name"):
                parts.append({"type": "text", "text": str(part["name"])})
            parts.append(_image_url_part(part))
        elif part.get("type") == "text":
            parts.append({"type": "text", "text": str(part.get("text") or "")})
    return parts


def _usage(resp: Any) -> Usage | None:
    u = getattr(resp, "usage", None)
    if u is None:
        return None
    prompt = int(getattr(u, "prompt_tokens", 0) or 0)
    completion = int(getattr(u, "completion_tokens", 0) or 0)
    return Usage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )


def _tool_calls(message: Any) -> tuple[ToolCall, ...]:
    return tuple(
        ToolCall(
            id=str(tc.id),
            function=FunctionCall(
                name=str(tc.function.name), arguments=tc.function.arguments or "{}"
            ),
        )
        for tc in getattr(message, "tool_calls", None) or ()
    )


class LiteLLMProvider:
    """litellm 驱动的 OpenAI 兼容 provider。能力边界见模块 docstring。"""

    def __init__(
        self,
        model: str = DEFAULT_LITELLM_MODEL,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        image_input: bool = False,
        timeout: float = DEFAULT_LITELLM_TIMEOUT_S,
        num_retries: int = DEFAULT_LITELLM_RETRIES,
        max_concurrency: int = DEFAULT_LITELLM_CONCURRENCY,
        max_tokens: int | None = None,
        truncation: TruncationPolicy | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        self.model = model
        self.api_base = api_base
        self._api_key = api_key
        self.supports_image_input = image_input
        self.timeout = timeout
        self.num_retries = num_retries
        self.max_tokens = max_tokens
        self.truncation = truncation or TruncationPolicy.from_env()
        # 网关拒绝 reasoning 关闭参数（BadRequest）后置 False，之后的截断重试不再带。
        self._reasoning_off_ok = True
        self._slots = threading.BoundedSemaphore(max_concurrency)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LiteLLMProvider":
        """按 RAGSPINE_LITELLM_MODEL / _API_BASE / _IMAGE_INPUT 装配（缺省模型 deepseek/deepseek-chat）。"""
        env = os.environ if env is None else env
        return cls(
            model=env.get(ENV_MODEL) or DEFAULT_LITELLM_MODEL,
            api_base=env.get(ENV_API_BASE) or None,
            image_input=env.get(ENV_IMAGE_INPUT, "").strip().lower() in _TRUE,
        )

    def _request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    **m,
                    "content": _openai_content(m.get("content"), images=self.supports_image_input),
                }
                for m in messages
            ],
            "timeout": self.timeout,
            "num_retries": self.num_retries,
        }
        if tools:
            kwargs["tools"] = tools
        if self.api_base is not None:
            kwargs["api_base"] = self.api_base
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        return kwargs

    def _completion(self, litellm: Any, kwargs: dict[str, Any]) -> Any:
        with self._slots:
            try:
                return litellm.completion(**kwargs)
            except tuple(litellm.LITELLM_EXCEPTION_TYPES) as exc:
                raise ProviderError(f"litellm 调用失败（{self.model}）：{exc}") from exc

    def _retry_completion(self, litellm: Any, kwargs: dict[str, Any], budget: int) -> Any:
        """截断重试：放大 max_tokens 并关掉 reasoning；网关拒绝关闭参数时去掉它再发一次。"""
        kwargs = {**kwargs, "max_tokens": budget}
        if not self._reasoning_off_ok:
            return self._completion(litellm, kwargs)
        with self._slots:
            try:
                resp = litellm.completion(**kwargs, **_REASONING_OFF)
            except litellm.BadRequestError:
                self._reasoning_off_ok = False
            except tuple(litellm.LITELLM_EXCEPTION_TYPES) as exc:
                raise ProviderError(f"litellm 调用失败（{self.model}）：{exc}") from exc
            else:
                note_reasoning_disabled()
                return resp
        note_attempt()
        note_reasoning_disabled(False)
        return self._completion(litellm, kwargs)

    @instrument_llm_call
    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        litellm = _load_litellm()
        kwargs = self._request(messages, tools)

        def attempt(budget: int | None) -> tuple[Any, int | None]:
            if budget is None:
                resp = self._completion(litellm, kwargs)
            else:
                resp = self._retry_completion(litellm, kwargs, budget)
            if resp.choices[0].finish_reason != "length":
                return resp, None
            usage = getattr(resp, "usage", None)
            used = int(getattr(usage, "completion_tokens", 0) or 0)
            return resp, budget or self.max_tokens or used

        resp = retry_on_truncation(attempt, self.truncation, what=f"litellm（{self.model}）")
        choice = resp.choices[0]
        tool_calls = _tool_calls(choice.message)
        message = ResponseMessage(
            role="assistant",
            content=choice.message.content or None,
            tool_calls=tool_calls or None,
        )
        finish = choice.finish_reason or ("tool_calls" if tool_calls else "stop")
        return ChatCompletion(
            choices=(Choice(index=0, message=message, finish_reason=str(finish)),),
            usage=_usage(resp),
            model=str(getattr(resp, "model", None) or self.model),
            id=str(getattr(resp, "id", None) or ""),
        )

    def chat_stream(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> Iterator[str]:
        """流式产出文本 delta（tool_call 增量不产出）。"""
        litellm = _load_litellm()
        kwargs = self._request(messages, tools)
        errors = tuple(litellm.LITELLM_EXCEPTION_TYPES)
        with self._slots:
            try:
                for chunk in litellm.completion(**kwargs, stream=True):
                    if not chunk.choices:
                        continue
                    text = getattr(chunk.choices[0].delta, "content", None)
                    if text:
                        yield str(text)
            except errors as exc:
                raise ProviderError(f"litellm 流式调用失败（{self.model}）：{exc}") from exc
