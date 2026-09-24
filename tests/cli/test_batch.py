"""``ragspine batch``：题集批量问答 / retrieval-only 评测的编排（resume、并发、落盘、缺库报错）。

全部离线：虚构 DI markdown 入库（按段切块，locator 带 ``@page=N``），MockProvider，零模型零网络。
local-http（Qwen embedding / reranker）只验证配置透传，检索组装被桩掉，不连模型服务。
"""

import json
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import LLMProvider, MockProvider
from ragspine.cli import main
from ragspine.common.observability.trace import TRACE_LOGGER_NAME
from ragspine.service.config import ServiceConfig
from ragspine.session import RAGSpine

_DECK = """# Fictional Deck

Welcome to the results.

<!-- PageBreak -->

# Distribution Mix

Agency share of VONB was 72%. Partnerships share of VONB was 28%.

<!-- PageBreak -->

# Returns

The record ROE of 17.5% was achieved.
"""

_QUESTIONS = [
    {"id": "mix", "question": "Agency share of VONB", "pages": [2], "expected": "72%"},
    {"id": "roe", "question": "record ROE achieved", "page": 3},
    {"id": "content", "question": "Partnerships share of VONB", "expected": "28%"},
    {"id": "free", "question": "Welcome to the results"},
]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    deck = tmp_path / "deck.md"
    deck.write_text(_DECK, encoding="utf-8")
    RAGSpine.local(ws).ingest(deck)
    return ws


@pytest.fixture
def questions(tmp_path: Path) -> Path:
    path = tmp_path / "q.jsonl"
    path.write_text("\n".join(json.dumps(q) for q in _QUESTIONS) + "\n", encoding="utf-8")
    return path


def _records(out: Path) -> list[dict[str, object]]:
    lines = (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _stable(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        ({k: v for k, v in r.items() if k != "seconds"} for r in records),
        key=lambda r: str(r["id"]),
    )


# ---------------------------------------------------------------------------
# 接入点
# ---------------------------------------------------------------------------


def test_batch_help_is_a_real_subcommand(capsys):
    """`batch` 登记在已知命令里——否则会被当成 `workflow create` 的自然语言请求。"""
    with pytest.raises(SystemExit) as exc:
        main(["batch", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--retrieval-only" in out and "--resume" in out and "claude-cli" in out


def test_missing_workspace_errors_without_creating_it(tmp_path, questions, capsys):
    missing = tmp_path / "nope"
    rc = main(["batch", str(questions), "--workspace", str(missing), "--retrieval-only"])
    assert rc == 2
    assert not missing.exists()
    assert str(missing) in capsys.readouterr().err


def test_workspace_without_chunks_errors_for_retrieval_only(tmp_path, questions, capsys):
    ws = tmp_path / "empty"
    RAGSpine.local(ws)  # 建了空库，但没有入库任何叙事块
    rc = main(["batch", str(questions), "--workspace", str(ws), "--retrieval-only"])
    assert rc == 2
    assert "ragspine ingest" in capsys.readouterr().err


def test_bad_question_set_errors(tmp_path, workspace, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "a", "question": "Q"}\n{"id": "a", "question": "Q"}\n')
    rc = main(["batch", str(bad), "--workspace", str(workspace), "--retrieval-only"])
    assert rc == 2
    assert "重复" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# retrieval-only
# ---------------------------------------------------------------------------


def test_retrieval_only_end_to_end(tmp_path, workspace, questions, capsys):
    out = tmp_path / "out"
    rc = main(
        [
            "batch",
            str(questions),
            "--workspace",
            str(workspace),
            "--retrieval-only",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    records = {r["id"]: r for r in _records(out)}
    assert set(records) == {"mix", "roe", "content", "free"}
    assert records["mix"]["basis"] == "page" and records["mix"]["page_rank"] == 1
    assert records["roe"]["page_rank"] == 1
    assert records["content"]["basis"] == "content" and records["content"]["rank"] == 1
    assert records["free"]["basis"] == "none"
    hits = records["mix"]["hits"]
    assert hits[0]["hit"] is True and hits[0]["page"] == 2 and "Agency" in hits[0]["text"]

    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert re.search(r"\| page_recall \| 1\.0000 \|", summary)
    assert "entity/period" in summary  # (c) 与 ask 的差异写明
    assert "✓" in summary
    assert str(out) in capsys.readouterr().out


def test_limit_and_resume_skip_done_questions(tmp_path, workspace, questions, monkeypatch):
    out = tmp_path / "out"
    base = ["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"]
    assert main([*base, "--out", str(out), "--limit", "2"]) == 0
    assert [r["id"] for r in _records(out)] == ["mix", "roe"]

    import ragspine.cli.batch as batch

    asked: list[str] = []
    real = batch.retrieve_hits

    def spy(retriever, question, *, top_k):
        asked.append(question)
        return real(retriever, question, top_k=top_k)

    monkeypatch.setattr(batch, "retrieve_hits", spy)
    assert main([*base, "--out", str(out), "--resume"]) == 0
    assert asked == ["Partnerships share of VONB", "Welcome to the results"]
    assert sorted(r["id"] for r in _records(out)) == ["content", "free", "mix", "roe"]
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "free" in summary and "mix" in summary


def test_existing_results_need_resume(tmp_path, workspace, questions, capsys):
    out = tmp_path / "out"
    base = ["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"]
    assert main([*base, "--out", str(out), "--limit", "1"]) == 0
    assert main([*base, "--out", str(out)]) == 2
    assert "--resume" in capsys.readouterr().err
    assert main([*base, "--resume"]) == 2  # 续跑必须指明 --out


def test_concurrency_matches_serial(tmp_path, workspace, questions):
    base = ["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"]
    assert main([*base, "--out", str(tmp_path / "serial")]) == 0
    assert main([*base, "--out", str(tmp_path / "par"), "--concurrency", "2"]) == 0
    assert _stable(_records(tmp_path / "serial")) == _stable(_records(tmp_path / "par"))


def test_default_out_dir_is_data_output_batch(tmp_path, workspace, questions, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"])
    assert rc == 0
    runs = list((tmp_path / "data" / "output" / "batch").glob("q-*"))
    assert len(runs) == 1 and (runs[0] / "summary.md").is_file()


# ---------------------------------------------------------------------------
# 端到端（ask）
# ---------------------------------------------------------------------------


def test_end_to_end_mock_records_answer_route_sources(tmp_path, workspace, questions):
    out = tmp_path / "e2e"
    rc = main(
        ["batch", str(questions), "--workspace", str(workspace), "--provider", "mock"]
        + ["--out", str(out), "--concurrency", "2"]
    )
    assert rc == 0
    records = {r["id"]: r for r in _records(out)}
    mix = records["mix"]
    assert mix["mode"] == "ask" and mix["answer"] and mix["route"]
    assert mix["sources"] and mix["page_hit"] is True
    assert "fallback" in mix  # ADR 0023：结构化回落叙事时记原因代码，否则为 None
    assert records["free"]["page_hit"] is None and records["free"]["content_hit"] is None
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "page_hit" in summary


# ---------------------------------------------------------------------------
# (d) 答案 / 片段只进评测产物，绝不进 observability trace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [["--retrieval-only"], []])
def test_batch_never_puts_text_into_traces(tmp_path, workspace, questions, caplog, mode):
    caplog.set_level(logging.DEBUG, logger=TRACE_LOGGER_NAME)
    out = tmp_path / "out"
    assert (
        main(["batch", str(questions), "--workspace", str(workspace), "--out", str(out), *mode])
        == 0
    )
    records = _records(out)
    assert "Agency share" in (out / "results.jsonl").read_text(encoding="utf-8")
    trace_records = [r for r in caplog.records if r.name == TRACE_LOGGER_NAME]
    assert trace_records  # 计数型 trace 照常发
    # 字段经 extra 挂在 LogRecord 属性上：连同属性一起扫，而不只是固定的 "trace" message。
    traced = "\n".join(repr(vars(r)) for r in trace_records)
    for text in ("Agency share", "72%", "17.5%", "record ROE"):
        assert text not in traced
    for record in records:
        answer = str(record.get("answer") or "")
        if answer:
            assert answer not in traced


# ---------------------------------------------------------------------------
# (a) 真实检索配置（Qwen embedding / reranker via local-http）经 CLI 透传到组装
# ---------------------------------------------------------------------------


class _FixedRetriever:
    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]:
        return [{"doc_id": "deck.md", "source_locator": "deck.md@page=2#para1-1", "text": "x"}]


def test_local_http_flags_reach_the_retriever_assembly(tmp_path, workspace, questions, monkeypatch):
    seen: list[ServiceConfig] = []

    @contextmanager
    def fake_open(config: ServiceConfig, provider: LLMProvider) -> Iterator[_FixedRetriever]:
        seen.append(config)
        yield _FixedRetriever()

    monkeypatch.setattr("ragspine.session.open_narrative_retriever", fake_open)
    rc = main(
        ["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"]
        + ["--profile", "balanced", "--embedding", "local-http", "--reranker", "local-http"]
        + ["--persist-vectors", "--out", str(tmp_path / "out")]
    )
    assert rc == 0
    assert seen
    assert {(c.embedding, c.reranker, c.persist_vectors) for c in seen} == {
        ("local-http", "local-http", True)
    }
    summary = (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")
    assert "local-http" in summary


def test_invalid_retrieval_combo_is_an_honest_error(workspace, questions, capsys):
    rc = main(
        ["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"]
        + ["--embedding", "local-http"]  # economy 预设不允许 embedding
    )
    assert rc == 2
    assert "--profile" in capsys.readouterr().err


def test_claude_cli_provider_and_concurrency_note(tmp_path, workspace, questions, monkeypatch):
    built: list[object] = []

    def fake_cli(*args: object, **kwargs: object) -> MockProvider:
        built.append(kwargs)
        return MockProvider()

    monkeypatch.setattr("ragspine.agent.claude_cli_provider.ClaudeCliProvider", fake_cli)
    out = tmp_path / "out"
    rc = main(
        ["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"]
        + ["--provider", "claude-cli", "--concurrency", "6", "--out", str(out)]
    )
    assert rc == 0 and built
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "claude-cli" in summary and "4" in summary and "排队" in summary
