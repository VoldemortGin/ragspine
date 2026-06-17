"""LLM provider 抽象：AnthropicProvider（真实调用）+ MockProvider（离线确定性）。

设计约束：
- anthropic SDK 延迟 import：不装 SDK 也能 import 本模块、跑 mock 全链路。
- base_url 可覆盖：适配企业网关（GenAI Hub）转发 Anthropic API。
- 默认模型名集中在 DEFAULT_ANTHROPIC_MODEL 一处。
- 为将来 OpenAIProvider 留口：Provider 协议只约定 create_message 一个方法，
  query_metric 的 OpenAI schema 已在 src/query_tools.py 备好。
"""

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from ragspine.agent.intent import parse_intent

# 默认模型名（唯一出处，改这里即全局生效）
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"

# 叙事合成提示的固定前缀：编排层与 MockProvider 共同约定
NARRATIVE_PROMPT_PREFIX = "请仅基于以下检索片段回答问题，不得引入片段之外的信息"


class ProviderError(Exception):
    """provider 调用失败的统一边界异常（网络/超时/API 错误归一到此）。

    只包裹 SDK 抛出的网络/API 异常；程序错误（KeyError/TypeError 等）不归此类，
    照常向上抛出，避免韧性兜底掩盖逻辑 bug。
    """


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str
    name: str
    input: dict


@dataclass
class ProviderResponse:
    """统一的响应载体：文本 + 工具调用 + 停止原因 + 原始内容（回传下一轮）。

    usage：本次调用的 token 用量 {"input_tokens", "output_tokens"}（用于成本/容量
    可观测）；provider 未回传时为 None（MockProvider 即 None）。
    """

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    raw_content: list = field(default_factory=list)
    usage: dict | None = None


class LLMProvider(Protocol):
    """provider 协议：发送 system+messages+tools，拿回统一响应。"""

    def create_message(
        self, *, system: str, messages: list[dict], tools: list[dict]
    ) -> ProviderResponse: ...


class AnthropicProvider:
    """真实 Claude 调用（Messages API + tool use）。SDK 延迟 import。"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        base_url: str | None = None,
        max_tokens: int = 16000,
        timeout: float = 30.0,
        max_retries: int = 2,
    ):
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "未安装 anthropic SDK：pip install 'ragspine[llm]' 或 "
                "pip install anthropic；离线场景请用 --provider mock。"
            ) from exc

        # 超时/重试透传给 SDK：429/5xx/超时由 SDK 原生指数退避处理，不自造退避。
        client_kwargs: dict = {"timeout": timeout, "max_retries": max_retries}
        if api_key is not None:
            client_kwargs["api_key"] = api_key
        if base_url is not None:
            client_kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**client_kwargs)
        self.model = model
        self.max_tokens = max_tokens

    def create_message(
        self, *, system: str, messages: list[dict], tools: list[dict]
    ) -> ProviderResponse:
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=tools,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 — SDK 网络/API 异常归一到 ProviderError
            # SDK 抛出的网络/超时/API 错误（重试耗尽后）统一边界为 ProviderError，
            # 保留 __cause__ 便于排障；程序错误不在此路径（resp 解析阶段才可能抛）。
            raise ProviderError(f"Anthropic 调用失败：{exc}") from exc

        texts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, input=dict(block.input))
                )
        return ProviderResponse(
            text="".join(texts),
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason,
            raw_content=resp.content,
            usage=_map_usage(getattr(resp, "usage", None)),
        )


def _map_usage(usage) -> dict | None:
    """SDK resp.usage → {"input_tokens", "output_tokens"}；属性缺失则 None（防御式）。"""
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None and output_tokens is None:
        return None
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _period_param(period: tuple[str, str]) -> str:
    """意图槽位 (period_type, period) → query_metric 的 period 字符串参数。"""
    period_type, value = period
    return f"FY{value}" if period_type == "FY" else value


def _last_tool_result(messages: list[dict]) -> dict | None:
    """从消息序列尾部找最近一条 tool_result，解析其 JSON 内容。"""
    for msg in reversed(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in reversed(content):
            if isinstance(item, dict) and item.get("type") == "tool_result":
                try:
                    return json.loads(item["content"])
                except (TypeError, ValueError, KeyError):
                    return None
    return None


def _last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            return msg["content"]
    return ""


class MockProvider:
    """确定性脚本化 provider：零网络、零 key，按规则模拟 tool use 循环。

    第一轮（无 tool_result）：用意图解析从用户话语产出 query_metric 调用；
    第二轮（有 tool_result）：按 found / not_found / unrecognized 三态生成最终回答，
    found 时回答必带数值与血缘，not_found 时明确说查不到，绝不编造。
    """

    def __init__(self, reference_date: date | None = None):
        self.reference_date = reference_date

    def create_message(
        self, *, system: str, messages: list[dict], tools: list[dict]
    ) -> ProviderResponse:
        result = _last_tool_result(messages)
        if result is not None:
            return self._final_answer(result)

        text = _last_user_text(messages)
        if text.startswith(NARRATIVE_PROMPT_PREFIX):
            # 叙事合成：确定性地回显检索片段正文
            body = text[len(NARRATIVE_PROMPT_PREFIX):].strip()
            answer = f"基于检索到的资料：\n{body}"
            return ProviderResponse(
                text=answer, raw_content=[{"type": "text", "text": answer}]
            )

        intent = parse_intent(text, reference_date=self.reference_date)
        if intent.metric and intent.entity and intent.period:
            call_input = {
                "metric": intent.metric,
                "entity": intent.entity,
                "period": _period_param(intent.period),
                "channel": intent.channel,
            }
            return ProviderResponse(
                text="",
                stop_reason="tool_use",
                tool_calls=[ToolCall(id="toolu_mock_1", name="query_metric",
                                     input=call_input)],
                raw_content=[{
                    "type": "tool_use", "id": "toolu_mock_1",
                    "name": "query_metric", "input": call_input,
                }],
            )

        fallback = "请补充想查询的指标/实体/期间，例如：香港去年REVENUE多少。"
        return ProviderResponse(
            text=fallback, raw_content=[{"type": "text", "text": fallback}]
        )

    @staticmethod
    def _final_answer(result: dict) -> ProviderResponse:
        status = result.get("status")
        if status == "found":
            source = result.get("source", {})
            # valid_as_of 存在时附「截至」业务时点；为 None 时文案字节级不变。
            valid_as_of = result.get("valid_as_of")
            asof = f" · 截至 {valid_as_of}" if valid_as_of else ""
            text = (
                f"{result['entity']} {result['period_type']}{result['period']} "
                f"{result['metric_code']} 为 {result['value']:g} {result['unit']}"
                f"（来源：{source.get('doc')} · {source.get('locator')}{asof}）"
            )
        elif status == "not_found":
            norm = result.get("normalized", {})
            text = (
                f"查不到：{norm.get('metric_code')} / {norm.get('entity')} / "
                f"{norm.get('period')}（{norm.get('channel')}）未在事实表中找到，"
                "不提供推测数字。"
            )
        elif status == "unrecognized_param":
            text = (
                f"无法识别参数 {result.get('param')}：'{result.get('raw')}'，"
                "请换一个说法或确认指标名称。"
            )
        else:
            text = "工具返回了未知状态，无法作答。"
        return ProviderResponse(text=text, raw_content=[{"type": "text", "text": text}])
