"""batch 端到端模式的逐次 LLM 调用（ADR 0028）：按线程旁听请求 trace 的 `llm_calls`，多线程并发不串线，
summary 的"LLM 调用（按阶段）"表与手算结果一致。"""

import os
import random
import time

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine import RAGSpine
from ragspine.agent.agent import AgentResult
from ragspine.cli.batch import MODE_ASK, _run_ask, render_summary
from ragspine.common.observability import emit_trace
from ragspine.common.observability.llm_calls import LLMCall, llm_trace_fields
from ragspine.eval.retrieval_only import BatchQuestion


def _call(stage: str, ms: int, *, attempts: int = 1) -> LLMCall:
    return LLMCall(
        stage=stage,
        ms=ms,
        attempts=attempts,
        trunc_retries=attempts - 1,
        retried=attempts > 1,
        truncated=False,
        reasoning_disabled=False,
        in_tokens=None,
        out_tokens=None,
        error="",
    )


class _StubRag(RAGSpine):
    """题目 `q-N`：发 N 条请求 trace，每条一次调用，ms 编码题号（N*1000+i）；N 为偶数时再发一条分解父 trace。"""

    def __init__(self) -> None:  # 不开 workspace：只替换 ask
        pass

    def ask(self, question: str) -> AgentResult:
        n = int(question.split("-")[1])
        for i in range(n):
            time.sleep(random.uniform(0, 0.003))
            emit_trace(
                None,
                request_id=f"{n}-{i}",
                tool_status_counts={"found": 0, "not_found": 0, "unrecognized": 0},
                **llm_trace_fields([_call("synthesis", n * 1000 + i)]),
            )
        if n % 2 == 0:
            emit_trace(
                None,
                request_id=f"{n}-parent",
                route="decomposed",
                n_subquestions=n,
                **llm_trace_fields([_call("decompose", n * 1000 + 999)]),
            )
        return AgentResult(answer="a", route="narrative")


def test_concurrent_questions_do_not_mix_llm_calls():
    pending = [BatchQuestion(id=f"q{n}", question=f"q-{n}") for n in range(1, 13)]
    records: list[dict] = []
    _run_ask(_StubRag(), pending, concurrency=4, emit=records.append)
    assert len(records) == 12
    for record in records:
        n = int(record["question"].split("-")[1])
        trace = record["trace"]
        assert trace["requests"] == n  # 分解父 trace 不计入 requests
        extra = 1 if n % 2 == 0 else 0
        assert trace["llm_n_calls"] == n + extra
        assert {c["ms"] // 1000 for c in trace["llm_calls"]} == {n}
        assert trace["llm_ms"] == sum(c["ms"] for c in trace["llm_calls"])
        assert trace["llm_n_retried"] == 0
        stages = [c["stage"] for c in trace["llm_calls"]]
        assert stages.count("decompose") == extra


def _record(qid: str, seconds: float, calls: list[LLMCall]) -> dict:
    fields = llm_trace_fields(calls)
    return {
        "id": qid,
        "question": qid,
        "mode": MODE_ASK,
        "answer": "a",
        "route": "narrative",
        "fallback": None,
        "sources": [],
        "page_hit": None,
        "content_hit": None,
        "seconds": seconds,
        "error": None,
        "trace": {
            "requests": 1,
            "input_tokens": None,
            "output_tokens": None,
            "page_images_sent": 0,
            "page_images_dropped": 0,
            "number_guard_rewrites": 0,
            "llm_calls": fields.get("llm_calls", []),
            "llm_n_calls": fields.get("llm_n_calls", 0),
            "llm_n_retried": fields.get("llm_n_retried", 0),
            "llm_ms": fields.get("llm_ms", 0),
        },
    }


def test_summary_llm_stage_table_matches_hand_computation():
    records = [
        _record("a", 1.0, [_call("synthesis", 100), _call("tool_round", 50, attempts=2)]),
        _record("b", 3.0, [_call("synthesis", 300), _call("translation", 50, attempts=3)]),
    ]
    summary = render_summary(records, {"mode": MODE_ASK, "provider": "mock"}, top_k=10)
    assert "## LLM 调用（按阶段）" in summary
    # 次数 / 每题平均 / 平均 ms / 最大 ms / 耗时占比（Σ500）/ 重试率
    assert "| translation | 1 | 0.50 | 50.0 | 50 | 10.0% | 100.0% (1/1) |" in summary
    assert "| tool_round | 1 | 0.50 | 50.0 | 50 | 10.0% | 100.0% (1/1) |" in summary
    assert "| synthesis | 2 | 1.00 | 200.0 | 300 | 80.0% | 0.0% (0/2) |" in summary
    # 按 stage 闭集顺序排列
    assert (
        summary.index("| translation |")
        < summary.index("| tool_round |")
        < summary.index("| synthesis |")
    )
    assert (
        "全部 4 次调用，重试率 50.0% (2/4)；Σllm_ms = 500 ms；"
        "未出错题的 Σllm_ms（500 ms）占其端到端延迟合计（4.00 s）的 12.5%"
    ) in summary
    assert "llm_ms 是各次调用耗时之和，不是墙钟时间" in summary


def test_summary_latency_share_excludes_errored_questions():
    """延迟占比与 _cost_rows 同口径：出错题的耗时与 llm_ms 都不计入占比（表内计数仍含它的调用）。"""
    ok = _record("a", 2.0, [_call("synthesis", 100)])
    failed = _record("b", 9.0, [_call("synthesis", 700)])
    failed.update(error="ProviderError: x", route="error")
    summary = render_summary([ok, failed], {"mode": MODE_ASK, "provider": "mock"}, top_k=10)
    assert "全部 2 次调用" in summary and "Σllm_ms = 800 ms" in summary
    assert "未出错题的 Σllm_ms（100 ms）占其端到端延迟合计（2.00 s）的 5.0%" in summary
    assert "| 延迟 p50 / p95（秒） | 2.00 / 2.00 |" in summary


def test_summary_without_llm_calls_has_no_llm_table():
    record = _record("a", 1.0, [])
    summary = render_summary([record], {"mode": MODE_ASK, "provider": "mock"}, top_k=10)
    assert "LLM 调用（按阶段）" not in summary
