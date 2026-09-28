"""按调用、分阶段的 LLM 埋点（common/observability/llm_calls，ADR 0028）。

覆盖：采集桶与调用探针；没有桶时 provider 行为与返回值逐字节不变（同一对象、异常原样、开销可忽略）；
嵌套请求隔离；未标注记为 other；未知 stage 在定义时抛 ValueError；采集代码自身出错不影响被测调用；
线程池用 copy_context().run 能记到、裸线程池记不到；多线程追加时加锁；条目值只可能是 int/bool/None/枚举。
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import FrozenInstanceError, fields
from types import SimpleNamespace

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.common.observability import llm_calls as mod
from ragspine.common.observability.llm_calls import (
    ERROR_CODES,
    STAGE_OTHER,
    STAGES,
    LLMCall,
    current_llm_calls,
    instrument_llm_call,
    llm_stage,
    llm_trace_fields,
    note_attempt,
    note_reasoning_disabled,
    note_truncated,
    record_llm_calls,
)


class _Boom(Exception):
    code = "provider.error"

    def __init__(self, message: str, extra: int) -> None:
        super().__init__(message)
        self.extra = extra


def _completion(prompt_tokens=11, completion_tokens=7):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    )


class _Provider:
    """最小 stub：按脚本返回 / 抛错，可在 chat 内部补记探针信息。"""

    def __init__(self, *outputs, hook=None):
        self.outputs = list(outputs)
        self.hook = hook

    @instrument_llm_call
    def chat(self, messages, *, tools=None):
        if self.hook is not None:
            self.hook()
        out = self.outputs.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


# ---------------------------------------------------------------------------
# 采集桶与探针
# ---------------------------------------------------------------------------


def test_bucket_records_one_entry_per_call_with_usage():
    resp = _completion()
    with record_llm_calls() as bucket, llm_stage("synthesis"):
        out = _Provider(resp).chat([])
    assert out is resp
    (call,) = bucket.calls
    assert call.stage == "synthesis"
    assert (call.attempts, call.trunc_retries, call.retried, call.truncated) == (1, 0, False, False)
    assert (call.in_tokens, call.out_tokens, call.error) == (11, 7, "")
    assert call.reasoning_disabled is False
    assert isinstance(call.ms, int) and call.ms >= 0


def test_missing_usage_records_none_tokens():
    with record_llm_calls() as bucket:
        _Provider(SimpleNamespace(usage=None)).chat([])
    (call,) = bucket.calls
    assert (call.in_tokens, call.out_tokens) == (None, None)


def test_probe_notes_attempts_truncation_and_reasoning():
    def hook():
        note_attempt(truncation=True)
        note_attempt()
        note_reasoning_disabled()

    with record_llm_calls() as bucket:
        _Provider(_completion(), hook=hook).chat([])
    (call,) = bucket.calls
    assert (call.attempts, call.trunc_retries, call.retried) == (3, 1, True)
    assert call.reasoning_disabled is True


def test_error_codes_are_mapped_to_closed_enum():
    class _Truncated(Exception):
        code = "provider.truncated"

    class _OtherProvider(Exception):
        code = "provider.rate_limited"

    def hook():
        note_truncated()

    with record_llm_calls() as bucket:
        for exc, h in (
            (_Boom("x", 1), None),
            (_Truncated("cut"), hook),
            (_OtherProvider("r"), None),
            (KeyError("bug"), None),
        ):
            with pytest.raises(type(exc)):
                _Provider(exc, hook=h).chat([])
    assert [c.error for c in bucket.calls] == [
        "provider.error",
        "provider.truncated",
        "provider.error",
        "error",
    ]
    assert [c.truncated for c in bucket.calls] == [False, True, False, False]
    assert set(ERROR_CODES) == {"", "provider.error", "provider.truncated", "error"}


def test_no_bucket_is_silent():
    """没有打开采集桶：调用照常、note_* 不报错、current_llm_calls 为空。"""
    note_attempt(truncation=True)
    note_truncated()
    note_reasoning_disabled()
    resp = _completion()
    assert _Provider(resp).chat([]) is resp
    assert current_llm_calls() == ()


def test_nested_buckets_are_isolated():
    with record_llm_calls() as outer:
        with llm_stage("decompose"):
            _Provider(_completion()).chat([])
        with record_llm_calls() as inner, llm_stage("synthesis"):
            _Provider(_completion()).chat([])
            assert [c.stage for c in current_llm_calls()] == ["synthesis"]
        with llm_stage("classify"):
            _Provider(_completion()).chat([])
    assert [c.stage for c in outer.calls] == ["decompose", "classify"]
    assert [c.stage for c in inner.calls] == ["synthesis"]


def test_record_llm_calls_as_decorator_opens_fresh_bucket_per_call():
    seen = []

    @record_llm_calls()
    def handler(n):
        for _ in range(n):
            _Provider(_completion()).chat([])
        seen.append(len(current_llm_calls()))
        return n

    assert handler(2) == 2
    assert handler(1) == 1
    assert seen == [2, 1]
    assert current_llm_calls() == ()


def test_unlabelled_call_is_other():
    with record_llm_calls() as bucket:
        _Provider(_completion()).chat([])
    assert [c.stage for c in bucket.calls] == [STAGE_OTHER]


def test_unknown_stage_raises_at_definition_time():
    with pytest.raises(ValueError, match="nope"):
        llm_stage("nope")
    with pytest.raises(ValueError):

        class _X:
            @llm_stage("synthesiss")
            def f(self):
                return 1


def test_llm_stage_as_decorator_labels_calls_and_restores():
    class _Stage:
        @llm_stage("hyde")
        def run(self, provider):
            return provider.chat([])

    with record_llm_calls() as bucket:
        _Stage().run(_Provider(_completion()))
        _Provider(_completion()).chat([])
    assert [c.stage for c in bucket.calls] == ["hyde", STAGE_OTHER]


def test_nested_instrumented_call_is_recorded_once():
    """转发型包装：外层已开探针时内层直接透传，不重复记录。"""

    class _Inner:
        @instrument_llm_call
        def chat(self, messages, *, tools=None):
            note_attempt()
            return _completion()

    class _Outer:
        def __init__(self):
            self.inner = _Inner()

        @instrument_llm_call
        def chat(self, messages, *, tools=None):
            return self.inner.chat(messages, tools=tools)

    with record_llm_calls() as bucket:
        _Outer().chat([])
    (call,) = bucket.calls
    assert call.attempts == 2


def test_collector_failure_never_breaks_the_call(monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("collector bug")

    monkeypatch.setattr(mod, "_int_or_none", broken)
    resp = _completion()
    with record_llm_calls() as bucket:
        assert _Provider(resp).chat([]) is resp
        with pytest.raises(_Boom):
            _Provider(_Boom("x", 1)).chat([])
    assert bucket.calls == ()


# ---------------------------------------------------------------------------
# 无桶时逐字节不变 + 开销可忽略
# ---------------------------------------------------------------------------


def _bare_chat(out):
    if isinstance(out, BaseException):
        raise out
    return out


_decorated_chat = instrument_llm_call(_bare_chat)


def test_no_bucket_returns_same_object():
    for out in (_completion(), SimpleNamespace(), object(), None, "text"):
        assert _decorated_chat(out) is _bare_chat(out)


def test_no_bucket_exception_is_unchanged():
    exc = _Boom("网络故障", 42)
    with pytest.raises(_Boom) as bare:
        _bare_chat(exc)
    with pytest.raises(_Boom) as decorated:
        _decorated_chat(exc)
    assert decorated.value is exc
    assert decorated.type is bare.type
    assert str(decorated.value) == str(bare.value) == "网络故障"
    assert decorated.value.args == bare.value.args
    assert (decorated.value.extra, decorated.value.code) == (42, "provider.error")
    # traceback 的最底层仍是原函数抛出的那一行
    assert decorated.traceback[-1].name == bare.traceback[-1].name == "_bare_chat"


def test_decorator_keeps_name_and_signature():
    import inspect

    assert _decorated_chat.__name__ == "_bare_chat"
    assert inspect.signature(_decorated_chat) == inspect.signature(_bare_chat)


def test_no_bucket_overhead_is_negligible():
    """1 万次调用的额外耗时与未装饰基线比（阈值放宽防误报：每次额外 < 20µs）。"""
    out = _completion()
    n = 10_000

    def timed(fn):
        best = float("inf")
        for _ in range(3):
            started = time.perf_counter()
            for _ in range(n):
                fn(out)
            best = min(best, time.perf_counter() - started)
        return best

    baseline = timed(_bare_chat)
    decorated = timed(_decorated_chat)
    assert (decorated - baseline) / n < 20e-6, (baseline, decorated)


# ---------------------------------------------------------------------------
# 并发规则：请求内部线程池必须每任务 copy_context().run，追加加锁
# ---------------------------------------------------------------------------


def test_thread_pool_with_copy_context_records_bare_pool_does_not():
    with record_llm_calls() as bucket, llm_stage("translation"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            ok = [
                pool.submit(copy_context().run, _Provider(_completion()).chat, []) for _ in range(3)
            ]
            for f in ok:
                f.result()
        with ThreadPoolExecutor(max_workers=2) as pool:
            lost = [pool.submit(_Provider(_completion()).chat, []) for _ in range(3)]
            for f in lost:
                f.result()
    assert [c.stage for c in bucket.calls] == ["translation"] * 3


def test_concurrent_appends_are_locked():
    n_threads, per_thread = 8, 200
    barrier = threading.Barrier(n_threads)

    def work():
        barrier.wait()
        for _ in range(per_thread):
            _Provider(_completion()).chat([])

    with record_llm_calls() as bucket:
        threads = [
            threading.Thread(target=copy_context().run, args=(work,)) for _ in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(bucket.calls) == n_threads * per_thread


# ---------------------------------------------------------------------------
# 值约束与 trace 字段
# ---------------------------------------------------------------------------


def test_llm_call_is_frozen_and_enum_constrained():
    call = LLMCall(
        stage="synthesis",
        ms=3,
        attempts=1,
        trunc_retries=0,
        retried=False,
        truncated=False,
        reasoning_disabled=False,
        in_tokens=None,
        out_tokens=None,
        error="",
    )
    with pytest.raises(FrozenInstanceError):
        call.stage = "other"  # type: ignore[misc]
    with pytest.raises(ValueError):
        LLMCall(**{**call.to_trace(), "stage": "<prompt 正文>"})
    with pytest.raises(ValueError):
        LLMCall(**{**call.to_trace(), "error": "Traceback: secret"})
    assert [f.name for f in fields(LLMCall)] == [
        "stage",
        "ms",
        "attempts",
        "trunc_retries",
        "retried",
        "truncated",
        "reasoning_disabled",
        "in_tokens",
        "out_tokens",
        "error",
    ]


def test_trace_fields_leaves_are_int_bool_none_or_enum():
    def hook():
        note_attempt(truncation=True)

    with record_llm_calls() as bucket:
        with llm_stage("tool_round"):
            _Provider(_completion(), hook=hook).chat([])
        with pytest.raises(_Boom):
            _Provider(_Boom("x", 1)).chat([])
    out = llm_trace_fields(bucket.calls)
    assert out["llm_n_calls"] == 2 and out["llm_n_retried"] == 1
    assert out["llm_ms"] == sum(c.ms for c in bucket.calls)
    assert (out["llm_truncation_retries"], out["llm_truncated_final"]) == (1, 0)
    for entry in out["llm_calls"]:
        for key, value in entry.items():
            if key == "stage":
                assert value in STAGES
            elif key == "error":
                assert value in ERROR_CODES
            else:
                assert value is None or isinstance(value, (int, bool)), (key, value)


def test_trace_fields_empty_without_calls_and_no_truncation_keys_when_zero():
    assert llm_trace_fields(()) == {}
    with record_llm_calls() as bucket:
        _Provider(_completion()).chat([])
    out = llm_trace_fields(bucket.calls)
    assert "llm_truncation_retries" not in out and "llm_truncated_final" not in out
    assert out["llm_n_retried"] == 0


def test_stage_enum_is_closed():
    assert STAGES == (
        "decompose",
        "classify",
        "hyde",
        "rag_fusion",
        "step_back",
        "translation",
        "listwise_rerank",
        "tool_round",
        "synthesis",
        "other",
    )
