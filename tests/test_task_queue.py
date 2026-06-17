"""task_queue 规格（TDD）：FakeQueue 同步内联执行 + map_rq_status 纯函数映射。

不依赖 Redis/rq。样例 job 函数定义在 MODULE LEVEL，便于以点路径
"tests.test_task_queue.<fn>" 被 importlib 解析。
"""

from ragspine.service.tasks.task_queue import (
    JOB_QUEUED,
    JOB_STARTED,
    JOB_FINISHED,
    JOB_FAILED,
    JobStatus,
    JobError,
    FakeQueue,
    map_rq_status,
)

# 模块级调用计数器，用于验证幂等（同 job_id 不重跑）
_CALL_COUNT: dict[str, int] = {}


def job_ok(payload: dict) -> dict:
    _CALL_COUNT["ok"] = _CALL_COUNT.get("ok", 0) + 1
    return {"echo": payload.get("x"), "ran": _CALL_COUNT["ok"]}


def job_plain_boom(payload: dict) -> dict:
    raise ValueError("plain boom")


def job_typed_boom(payload: dict) -> dict:
    raise JobError("typed boom", stage="ingest", retryable=True)


def setup_function(_fn) -> None:
    _CALL_COUNT.clear()


def test_enqueue_returns_job_id():
    q = FakeQueue()
    jid = q.enqueue("tests.test_task_queue.job_ok", {"x": 1})
    assert isinstance(jid, str) and jid


def test_fake_runs_inline_and_finishes():
    q = FakeQueue()
    jid = q.enqueue("tests.test_task_queue.job_ok", {"x": 42})
    st = q.get(jid)
    assert isinstance(st, JobStatus)
    assert st.id == jid
    assert st.status == JOB_FINISHED
    assert st.result == {"echo": 42, "ran": 1}
    assert st.error is None


def test_failing_job_plain_exception():
    q = FakeQueue()
    jid = q.enqueue("tests.test_task_queue.job_plain_boom", {})
    st = q.get(jid)
    assert st.status == JOB_FAILED
    assert st.result is None
    assert set(st.error.keys()) == {"type", "message", "stage", "retryable"}
    assert st.error["type"] == "ValueError"
    assert st.error["message"] == "plain boom"
    assert st.error["stage"] == "execution"
    assert st.error["retryable"] is False


def test_failing_job_joberror_propagates_stage_retryable():
    q = FakeQueue()
    jid = q.enqueue("tests.test_task_queue.job_typed_boom", {})
    st = q.get(jid)
    assert st.status == JOB_FAILED
    assert st.error["type"] == "JobError"
    assert st.error["message"] == "typed boom"
    assert st.error["stage"] == "ingest"
    assert st.error["retryable"] is True


def test_enqueue_never_raises_out_on_failure():
    q = FakeQueue()
    # 不应抛出，只把失败记进 JobStatus
    jid = q.enqueue("tests.test_task_queue.job_plain_boom", {})
    assert q.get(jid).status == JOB_FAILED


def test_idempotent_explicit_job_id_does_not_rerun():
    q = FakeQueue()
    jid1 = q.enqueue("tests.test_task_queue.job_ok", {"x": 1}, job_id="fixed")
    assert jid1 == "fixed"
    assert _CALL_COUNT.get("ok") == 1
    # 再次提交同 job_id：返回同 id，不重跑
    jid2 = q.enqueue("tests.test_task_queue.job_ok", {"x": 2}, job_id="fixed")
    assert jid2 == "fixed"
    assert _CALL_COUNT.get("ok") == 1  # 计数没变 -> 没重跑
    st = q.get("fixed")
    assert st.result == {"echo": 1, "ran": 1}  # 仍是首次结果


def test_get_unknown_returns_none():
    q = FakeQueue()
    assert q.get("nope") is None


def test_payload_stays_plain_serializable_dict():
    import json

    q = FakeQueue()
    payload = {"path": "/tmp/a.xlsx", "dry_run": True, "tags": ["x", "y"]}
    jid = q.enqueue("tests.test_task_queue.job_ok", payload)
    st = q.get(jid)
    # payload 与结果都能 json round-trip（即纯可序列化）
    json.dumps(payload)
    json.dumps(st.result)


def test_map_rq_status_all_mappings():
    assert map_rq_status("queued") == JOB_QUEUED
    assert map_rq_status("deferred") == JOB_QUEUED
    assert map_rq_status("scheduled") == JOB_QUEUED
    assert map_rq_status("started") == JOB_STARTED
    assert map_rq_status("finished") == JOB_FINISHED
    assert map_rq_status("failed") == JOB_FAILED
    assert map_rq_status("stopped") == JOB_FAILED
    assert map_rq_status("canceled") == JOB_FAILED
    assert map_rq_status(None) == JOB_QUEUED
    assert map_rq_status("weird-unknown") == JOB_QUEUED


def test_job_error_defaults():
    e = JobError("boom")
    assert e.stage == "execution"
    assert e.retryable is False
    assert str(e) == "boom"
