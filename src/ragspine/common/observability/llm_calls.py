"""按调用、分阶段的 LLM 埋点（ADR 0028）：每次 provider.chat 记一条只含计数 / 耗时 / 枚举的条目。

三个 ContextVar：
- `_BUCKET` —— 采集桶。`record_llm_calls()` 打开（with 或装饰器；`answer_question` 整个请求一个桶），
  嵌套打开时内层与外层隔离，退出即恢复外层。没有桶时一切静默。
- `_STAGE` —— 当前阶段标签。`llm_stage("hyde")` 设置（with 或装饰器），取值是闭集 `STAGES`，未知值在
  定义时就抛 `ValueError`；未标注的调用记为 `other`。
- `_PROBE` —— 当前调用探针。`instrument_llm_call` 装饰 provider 的 `chat`：计时、打开探针，provider 内部用
  `note_attempt` / `note_truncated` / `note_reasoning_disabled` 补充重试信息，结束时从返回值的
  `usage.prompt_tokens/completion_tokens`（鸭子类型）读 token、从异常的 `code` 属性取错误码，生成一条冻结的
  `LLMCall` 追加进桶。已有探针时（转发型包装器里再调一个被装饰的 provider）直接透传，只记一次。

保证：采集逻辑自身永远不抛错，也不改变被装饰函数的返回值与异常（同一对象、原样重抛）；没有桶时只多一次
ContextVar 读取。条目的字段只可能是 int / bool / None 或受控枚举（`STAGES` / `ERROR_CODES`），不记模型名、
prompt、回复或其长度。

并发：桶的追加加锁。新线程不继承 ContextVar，所以请求内部如果用线程池，必须每个任务各自
`copy_context().run(...)` 才会记进本请求的桶；裸提交的任务不会被记录。只用 stdlib。
"""

import functools
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import _GeneratorContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass

__all__ = [
    "ERROR_CODES",
    "STAGES",
    "STAGE_OTHER",
    "LLMCall",
    "LLMCallBucket",
    "current_llm_calls",
    "instrument_llm_call",
    "llm_stage",
    "llm_trace_fields",
    "note_attempt",
    "note_reasoning_disabled",
    "note_truncated",
    "record_llm_calls",
]

STAGE_OTHER = "other"

# 阶段闭集：按代码里实际存在的调用点（ADR 0028 表）；未标注的调用记为 other。
STAGES: tuple[str, ...] = (
    "decompose",
    "classify",
    "hyde",
    "rag_fusion",
    "step_back",
    "translation",
    "listwise_rerank",
    "tool_round",
    "synthesis",
    STAGE_OTHER,
)

# 错误码闭集：无错 / ProviderError（含其它 provider.* 子码）/ 截断用尽 / 其余异常。
ERROR_CODES: tuple[str, ...] = ("", "provider.error", "provider.truncated", "error")


@dataclass(frozen=True)
class LLMCall:
    """一次 provider.chat 的埋点条目（值只可能是 int / bool / None 或枚举）。"""

    stage: str
    ms: int
    attempts: int
    trunc_retries: int
    retried: bool
    truncated: bool
    reasoning_disabled: bool
    in_tokens: int | None
    out_tokens: int | None
    error: str

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(f"未知 LLM stage：{self.stage!r}")
        if self.error not in ERROR_CODES:
            raise ValueError(f"未知 LLM 错误码：{self.error!r}")

    def to_trace(self) -> dict[str, object]:
        return asdict(self)


class LLMCallBucket:
    """一次请求的采集桶；追加加锁（请求内并发任务共享同一个桶）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[LLMCall] = []

    def append(self, call: LLMCall) -> None:
        with self._lock:
            self._calls.append(call)

    @property
    def calls(self) -> tuple[LLMCall, ...]:
        with self._lock:
            return tuple(self._calls)


@dataclass
class _Probe:
    attempts: int = 1
    trunc_retries: int = 0
    truncated: bool = False
    reasoning_disabled: bool = False


_BUCKET: ContextVar[LLMCallBucket | None] = ContextVar("ragspine_llm_bucket", default=None)
_STAGE: ContextVar[str] = ContextVar("ragspine_llm_stage", default=STAGE_OTHER)
_PROBE: ContextVar[_Probe | None] = ContextVar("ragspine_llm_probe", default=None)


@contextmanager
def record_llm_calls() -> Iterator[LLMCallBucket]:
    """打开一个采集桶（with 产出该桶；也可当装饰器，每次调用各开一个新桶）。"""
    bucket = LLMCallBucket()
    token = _BUCKET.set(bucket)
    try:
        yield bucket
    finally:
        _BUCKET.reset(token)


def current_llm_calls() -> tuple[LLMCall, ...]:
    """当前桶里已记录的条目；没有桶时为空。"""
    bucket = _BUCKET.get()
    return bucket.calls if bucket is not None else ()


@contextmanager
def _stage_scope(stage: str) -> Iterator[None]:
    token = _STAGE.set(stage)
    try:
        yield
    finally:
        _STAGE.reset(token)


def llm_stage(stage: str) -> _GeneratorContextManager[None]:
    """给其中的 LLM 调用打阶段标签（with 或装饰器）；stage 不在 `STAGES` 里时立即抛 ValueError。"""
    if stage not in STAGES:
        raise ValueError(f"未知 LLM stage：{stage!r}（可选 {' / '.join(STAGES)}）")
    return _stage_scope(stage)


def note_attempt(*, truncation: bool = False) -> None:
    """provider 在同一次 chat 里又发出一个请求（截断重试 / 格式重试 / 去掉参数重发）。"""
    probe = _PROBE.get()
    if probe is None:
        return
    probe.attempts += 1
    if truncation:
        probe.trunc_retries += 1


def note_truncated() -> None:
    """重试用尽后输出仍被截断。"""
    probe = _PROBE.get()
    if probe is not None:
        probe.truncated = True


def note_reasoning_disabled(disabled: bool = True) -> None:
    """最终成功的那次请求是否带着关闭 reasoning 的参数。"""
    probe = _PROBE.get()
    if probe is not None:
        probe.reasoning_disabled = disabled


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _error_code(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    code = getattr(exc, "code", None)
    if code in ERROR_CODES and code:
        return str(code)
    if isinstance(code, str) and code.startswith("provider."):
        return "provider.error"
    return "error"


def _finish(
    bucket: LLMCallBucket,
    stage: str,
    probe: _Probe,
    started: float,
    result: object,
    exc: BaseException | None,
) -> None:
    """生成条目并追加；任何失败都吞掉（采集逻辑永不影响被测调用）。"""
    try:
        usage = getattr(result, "usage", None) if exc is None else None
        bucket.append(
            LLMCall(
                stage=stage,
                ms=round((time.perf_counter() - started) * 1000),
                attempts=probe.attempts,
                trunc_retries=probe.trunc_retries,
                retried=probe.attempts > 1,
                truncated=probe.truncated,
                reasoning_disabled=probe.reasoning_disabled,
                in_tokens=_int_or_none(getattr(usage, "prompt_tokens", None)),
                out_tokens=_int_or_none(getattr(usage, "completion_tokens", None)),
                error=_error_code(exc),
            )
        )
    except Exception:  # noqa: BLE001, S110 — 埋点失败不得影响业务调用
        pass


def instrument_llm_call[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """provider `chat` 的装饰器：有桶且没有外层探针时记一条 `LLMCall`，否则直接透传。"""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        bucket = _BUCKET.get()
        if bucket is None or _PROBE.get() is not None:
            return func(*args, **kwargs)
        stage = _STAGE.get()
        probe = _Probe()
        token = _PROBE.set(probe)
        started = time.perf_counter()
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            _finish(bucket, stage, probe, started, None, exc)
            raise
        finally:
            _PROBE.reset(token)
        _finish(bucket, stage, probe, started, result, None)
        return result

    return wrapper


def llm_trace_fields(calls: Iterable[LLMCall]) -> dict[str, object]:
    """请求 trace 的 LLM 字段；没有调用时为空（零 LLM 路径的 trace 逐字节不变）。

    `llm_ms` 是各次调用耗时之和，不是墙钟时间。截断两个顶层键由条目推导，只在非零时出现。
    """
    entries = list(calls)
    if not entries:
        return {}
    fields: dict[str, object] = {
        "llm_calls": [c.to_trace() for c in entries],
        "llm_n_calls": len(entries),
        "llm_n_retried": sum(1 for c in entries if c.retried),
        "llm_ms": sum(c.ms for c in entries),
    }
    retries = sum(c.trunc_retries for c in entries)
    final = sum(1 for c in entries if c.truncated)
    if retries or final:
        fields["llm_truncation_retries"] = retries
        fields["llm_truncated_final"] = final
    return fields
