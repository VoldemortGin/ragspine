"""异步任务队列抽象：TaskQueue 协议 + FakeQueue（同步内联，测试用）+ RQQueue（RQ/Redis）。

设计要点：
- 与既有的 ReviewQueue（SME 业务复核队列）彻底区分——此处是 worker/job 队列。
- v1 落地 RQ+Redis，但保留 TaskQueue 协议，未来可平替为 Celery。
- rq/redis 在 RQQueue 内部延迟 import，不装也能 import 本模块、用 FakeQueue 跑离线测试。
- job payload 必须是纯可序列化 dict；func 是点路径或可调用对象，末段签名 fn(payload)->dict。

与 corespine 的关系（queue 缝）：
- JobStatus（id/status/result/error 状态快照）直接复用 corespine 中性核，全家族同一形状。
- 本模块 TaskQueue 协议在概念上扩展 corespine.TaskQueue（enqueue/get）：额外约定 RQ 专属
  kwargs（timeout/max_retries/result_ttl/failure_ttl）与点路径 / 可调用 func，供 RQQueue 落地。
- RQ 专属（Retry 映射、map_rq_status、ping、JobError.stage/retryable）留在 ragspine，不外迁；
  JobStatus.error 的 {type,message,stage,retryable} 形状契约亦在本地维护（经 error_to_dict 归一）。
"""

import uuid
from collections.abc import Callable
from importlib import import_module
from typing import Any, Protocol, cast, runtime_checkable

from corespine import CorespineError, JobStatus, error_to_dict
from corespine import TaskQueue as _CoreTaskQueue
from corespine.queue.task_queue import JobFunc

__all__ = [
    "JOB_QUEUED",
    "JOB_STARTED",
    "JOB_FINISHED",
    "JOB_FAILED",
    "JobStatus",
    "JobError",
    "TaskQueue",
    "FakeQueue",
    "RQQueue",
    "map_rq_status",
]

# job 状态常量（与 RQ 语义对齐后的统一口径）
JOB_QUEUED = "queued"
JOB_STARTED = "started"
JOB_FINISHED = "finished"
JOB_FAILED = "failed"


class JobError(CorespineError):
    """job 执行内部抛出的受控错误：携带失败阶段与是否可重试。

    继承家族统一异常基类，稳定 code 为 "job.error"（ADR errors 缝）：retryable 作实例级
    覆盖，stage 进 context；stage/retryable 仍作实例属性暴露，保持既有调用方读取方式不变。
    """

    code = "job.error"

    def __init__(self, message: str, *, stage: str = "execution", retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable, stage=stage)
        self.stage = stage


@runtime_checkable
class TaskQueue(_CoreTaskQueue, Protocol):
    """ragspine 的任务队列协议：扩展 corespine.TaskQueue（enqueue/get）。

    在中性核的基础上，enqueue 额外约定 RQ 专属 kwargs（timeout/max_retries/result_ttl/
    failure_ttl）；func 沿用中性核的 JobFunc | str（点路径或可调用对象），ragspine 落地
    时主用点路径字符串。任一本协议实现都满足 corespine.TaskQueue 的结构契约。
    """

    def enqueue(
        self,
        func: JobFunc | str,
        payload: dict[str, Any],
        *,
        job_id: str | None = None,
        timeout: int | None = None,
        max_retries: int = 0,
        result_ttl: int | None = None,
        failure_ttl: int | None = None,
    ) -> str: ...

    def get(self, job_id: str) -> JobStatus | None: ...


def map_rq_status(rq_status: str | None) -> str:
    """把 RQ 的 job 状态映射成统一口径；未知/None 一律保守视作 queued。"""
    if rq_status in ("queued", "deferred", "scheduled"):
        return JOB_QUEUED
    if rq_status == "started":
        return JOB_STARTED
    if rq_status == "finished":
        return JOB_FINISHED
    if rq_status in ("failed", "stopped", "canceled"):
        return JOB_FAILED
    return JOB_QUEUED


def _resolve_callable(func: JobFunc | str) -> Callable[..., Any]:
    """可调用对象原样；点路径字符串 -> 可调用对象（末段为属性名）。"""
    if callable(func):
        return func
    module_path, _, attr = func.rpartition(".")
    if not module_path:
        raise ValueError(f"func must be a callable or dotted path: {func!r}")
    mod = import_module(module_path)
    # getattr on a dynamically imported module yields Any; the contract is fn(payload)->dict.
    return cast("Callable[..., Any]", getattr(mod, attr))


def _error_dict_from_exc(exc: BaseException) -> dict[str, Any]:
    # 经 corespine.error_to_dict 归一后，再适配回 JobStatus.error 的对外契约
    # {type, message, stage, retryable}：stage 由 context 承载（JobError 写入），
    # 非受控异常 context 为空 -> stage 回退 "execution"。
    normalized = error_to_dict(exc)
    context = cast("dict[str, Any]", normalized.get("context", {}))
    stage = context.get("stage", "execution")
    return {
        "type": normalized["type"],
        "message": normalized["message"],
        "stage": stage,
        "retryable": normalized["retryable"],
    }


class FakeQueue:
    """同步内存队列：enqueue 时内联执行 job（测试用，无需 Redis）。"""

    def __init__(self) -> None:
        self._jobs: dict[str, JobStatus] = {}

    def enqueue(
        self,
        func: JobFunc | str,
        payload: dict[str, Any],
        *,
        job_id: str | None = None,
        timeout: int | None = None,
        max_retries: int = 0,
        result_ttl: int | None = None,
        failure_ttl: int | None = None,
    ) -> str:
        # 幂等：显式 job_id 且已知 -> 直接返回，不重跑
        if job_id is not None and job_id in self._jobs:
            return job_id

        jid = job_id or uuid.uuid4().hex[:12]
        try:
            fn = _resolve_callable(func)
            result = fn(payload)
            self._jobs[jid] = JobStatus(id=jid, status=JOB_FINISHED, result=result)
        except Exception as exc:  # 内联失败不外抛，记进 JobStatus
            self._jobs[jid] = JobStatus(id=jid, status=JOB_FAILED, error=_error_dict_from_exc(exc))
        return jid

    def get(self, job_id: str) -> JobStatus | None:
        return self._jobs.get(job_id)


class RQQueue:
    """RQ+Redis 后端。rq/redis 延迟 import，仅在实例化/调用时需要。"""

    def __init__(self, redis_url: str, *, queue_name: str = "ragspine-ingest") -> None:
        self._redis_url = redis_url
        self._queue_name = queue_name

    def _connection(self) -> Any:
        import redis

        return redis.Redis.from_url(self._redis_url)

    def _queue(self, connection: Any) -> Any:
        import rq

        return rq.Queue(self._queue_name, connection=connection)

    def enqueue(
        self,
        func: JobFunc | str,
        payload: dict[str, Any],
        *,
        job_id: str | None = None,
        timeout: int | None = None,
        max_retries: int = 0,
        result_ttl: int | None = None,
        failure_ttl: int | None = None,
    ) -> str:
        import rq

        connection = self._connection()
        queue = self._queue(connection)
        retry = rq.Retry(max=max_retries) if max_retries else None
        job = queue.enqueue(
            func,
            payload,
            job_id=job_id,
            job_timeout=timeout,
            retry=retry,
            result_ttl=result_ttl,
            failure_ttl=failure_ttl,
        )
        return cast(str, job.id)

    def get(self, job_id: str) -> JobStatus | None:
        import rq

        connection = self._connection()
        try:
            job = rq.job.Job.fetch(job_id, connection=connection)
        except Exception:
            return None

        status = map_rq_status(job.get_status())
        error: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        if status == JOB_FINISHED:
            raw = job.result
            result = raw if isinstance(raw, dict) else {"value": raw}
        elif status == JOB_FAILED:
            error = {
                "type": "JobFailed",
                "message": (job.exc_info or "").strip().splitlines()[-1]
                if job.exc_info
                else "job failed",
                "stage": "execution",
                "retryable": False,
            }
        return JobStatus(id=job_id, status=status, result=result, error=error)

    def ping(self) -> bool:
        try:
            return bool(self._connection().ping())
        except Exception:
            return False
