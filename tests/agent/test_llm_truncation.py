"""LLM 输出截断的 provider 层重试（agent/truncation）。

litellm.completion / subprocess.run / anthropic 客户端全部 mock，不发真实请求。覆盖：截断 1 次后成功、
截断到上限、tool_call 被截断、reasoning 关闭参数不被支持时的容错、没有截断时请求参数逐字节不变，
以及编排层 trace 只记计数、最终截断走诚实降级（半截答案不被采纳）。
"""

import json
import logging
import os
import sys
import types
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ProviderError

from ragspine.agent import claude_cli_provider as cli_mod
from ragspine.agent import litellm_provider as litellm_mod
from ragspine.agent.agent import NARRATIVE_FALLBACK_ENV, answer_question
from ragspine.agent.claude_cli_provider import ClaudeCliProvider
from ragspine.agent.litellm_provider import LiteLLMProvider
from ragspine.agent.llm_provider import AnthropicProvider
from ragspine.agent.number_guard import NARRATIVE_NUMBER_GUARD_ENV
from ragspine.agent.query_tools import QUERY_METRIC_TOOL_OPENAI
from ragspine.agent.truncation import (
    DEFAULT_TRUNCATION_MAX_RETRIES,
    DEFAULT_TRUNCATION_MAX_TOKENS,
    TRUNCATION_MAX_TOKENS_ENV,
    TRUNCATION_RETRY_ENV,
    TruncatedOutputError,
    TruncationPolicy,
    retry_on_truncation,
)
from ragspine.common.observability.llm_calls import record_llm_calls
from ragspine.eval.nl_gold_ragspine import ForcedNarrativeIntentParser
from ragspine.storage.fact_store import Fact, SqliteFactStore

TOOLS = [QUERY_METRIC_TOOL_OPENAI]
MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
ARGS = {"metric": "REVENUE", "entity": "ACME_HK", "period": "FY2025", "channel": "TOTAL"}
ON = TruncationPolicy()
OFF = TruncationPolicy(enabled=False)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for key in (
        TRUNCATION_RETRY_ENV,
        TRUNCATION_MAX_TOKENS_ENV,
        NARRATIVE_NUMBER_GUARD_ENV,
        NARRATIVE_FALLBACK_ENV,
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.delenv(key, raising=False)


class _Counts:
    """采集桶上的截断计数视图：口径与原 TruncationStats 相同（重试次数 / 重试后仍截断的调用数）。"""

    def __init__(self, bucket):
        self.bucket = bucket

    @property
    def calls(self):
        return self.bucket.calls

    @property
    def retries(self):
        return sum(c.trunc_retries for c in self.bucket.calls)

    @property
    def truncated_final(self):
        return sum(1 for c in self.bucket.calls if c.truncated)


@pytest.fixture
def stats():
    with record_llm_calls() as bucket:
        yield _Counts(bucket)


# ---------------------------------------------------------------------------
# 策略与驱动
# ---------------------------------------------------------------------------


def test_policy_defaults_and_env():
    assert TruncationPolicy.from_env({}) == TruncationPolicy(
        enabled=True, max_retries=2, max_tokens=16384
    )
    assert (DEFAULT_TRUNCATION_MAX_RETRIES, DEFAULT_TRUNCATION_MAX_TOKENS) == (2, 16384)
    env = {TRUNCATION_RETRY_ENV: "off", TRUNCATION_MAX_TOKENS_ENV: "8192"}
    assert TruncationPolicy.from_env(env) == TruncationPolicy(enabled=False, max_tokens=8192)
    with pytest.raises(ValueError, match=TRUNCATION_RETRY_ENV):
        TruncationPolicy.from_env({TRUNCATION_RETRY_ENV: "maybe"})


def test_next_budget_doubles_up_to_cap():
    p = TruncationPolicy(max_tokens=8192)
    assert p.next_budget(1000) == 2000
    assert p.next_budget(5000) == 8192
    assert p.next_budget(8192) is None
    assert p.next_budget(32000) is None
    assert p.next_budget(0) == 8192  # 未知预算直接给上限


def test_driver_without_bound_stats_is_silent():
    """不在请求内（未绑定计数器）时照常重试，计数静默丢弃。"""
    outcomes = iter([("cut", 100), ("full", None)])
    assert retry_on_truncation(lambda _b: next(outcomes), ON, what="x") == "full"


# ---------------------------------------------------------------------------
# LiteLLMProvider
# ---------------------------------------------------------------------------

try:
    litellm = litellm_mod._load_litellm()  # 经 provider 自己的加载器：本地价格表、不联网
except ImportError:
    litellm = None


def _resp(content=None, tool_calls=None, finish_reason="stop", completion_tokens=7):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        id="chatcmpl-1",
        model="deepseek-chat",
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=completion_tokens),
    )


def _tool_call(arguments):
    return SimpleNamespace(
        id="c1", type="function", function=SimpleNamespace(name="query_metric", arguments=arguments)
    )


class FakeCompletion:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outputs.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


@pytest.fixture
def fake(monkeypatch):
    if litellm is None:
        pytest.skip("未安装 [litellm] extra")

    def install(*outputs):
        f = FakeCompletion(*outputs)
        monkeypatch.setattr(litellm, "completion", f)
        return f

    return install


def _old_litellm_kwargs(tools=None, **extra):
    """引入截断重试前 `_request` 产出的请求参数（逐字节基准）。"""
    kwargs = {
        "model": "deepseek/deepseek-chat",
        "messages": [dict(m) for m in MESSAGES],
        "timeout": 120.0,
        "num_retries": 2,
    }
    if tools:
        kwargs["tools"] = tools
    kwargs.update(extra)
    return kwargs


@pytest.mark.parametrize("tools", [None, TOOLS])
@pytest.mark.parametrize("max_tokens", [None, 512])
def test_litellm_no_truncation_request_is_byte_identical(fake, tools, max_tokens):
    extra = {} if max_tokens is None else {"max_tokens": max_tokens}
    sent = []
    for policy in (ON, OFF):
        f = fake(_resp("ok", finish_reason="stop"))
        out = LiteLLMProvider(max_tokens=max_tokens, truncation=policy).chat(MESSAGES, tools=tools)
        assert out.choices[0].message.content == "ok"
        assert len(f.calls) == 1
        sent.append(json.dumps(f.calls[0], ensure_ascii=False, sort_keys=True))
    assert sent[0] == sent[1]
    assert sent[0] == json.dumps(
        _old_litellm_kwargs(tools, **extra), ensure_ascii=False, sort_keys=True
    )


def test_litellm_truncated_once_then_succeeds(fake, stats):
    f = fake(_resp("半截", finish_reason="length"), _resp("完整答案", finish_reason="stop"))
    out = LiteLLMProvider(max_tokens=1000, truncation=ON).chat(MESSAGES)
    assert out.choices[0].message.content == "完整答案"
    assert out.choices[0].finish_reason == "stop"
    assert f.calls[0] == _old_litellm_kwargs(max_tokens=1000)
    assert f.calls[1] == _old_litellm_kwargs(
        max_tokens=2000, reasoning_effort="none", drop_params=True
    )
    assert (stats.retries, stats.truncated_final) == (1, 0)
    (call,) = stats.calls
    assert (call.attempts, call.trunc_retries, call.retried) == (2, 1, True)
    assert (call.reasoning_disabled, call.truncated, call.error) == (True, False, "")
    assert (call.in_tokens, call.out_tokens) == (11, 7)


def test_litellm_unknown_budget_uses_observed_completion_tokens(fake):
    f = fake(_resp("半截", finish_reason="length", completion_tokens=4096), _resp("完整"))
    LiteLLMProvider(truncation=ON).chat(MESSAGES)
    assert "max_tokens" not in f.calls[0]
    assert f.calls[1]["max_tokens"] == 8192


def test_litellm_truncated_until_limit_raises(fake, stats):
    cut = [_resp("半截", finish_reason="length") for _ in range(3)]
    f = fake(*cut)
    with pytest.raises(TruncatedOutputError) as exc_info:
        LiteLLMProvider(max_tokens=2000, truncation=ON).chat(MESSAGES)
    assert isinstance(exc_info.value, ProviderError)  # 走上层现成的诚实降级
    assert [c.get("max_tokens") for c in f.calls] == [2000, 4000, 8000]  # 最多重试 2 次
    assert (stats.retries, stats.truncated_final) == (2, 1)
    (call,) = stats.calls
    assert (call.attempts, call.truncated, call.error) == (3, True, "provider.truncated")
    assert (call.in_tokens, call.out_tokens) == (None, None)


def test_litellm_stops_at_cap(fake, stats):
    f = fake(_resp("半截", finish_reason="length"), _resp("半截", finish_reason="length"))
    with pytest.raises(TruncatedOutputError):
        LiteLLMProvider(max_tokens=6000, truncation=TruncationPolicy(max_tokens=8192)).chat(
            MESSAGES
        )
    assert [c["max_tokens"] for c in f.calls] == [6000, 8192]
    assert (stats.retries, stats.truncated_final) == (1, 1)


def test_litellm_truncated_tool_call_is_retried(fake):
    f = fake(
        _resp(None, [_tool_call('{"metric": "REVEN')], finish_reason="length"),
        _resp(None, [_tool_call(json.dumps(ARGS))], finish_reason="tool_calls"),
    )
    out = LiteLLMProvider(max_tokens=64, truncation=ON).chat(MESSAGES, tools=TOOLS)
    assert len(f.calls) == 2 and f.calls[1]["tools"] == TOOLS
    assert json.loads(out.choices[0].message.tool_calls[0].function.arguments) == ARGS


@pytest.mark.parametrize(
    "rejection",
    [
        lambda: litellm.UnsupportedParamsError(message="reasoning_effort", llm_provider="openai"),
        lambda: litellm.BadRequestError(message="bad param", model="m", llm_provider="openai"),
    ],
)
def test_litellm_reasoning_param_rejected_falls_back_without_it(fake, rejection, stats):
    f = fake(
        _resp("半截", finish_reason="length"),
        rejection(),
        _resp("半截", finish_reason="length"),
        _resp("完整", finish_reason="stop"),
    )
    provider = LiteLLMProvider(max_tokens=100, truncation=ON)
    out = provider.chat(MESSAGES)
    assert out.choices[0].message.content == "完整"
    assert f.calls[1] == _old_litellm_kwargs(
        max_tokens=200, reasoning_effort="none", drop_params=True
    )
    assert f.calls[2] == _old_litellm_kwargs(max_tokens=200)  # 同一预算、去掉关闭参数再发
    assert f.calls[3] == _old_litellm_kwargs(max_tokens=400)  # 记住：之后不再带
    assert provider._reasoning_off_ok is False
    (call,) = stats.calls
    assert (call.attempts, call.trunc_retries, call.reasoning_disabled) == (4, 2, False)


def test_litellm_bad_request_fallback_counts_three_attempts(fake, stats):
    """截断一次 → 带关闭参数重试被网关 BadRequest → 去掉参数重发成功：attempts=3、reasoning 未关闭。"""
    fake(
        _resp("半截", finish_reason="length"),
        litellm.BadRequestError(message="bad param", model="m", llm_provider="openai"),
        _resp("完整", finish_reason="stop"),
    )
    out = LiteLLMProvider(max_tokens=100, truncation=ON).chat(MESSAGES)
    assert out.choices[0].message.content == "完整"
    (call,) = stats.calls
    assert (call.attempts, call.trunc_retries, call.retried) == (3, 1, True)
    assert (call.reasoning_disabled, call.truncated, call.error) == (False, False, "")


def test_litellm_disabled_returns_truncated_result_unchanged(fake):
    f = fake(_resp("半截", finish_reason="length"))
    out = LiteLLMProvider(max_tokens=100, truncation=OFF).chat(MESSAGES)
    assert len(f.calls) == 1
    assert (out.choices[0].message.content, out.choices[0].finish_reason) == ("半截", "length")


def test_litellm_policy_from_env_by_default(monkeypatch):
    monkeypatch.setenv(TRUNCATION_RETRY_ENV, "off")
    monkeypatch.setenv(TRUNCATION_MAX_TOKENS_ENV, "4096")
    assert LiteLLMProvider().truncation == TruncationPolicy(enabled=False, max_tokens=4096)


# ---------------------------------------------------------------------------
# 编排层：tool 循环 / 叙事合成都经 provider.chat，trace 只记计数
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    fs = SqliteFactStore(tmp_path / "f.db")
    fs.init_schema()
    fs.upsert_facts(
        [
            Fact(
                metric_code="REVENUE",
                entity="ACME_HK",
                geography="HK",
                channel="TOTAL",
                period_type="FY",
                period="2025",
                value=1702.0,
                unit="USD_M",
                source_doc_id="ACME_FY2025_Results.pptx",
                source_locator="slide=5,table=1,row=2,col=3",
            )
        ]
    )
    yield fs
    fs.close()


def _traces(caplog):
    return [r for r in caplog.records if r.name == "ragspine.trace"]


def test_tool_loop_truncated_tool_call_is_retried_and_traced(fake, store, caplog):
    fake(
        _resp(None, [_tool_call('{"metric": "REVENUE", "ent')], finish_reason="length"),
        _resp(None, [_tool_call(json.dumps(ARGS))], finish_reason="tool_calls"),
        _resp("香港 FY2025 REVENUE 为 1702 USD_M"),
    )
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        result = answer_question(
            "香港FY2025的REVENUE是多少",
            store,
            LiteLLMProvider(max_tokens=64, truncation=ON),
            reference_date=date(2026, 6, 12),
        )
    assert "1702" in result.answer
    (trace,) = _traces(caplog)
    assert (trace.llm_truncation_retries, trace.llm_truncated_final) == (1, 0)
    assert trace.provider_error is False
    assert sum(c["trunc_retries"] for c in trace.llm_calls) == trace.llm_truncation_retries
    assert [c["stage"] for c in trace.llm_calls] == ["tool_round", "tool_round"]


def test_tool_loop_still_truncated_degrades_honestly(fake, store, caplog):
    """最后一次仍截断：抛 ProviderError → 固定降级文案，不崩（半截 JSON 不进 json.loads）、不给数字。"""
    fake(*[_resp(None, [_tool_call('{"metric": "RE')], finish_reason="length") for _ in range(3)])
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        result = answer_question(
            "香港FY2025的REVENUE是多少",
            store,
            LiteLLMProvider(max_tokens=64, truncation=ON),
            reference_date=date(2026, 6, 12),
            narrative_fallback=False,
        )
    assert "1702" not in result.answer and "不可用" in result.answer
    (trace,) = _traces(caplog)
    assert (trace.llm_truncation_retries, trace.llm_truncated_final) == (2, 1)
    assert trace.provider_error is True
    assert sum(c["trunc_retries"] for c in trace.llm_calls) == trace.llm_truncation_retries
    assert sum(c["truncated"] for c in trace.llm_calls) == trace.llm_truncated_final
    assert trace.llm_calls[0]["error"] == "provider.truncated"


class _Retriever:
    def retrieve(self, query, *, filters=None, top_k=50):
        return [
            {
                "text": "1H26 VONB was US$514m, up 15 per cent.",
                "doc_id": "deck.md",
                "locator": "deck.md@page=10#para1-5",
            }
        ]


def _narrative(provider, store):
    return answer_question(
        "What was VONB in 1H26?",
        store,
        provider,
        reference_date=date(2026, 9, 24),
        narrative_retriever=_Retriever(),
        intent_parser=ForcedNarrativeIntentParser(),
    )


def test_narrative_half_answer_is_never_adopted(fake, store, caplog):
    """叙事合成最后仍截断：半截答案（其中数字本可通过数字防护）不被采纳，走叙事降级。"""
    half = "1H26 VONB 为 US$514m，同比增长 15%，主要驱动因素包括"
    fake(*[_resp(half, finish_reason="length") for _ in range(3)])
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        result = _narrative(LiteLLMProvider(max_tokens=100, truncation=ON), store)
    assert half not in result.answer and "514" not in result.answer
    assert result.sources == []
    (trace,) = _traces(caplog)
    assert trace.llm_truncated_final == 1


def test_no_truncation_trace_has_no_truncation_keys(fake, store, caplog):
    fake(_resp("1H26 VONB 为 US$514m。"))
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        result = _narrative(LiteLLMProvider(truncation=ON), store)
    assert "514" in result.answer
    (trace,) = _traces(caplog)
    assert not hasattr(trace, "llm_truncation_retries")
    assert not hasattr(trace, "llm_truncated_final")


# ---------------------------------------------------------------------------
# AnthropicProvider
# ---------------------------------------------------------------------------


def _install_fake_anthropic(monkeypatch, *responses):
    calls: list[dict] = []
    queue = list(responses)

    class _Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return queue.pop(0)

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = _FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    return calls


def _anthropic_text(text, stop_reason):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason
    )


def _old_anthropic_kwargs(max_tokens=16000, tools=None):
    kwargs = {
        "model": "claude-opus-4-8",
        "max_tokens": max_tokens,
        "system": "sys",
        "messages": [{"role": "user", "content": "q"}],
    }
    if tools:
        kwargs["tools"] = [
            {
                "name": "query_metric",
                "description": QUERY_METRIC_TOOL_OPENAI["function"]["description"],
                "input_schema": QUERY_METRIC_TOOL_OPENAI["function"]["parameters"],
            }
        ]
    return kwargs


@pytest.mark.parametrize("tools", [None, TOOLS])
def test_anthropic_no_truncation_request_is_byte_identical(monkeypatch, tools):
    sent = []
    for policy in (ON, OFF):
        calls = _install_fake_anthropic(monkeypatch, _anthropic_text("ok", "end_turn"))
        out = AnthropicProvider(api_key="k", truncation=policy).chat(MESSAGES, tools=tools)
        assert out.choices[0].message.content == "ok"
        assert len(calls) == 1
        sent.append(json.dumps(calls[0], ensure_ascii=False, sort_keys=True))
    assert sent[0] == sent[1]
    assert sent[0] == json.dumps(
        _old_anthropic_kwargs(tools=tools), ensure_ascii=False, sort_keys=True
    )


def test_anthropic_truncated_once_then_succeeds(monkeypatch, stats):
    calls = _install_fake_anthropic(
        monkeypatch, _anthropic_text("半截", "max_tokens"), _anthropic_text("完整", "end_turn")
    )
    out = AnthropicProvider(api_key="k", max_tokens=1000, truncation=ON).chat(MESSAGES)
    assert (out.choices[0].message.content, out.choices[0].finish_reason) == ("完整", "stop")
    assert calls[0] == _old_anthropic_kwargs(1000)
    assert calls[1] == _old_anthropic_kwargs(2000)
    assert (stats.retries, stats.truncated_final) == (1, 0)


def test_anthropic_truncated_until_limit_raises(monkeypatch, stats):
    calls = _install_fake_anthropic(
        monkeypatch, *[_anthropic_text("半截", "max_tokens") for _ in range(3)]
    )
    with pytest.raises(TruncatedOutputError):
        AnthropicProvider(api_key="k", max_tokens=1000, truncation=ON).chat(MESSAGES)
    assert [c["max_tokens"] for c in calls] == [1000, 2000, 4000]
    assert (stats.retries, stats.truncated_final) == (2, 1)
    (call,) = stats.calls
    assert (call.attempts, call.truncated, call.error) == (3, True, "provider.truncated")


def test_anthropic_truncated_tool_use_is_retried(monkeypatch):
    partial = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="t1", name="query_metric", input={})],
        stop_reason="max_tokens",
    )
    full = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="t1", name="query_metric", input=ARGS)],
        stop_reason="tool_use",
    )
    calls = _install_fake_anthropic(monkeypatch, partial, full)
    out = AnthropicProvider(api_key="k", max_tokens=64, truncation=ON).chat(MESSAGES, tools=TOOLS)
    assert len(calls) == 2 and calls[1]["max_tokens"] == 128
    assert json.loads(out.choices[0].message.tool_calls[0].function.arguments) == ARGS


def test_anthropic_disabled_returns_truncated_result_unchanged(monkeypatch):
    calls = _install_fake_anthropic(monkeypatch, _anthropic_text("半截", "max_tokens"))
    out = AnthropicProvider(api_key="k", max_tokens=100, truncation=OFF).chat(MESSAGES)
    assert len(calls) == 1 and out.choices[0].finish_reason == "length"


# ---------------------------------------------------------------------------
# ClaudeCliProvider
# ---------------------------------------------------------------------------


def _cli_ok(result):
    return 0, json.dumps({"type": "result", "is_error": False, "result": result, "usage": {}})


def _cli_cut(limit):
    return 1, json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "stop_reason": "stop_sequence",
            "terminal_reason": "api_error",
            "result": f"API Error: Claude's response exceeded the {limit} output token maximum. "
            "To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable.",
        }
    )


class FakeRun:
    """记录 subprocess.run 的完整参数（kwargs 原样保留、cwd 换成占位），按脚本返回。"""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        system_file = Path(cmd[cmd.index("--system-prompt-file") + 1])
        norm = [a if a != str(system_file) else "<system-file>" for a in cmd]
        self.calls.append({"cmd": norm, **{k: v for k, v in kwargs.items() if k != "cwd"}})
        code, stdout = self.outputs.pop(0)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(cli_mod.shutil, "which", lambda name: f"/fake/bin/{name}")

    def install(*outputs):
        f = FakeRun(*outputs)
        monkeypatch.setattr(cli_mod.subprocess, "run", f)
        return f

    return install


_OLD_CLI_CMD = [
    "/fake/bin/claude",
    "-p",
    "--output-format",
    "json",
    "--system-prompt-file",
    "<system-file>",
    "--tools",
    "",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
]
_OLD_CLI_KWARGS = {
    "input": "q",
    "capture_output": True,
    "text": True,
    "encoding": "utf-8",
    "timeout": 300.0,
    "check": False,
}


def test_cli_no_truncation_command_is_byte_identical(cli):
    sent = []
    for policy in (ON, OFF):
        f = cli(_cli_ok("ok"))
        out = ClaudeCliProvider(truncation=policy).chat(MESSAGES)
        assert out.choices[0].message.content == "ok"
        assert len(f.calls) == 1
        sent.append(json.dumps(f.calls[0], ensure_ascii=False, sort_keys=True))
    assert sent[0] == sent[1]
    assert f.calls[0] == {"cmd": _OLD_CLI_CMD, **_OLD_CLI_KWARGS}  # 无 env、无 --settings


def test_cli_truncated_once_then_succeeds(cli, stats, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "4000")
    f = cli(_cli_cut(4000), _cli_ok("完整"))
    out = ClaudeCliProvider(truncation=ON).chat(MESSAGES)
    assert out.choices[0].message.content == "完整"
    first, second = f.calls
    assert first == {"cmd": _OLD_CLI_CMD, **_OLD_CLI_KWARGS}
    assert second["cmd"] == [*_OLD_CLI_CMD, "--settings", '{"alwaysThinkingEnabled": false}']
    assert second["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8000"
    assert {k: v for k, v in second.items() if k not in ("cmd", "env")} == _OLD_CLI_KWARGS
    assert (stats.retries, stats.truncated_final) == (1, 0)
    (call,) = stats.calls
    assert (call.attempts, call.trunc_retries, call.reasoning_disabled) == (2, 1, True)


def test_cli_format_retry_counts_attempt_not_truncation(cli, stats):
    """工具模式格式重试计入 attempts，不计入 trunc_retries。"""
    f = cli(
        _cli_ok("不是 JSON"),
        _cli_ok(json.dumps({"tool_calls": [{"name": "query_metric", "arguments": ARGS}]})),
    )
    out = ClaudeCliProvider(truncation=ON).chat(MESSAGES, tools=TOOLS)
    assert len(f.calls) == 2
    assert json.loads(out.choices[0].message.tool_calls[0].function.arguments) == ARGS
    (call,) = stats.calls
    assert (call.attempts, call.trunc_retries, call.retried) == (2, 0, True)
    assert (call.reasoning_disabled, call.truncated) == (False, False)


def test_cli_truncated_until_limit_raises(cli, stats):
    f = cli(_cli_cut(4000), _cli_cut(8000), _cli_cut(16000))
    with pytest.raises(TruncatedOutputError):
        ClaudeCliProvider(truncation=ON).chat(MESSAGES)
    assert [c.get("env", {}).get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") for c in f.calls] == [
        None,
        "8000",
        "16000",
    ]
    assert (stats.retries, stats.truncated_final) == (2, 1)


def test_cli_default_limit_above_cap_raises_without_retry(cli, stats):
    """CLI 默认上限 32000 已超过重试上限：不重试，直接抛 TruncatedOutputError。"""
    f = cli(_cli_cut(32000))
    with pytest.raises(TruncatedOutputError):
        ClaudeCliProvider(truncation=ON).chat(MESSAGES)
    assert len(f.calls) == 1
    assert (stats.retries, stats.truncated_final) == (0, 1)
    (call,) = stats.calls
    assert (call.attempts, call.trunc_retries, call.truncated) == (1, 0, True)


def test_cli_truncated_tool_mode_is_retried(cli):
    f = cli(
        _cli_cut(1000),
        _cli_ok(json.dumps({"tool_calls": [{"name": "query_metric", "arguments": ARGS}]})),
    )
    out = ClaudeCliProvider(truncation=ON).chat(MESSAGES, tools=TOOLS)
    assert len(f.calls) == 2
    assert json.loads(out.choices[0].message.tool_calls[0].function.arguments) == ARGS


def test_cli_disabled_keeps_old_provider_error(cli):
    f = cli(_cli_cut(4000))
    with pytest.raises(ProviderError) as exc_info:
        ClaudeCliProvider(truncation=OFF).chat(MESSAGES)
    assert not isinstance(exc_info.value, TruncatedOutputError)
    assert "退出码 1" in str(exc_info.value)
    assert len(f.calls) == 1


def test_cli_other_errors_are_not_truncation(cli):
    cli(
        (1, json.dumps({"type": "result", "is_error": True, "result": "API Error: 529 overloaded"}))
    )
    with pytest.raises(ProviderError) as exc_info:
        ClaudeCliProvider(truncation=ON).chat(MESSAGES)
    assert not isinstance(exc_info.value, TruncatedOutputError)
