"""LLM provider 适配:AnthropicProvider（真实调用）+ MockProvider（离线确定性脚本）。

迁到 corespine 的 chat 缝（OpenAI chat-completions 规范）：provider 实现 corespine 的
`LLMProvider.chat(messages, *, tools=None) -> ChatCompletion`（OpenAI 形状）。AnthropicProvider
把 OpenAI 形状 messages/tools 在内部转成 Anthropic Messages API、再把响应转回 OpenAI ChatCompletion
（与 spineagent 同一适配模式）。MockProvider 是离线确定性脚本：零网络、零 key，按规则模拟
query_metric 的 tool-use 循环与三态最终回答，绝不编造数字。

设计约束：
- anthropic SDK 延迟 import：不装 SDK 也能 import 本模块、跑 mock 全链路。
- base_url 可覆盖：适配企业网关（GenAI Hub）转发 Anthropic API。
- SDK 自带 max_retries（撞 429 被动退避）做兜底；主动 TPM 限流由 corespine RateLimitedProvider
  在 build_provider 处包装（见 service/config.py），两层互补。
"""

import json
from datetime import date
from typing import Any

from corespine import (
    ChatCompletion,
    Choice,
    FunctionCall,
    LLMProvider,
    ProviderError,
    ResponseMessage,
    ToolCall,
    Usage,
)

from ragspine.agent.intent import parse_intent

# 默认模型名（唯一出处，改这里即全局生效）
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"

# 叙事合成提示的固定前缀：编排层与 MockProvider 共同约定
NARRATIVE_PROMPT_PREFIX = "请仅基于以下检索片段回答问题，不得引入片段之外的信息"

# Anthropic stop_reason → OpenAI finish_reason。
_ANTHROPIC_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


# re-export corespine 的 LLMProvider 协议与 ProviderError 异常；ragspine 不再自定义。
__all__ = ["LLMProvider", "AnthropicProvider", "MockProvider", "ProviderError",
           "DEFAULT_ANTHROPIC_MODEL", "NARRATIVE_PROMPT_PREFIX"]


def _openai_tool_to_anthropic(tool: dict[str, Any]) -> dict[str, Any]:
    """OpenAI function-tool 形状 → Anthropic 工具形状（name/description/input_schema）。"""
    fn = tool.get("function", tool)
    return {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


def _openai_messages_to_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI messages → (system 字符串, Anthropic messages)。

    system 角色合并进 system 参数；tool 角色转 tool_result block；assistant 的 tool_calls 转
    tool_use block——让多轮 function-calling 的工具结果原样喂回 Anthropic。
    """
    system_parts: list[str] = []
    convo: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            system_parts.append(str(content or ""))
        elif role == "tool":
            convo.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": str(content or ""),
                }],
            })
        elif role == "assistant" and m.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for tc in m["tool_calls"]:
                fn = tc["function"]
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": fn["name"],
                    "input": json.loads(fn.get("arguments") or "{}"),
                })
            convo.append({"role": "assistant", "content": blocks})
        else:
            convo.append({
                "role": "assistant" if role == "assistant" else "user",
                "content": str(content or ""),
            })
    return "\n".join(system_parts), convo


class AnthropicProvider:
    """真实 Claude 调用（Messages API + tool use），实现 corespine LLMProvider.chat。SDK 延迟 import。"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        base_url: str | None = None,
        max_tokens: int = 16000,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "未安装 anthropic SDK：pip install 'ragspine[llm]' 或 "
                "pip install anthropic；离线场景请用 --provider mock。"
            ) from exc

        # 超时/重试透传给 SDK：429/5xx/超时由 SDK 原生指数退避处理（被动兜底，与
        # corespine RateLimitedProvider 的主动 TPM 限流互补）。
        client_kwargs: dict[str, object] = {"timeout": timeout, "max_retries": max_retries}
        if api_key is not None:
            client_kwargs["api_key"] = api_key
        if base_url is not None:
            client_kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**client_kwargs)
        self.model = model
        self.max_tokens = max_tokens

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        system, convo = _openai_messages_to_anthropic(messages)
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = [_openai_tool_to_anthropic(t) for t in tools]
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=convo,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 — SDK 网络/API 异常归一到 ProviderError
            raise ProviderError(f"Anthropic 调用失败：{exc}") from exc

        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
        tool_calls = tuple(
            ToolCall(id=b.id, function=FunctionCall(name=b.name, arguments=json.dumps(dict(b.input))))
            for b in resp.content
            if getattr(b, "type", None) == "tool_use"
        )
        message = ResponseMessage(
            role="assistant", content=(text or None), tool_calls=(tool_calls or None)
        )
        choice = Choice(
            index=0,
            message=message,
            finish_reason=_ANTHROPIC_FINISH.get(resp.stop_reason, "stop"),
        )
        usage: Usage | None = None
        u = getattr(resp, "usage", None)
        if u is not None:
            it = getattr(u, "input_tokens", 0) or 0
            ot = getattr(u, "output_tokens", 0) or 0
            usage = Usage(prompt_tokens=it, completion_tokens=ot, total_tokens=it + ot)
        return ChatCompletion(
            choices=(choice,),
            usage=usage,
            model=getattr(resp, "model", self.model),
            id=getattr(resp, "id", ""),
        )


def _completion_text(text: str) -> ChatCompletion:
    """把一段纯文本包成 OpenAI ChatCompletion（无 tool_calls，finish_reason=stop）。"""
    msg = ResponseMessage(role="assistant", content=text)
    return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="stop"),))


def _period_param(period: tuple[str, str]) -> str:
    """意图槽位 (period_type, period) → query_metric 的 period 字符串参数。"""
    period_type, value = period
    return f"FY{value}" if period_type == "FY" else value


def _last_tool_result(messages: list[dict[str, Any]]) -> dict[str, object] | None:
    """从消息序列尾部找最近一条 tool 角色消息（OpenAI 形状），解析其 JSON 内容。"""
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            content = msg.get("content")
            try:
                return json.loads(content) if isinstance(content, str) else None
            except (TypeError, ValueError):
                return None
    return None


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """取最近一条 user 角色消息的文本内容（OpenAI 形状）。"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
    return ""


class MockProvider:
    """确定性脚本化 provider：零网络、零 key，实现 corespine chat，模拟 tool use 循环。

    第一轮（无 tool 结果）：用意图解析从用户话语产出 query_metric 的 tool_call（OpenAI 形状）；
    第二轮（有 tool 结果）：按 found / not_found / unrecognized 三态生成最终回答，found 时回答必带
    数值与血缘，not_found 时明确说查不到，绝不编造。
    """

    def __init__(self, reference_date: date | None = None) -> None:
        self.reference_date = reference_date

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        result = _last_tool_result(messages)
        if result is not None:
            return _completion_text(self._final_answer_text(result))

        text = _last_user_text(messages)
        if text.startswith(NARRATIVE_PROMPT_PREFIX):
            # 叙事合成：确定性地回显检索片段正文
            body = text[len(NARRATIVE_PROMPT_PREFIX):].strip()
            return _completion_text(f"基于检索到的资料：\n{body}")

        intent = parse_intent(text, reference_date=self.reference_date)
        if intent.metric and intent.entity and intent.period:
            call_input: dict[str, object] = {
                "metric": intent.metric,
                "entity": intent.entity,
                "period": _period_param(intent.period),
                "channel": intent.channel,
            }
            tc = ToolCall(
                id="toolu_mock_1",
                function=FunctionCall(
                    name="query_metric", arguments=json.dumps(call_input, ensure_ascii=False)
                ),
            )
            msg = ResponseMessage(role="assistant", content=None, tool_calls=(tc,))
            return ChatCompletion(
                choices=(Choice(index=0, message=msg, finish_reason="tool_calls"),)
            )

        return _completion_text("请补充想查询的指标/实体/期间，例如：香港去年REVENUE多少。")

    @staticmethod
    def _final_answer_text(result: dict[str, object]) -> str:
        status = result.get("status")
        if status == "found":
            raw_source = result.get("source", {})
            source = raw_source if isinstance(raw_source, dict) else {}
            # valid_as_of 存在时附「截至」业务时点；为 None 时文案字节级不变。
            valid_as_of = result.get("valid_as_of")
            asof = f" · 截至 {valid_as_of}" if valid_as_of else ""
            return (
                f"{result['entity']} {result['period_type']}{result['period']} "
                f"{result['metric_code']} 为 {result['value']:g} {result['unit']}"
                f"（来源：{source.get('doc')} · {source.get('locator')}{asof}）"
            )
        if status == "not_found":
            raw_norm = result.get("normalized", {})
            norm = raw_norm if isinstance(raw_norm, dict) else {}
            return (
                f"查不到：{norm.get('metric_code')} / {norm.get('entity')} / "
                f"{norm.get('period')}（{norm.get('channel')}）未在事实表中找到，"
                "不提供推测数字。"
            )
        if status == "unrecognized_param":
            return (
                f"无法识别参数 {result.get('param')}：'{result.get('raw')}'，"
                "请换一个说法或确认指标名称。"
            )
        return "工具返回了未知状态，无法作答。"
