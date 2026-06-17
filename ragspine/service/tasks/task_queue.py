"""异步任务队列抽象：TaskQueue 协议 + FakeQueue（同步内联，测试用）+ RQQueue（RQ/Redis）。

设计要点：
- 与既有的 ReviewQueue（SME 业务复核队列）彻底区分——此处是 worker/job 队列。
- v1 落地 RQ+Redis，但保留 TaskQueue 协议，未来可平替为 Celery。
- rq/redis 在 RQQueue 内部延迟 import，不装也能 import 本模块、用 FakeQueue 跑离线测试。
- job payload 必须是纯可序列化 dict；func_path 是点路径，末段为可调用对象，签名 fn(payload)->dict。
"""

from dataclasses import dataclass
from importlib import import_module
from typing import Protocol, runtime_checkable
import uuid

# job 状态常量（与 RQ 语义对齐后的统一口径）
JOB_QUEUED = "queued"
JOB_STARTED = "started"
JOB_FINISHED = "finished"
JOB_FAILED = "failed"


@dataclass
class JobStatus:
    id: str
    status: str
    result: dict | None = None
    error: dict | None = None
    # 失败时 error 形如：{"type": str, "message": str, "stage": str, "retryable": bool}


class JobError(Exception):
    """job 执行内部抛出的受控错误：携带失败阶段与是否可重试。"""

    def __init__(self, message: str, *, stage: str = "execution", retryable: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable


@runtime_checkable
class TaskQueue(Protocol):
    def enqueue(
        self,
        func_path: str,
        payload: dict,
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


def _resolve_callable(func_path: str):
    """点路径 -> 可调用对象（末段为属性名）。"""
    module_path, _, attr = func_path.rpartition(".")
    if not module_path:
        raise ValueError(f"func_path must be a dotted path: {func_path!r}")
    mod = import_module(module_path)
    return getattr(mod, attr)


def _error_dict_from_exc(exc: BaseException) -> dict:
    if isinstance(exc, JobError):
        stage, retryable = exc.stage, exc.retryable
    else:
        stage, retryable = "execution", False
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "stage": stage,
        "retryable": retryable,
    }


class FakeQueue:
    """同步内存队列：enqueue 时内联执行 job（测试用，无需 Redis）。"""

    def __init__(self) -> None:
        self._jobs: dict[str, JobStatus] = {}

    def enqueue(
        self,
        func_path: str,
        payload: dict,
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
            fn = _resolve_callable(func_path)
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

    def _connection(self):
        import redis

        return redis.Redis.from_url(self._redis_url)

    def _queue(self, connection):
        import rq

        return rq.Queue(self._queue_name, connection=connection)

    def enqueue(
        self,
        func_path: str,
        payload: dict,
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
            func_path,
            payload,
            job_id=job_id,
            job_timeout=timeout,
            retry=retry,
            result_ttl=result_ttl,
            failure_ttl=failure_ttl,
        )
        return job.id

    def get(self, job_id: str) -> JobStatus | None:
        import rq

        connection = self._connection()
        try:
            job = rq.job.Job.fetch(job_id, connection=connection)
        except Exception:
            return None

        status = map_rq_status(job.get_status())
        error: dict | None = None
        result: dict | None = None
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
