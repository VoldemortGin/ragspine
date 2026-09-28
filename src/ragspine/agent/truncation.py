"""LLM 输出截断的 provider 层重试（LiteLLM / ClaudeCli / Anthropic 共用）。

输出因长度被截断（OpenAI `finish_reason="length"`、Anthropic `stop_reason="max_tokens"`、`claude -p` 的
"exceeded the N output token maximum" 错误）时，把输出 token 预算翻倍后重试，同时关掉 reasoning（思考 token
也占输出预算）；最多重试 `max_retries` 次，预算不超过 `max_tokens`。叙事合成、tool 循环、翻译、分解等所有调用都
经 provider 的 `chat`，因此在这里处理就一并覆盖（tool 循环里截断的 tool_call JSON 同样带长度信号）。

最后一次仍被截断 → 抛 `TruncatedOutputError`（`ProviderError` 子类），不返回半截结果：上层没有任何一处看
`finish_reason`，半截叙事答案里的数字照样能通过数字防护、被当成完整答案采纳；半截 tool_call 参数会让 tool
循环 `json.loads` 直接崩。抛 ProviderError 则走各调用点现成的诚实降级（固定降级文案 / 原序 / 不翻译）。

没有截断时一次调用、请求参数逐字节不变；`RAGSPINE_LLM_TRUNCATION_RETRY=off` 时截断结果照旧原样返回
（与引入本模块前一致）。trace 只记计数：每次重试 / 最终截断记到当前 LLM 调用探针
（`common/observability/llm_calls`，ADR 0028），请求 trace 的 `llm_truncation_retries` / `llm_truncated_final`
由 `llm_calls` 推导；不在采集桶内时计数静默丢弃。
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from corespine import ProviderError

from ragspine.common.observability.llm_calls import note_attempt, note_truncated

DEFAULT_TRUNCATION_MAX_RETRIES = 2
DEFAULT_TRUNCATION_MAX_TOKENS = 16384

TRUNCATION_RETRY_ENV = "RAGSPINE_LLM_TRUNCATION_RETRY"
TRUNCATION_MAX_TOKENS_ENV = "RAGSPINE_LLM_TRUNCATION_MAX_TOKENS"


class TruncatedOutputError(ProviderError):
    """重试用尽 / 预算到顶后输出仍被截断。"""

    code = "provider.truncated"


@dataclass(frozen=True)
class TruncationPolicy:
    """截断重试策略：开关、最多重试次数、输出预算上限。"""

    enabled: bool = True
    max_retries: int = DEFAULT_TRUNCATION_MAX_RETRIES
    max_tokens: int = DEFAULT_TRUNCATION_MAX_TOKENS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TruncationPolicy":
        """读 RAGSPINE_LLM_TRUNCATION_RETRY（on|off，默认 on）与 RAGSPINE_LLM_TRUNCATION_MAX_TOKENS。"""
        env = os.environ if env is None else env
        spec = (env.get(TRUNCATION_RETRY_ENV) or "on").strip().lower()
        if spec not in ("on", "off"):
            raise ValueError(f"{TRUNCATION_RETRY_ENV} 只能是 on / off，收到 {spec!r}")
        cap = (env.get(TRUNCATION_MAX_TOKENS_ENV) or "").strip()
        return cls(
            enabled=spec == "on",
            max_tokens=int(cap) if cap else DEFAULT_TRUNCATION_MAX_TOKENS,
        )

    def next_budget(self, current: int) -> int | None:
        """翻倍后的预算（不超过上限）；已到上限、无法再增长时返回 None。current<=0 表示未知，直接给上限。"""
        budget = min(current * 2, self.max_tokens) if current > 0 else self.max_tokens
        return budget if budget > current else None


def retry_on_truncation[T](
    attempt: Callable[[int | None], tuple[T, int | None]],
    policy: TruncationPolicy,
    *,
    what: str,
) -> T:
    """按策略驱动截断重试。

    attempt(budget) → (结果, 截断时耗尽的预算)。budget=None 表示首次调用（沿用 provider 原有参数，请求逐字节
    不变）；非 None 是重试预算，provider 据此放大输出上限并关掉 reasoning。第二项为 None 表示没被截断；为 int
    表示被截断时的有效预算（未知时给 0）。
    """
    result, hit = attempt(None)
    retries = 0
    while hit is not None and policy.enabled:
        budget = policy.next_budget(hit)
        if budget is None or retries >= policy.max_retries:
            note_truncated()
            raise TruncatedOutputError(
                f"{what} 输出被截断（已重试 {retries} 次，最后预算 {hit} tokens）"
            )
        retries += 1
        note_attempt(truncation=True)
        result, hit = attempt(budget)
    return result
