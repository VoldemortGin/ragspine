"""OpenAICompatProvider：直连 OpenAI 风格 `/v1/chat/completions` 的 provider（标准库 HTTP，不引入 openai SDK）。

实现 corespine `LLMProvider.chat(messages, *, tools=None) -> ChatCompletion`。传输与配置全部复用证据链那条线：
`LLMConfig`（`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`（`APP_LLM_*` 为别名），base URL 必须 https）、
`load_llm_config()`、`_send_once`（`http.client`，一次请求，非 200 抛 `ProviderRequestError`）；`sender` 可注入
（`SmokeSender` 协议），测试经它离线运行。

- 不重试：与 `_send_once` 语义一致，一次 `chat` 至多一个 HTTP 请求（429 / 5xx / 超时都直接上抛）；
  不做截断重试——`finish_reason="length"` 直接抛 `TruncatedOutputError`，半截结果绝不返回（见 agent/CLAUDE.md）。
- 消息 / 工具：ragspine 的 messages 与 tools 本就是 OpenAI 形状，原样发出；仅 content 部件列表经
  `litellm_provider._openai_content` 转换（该函数不依赖 litellm，import 本模块不会加载 litellm）。
- 图片：`supports_image_input` 由构造参数 `image_input` 显式声明（默认 False，同 LiteLLMProvider）；开启时
  `{"type":"image","path"}` 转成 base64 `image_url`，关闭时部件列表拍平成文本。本地路径不外发。
- 错误：`ProviderRequestError`（HTTP / 超时 / 连接 / 超限，信息已脱敏）与畸形响应一律归一为 `ProviderError`，
  程序错误（KeyError / TypeError …）照常传播。key 不进异常信息，也不进 trace。
- 隐私：本 provider 不打日志、不写 trace；`chat` 挂 `instrument_llm_call`（ADR 0028），只记计数 / 耗时。
"""

import json
from collections.abc import Mapping
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

from ragspine.agent.litellm_provider import _openai_content
from ragspine.agent.truncation import TruncatedOutputError
from ragspine.common.evidence.providers.providers import (
    LLMConfig,
    ProviderRequestError,
    SmokeSender,
    _send_once,
)
from ragspine.common.observability.llm_calls import instrument_llm_call, note_truncated

DEFAULT_OPENAI_COMPAT_TIMEOUT_S = 120.0


def _malformed(what: str) -> ProviderError:
    return ProviderError(f"OpenAI 兼容服务返回了无法解析的响应：{what}")


def _int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _malformed(what)
    return value


def _parse_usage(raw: object) -> Usage | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise _malformed("usage 不是对象")
    prompt = _int(raw.get("prompt_tokens", 0) or 0, "usage.prompt_tokens")
    completion = _int(raw.get("completion_tokens", 0) or 0, "usage.completion_tokens")
    return Usage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )


def _parse_tool_calls(raw: object) -> tuple[ToolCall, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _malformed("tool_calls 不是数组")
    calls: list[ToolCall] = []
    for item in raw:
        fn = item.get("function") if isinstance(item, Mapping) else None
        if not isinstance(fn, Mapping) or not isinstance(fn.get("name"), str):
            raise _malformed("tool_call 缺少 function.name")
        arguments = fn.get("arguments") or "{}"
        if not isinstance(arguments, str) or not item.get("id"):
            raise _malformed("tool_call 缺少 id 或 arguments 不是字符串")
        calls.append(
            ToolCall(
                id=str(item["id"]),
                function=FunctionCall(name=fn["name"], arguments=arguments),
            )
        )
    return tuple(calls)


class OpenAICompatProvider:
    """OpenAI 兼容 chat-completions provider。能力边界见模块 docstring。"""

    def __init__(
        self,
        config: LLMConfig,
        *,
        timeout: float = DEFAULT_OPENAI_COMPAT_TIMEOUT_S,
        max_tokens: int | None = None,
        image_input: bool = False,
        sender: SmokeSender | None = None,
    ) -> None:
        self.config = config
        self.model = config.model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.supports_image_input = image_input
        self._sender: SmokeSender = sender if sender is not None else _send_once

    def _payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> bytes:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    **m,
                    "content": _openai_content(m.get("content"), images=self.supports_image_input),
                }
                for m in messages
            ],
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    @instrument_llm_call
    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        try:
            raw = self._sender(
                self.config.chat_completions_url,
                api_key=self.config.api_key.get_secret_value(),
                payload=self._payload(messages, tools),
                timeout=self.timeout,
            )
        except ProviderRequestError as exc:
            raise ProviderError(
                f"OpenAI 兼容服务调用失败（{self.model}，{exc.category}"
                f"{'' if exc.status is None else f' HTTP {exc.status}'}）：{exc}"
            ) from exc
        return self._parse(raw)

    def _parse(self, raw: bytes) -> ChatCompletion:
        try:
            data = json.loads(raw)
        except ValueError as exc:  # JSONDecodeError / UnicodeDecodeError
            raise _malformed("不是合法 JSON") from exc
        choices = data.get("choices") if isinstance(data, Mapping) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise _malformed("缺少 choices")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise _malformed("缺少 message")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise _malformed("message.content 不是字符串")
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
        finish = str(choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop"))
        if finish == "length":
            note_truncated()
            raise TruncatedOutputError(f"OpenAI 兼容服务（{self.model}）输出被截断（未重试）")
        return ChatCompletion(
            choices=(
                Choice(
                    index=0,
                    message=ResponseMessage(
                        role="assistant",
                        content=content or None,
                        tool_calls=tool_calls or None,
                    ),
                    finish_reason=finish,
                ),
            ),
            usage=_parse_usage(data.get("usage")),
            model=str(data.get("model") or self.model),
            id=str(data.get("id") or ""),
        )
