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
    assert "1. ✓ `deck.md@page=2#para1-2` Distribution Mix Agency share of VONB was 72%." in summary
    assert "### mix — rank=1 page_rank=1" in summary
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
    assert [line for line in summary.splitlines() if line.startswith("### ")] == [
        "### mix — rank=1 page_rank=1",
        "### roe — rank=1 page_rank=1",
        "### content — rank=1 page_rank=1",
        "### free",
    ]


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
    # 每条记录与直接调 RAGSpine.ask 的结果逐字段一致（batch 只编排，不改答案 / 路由 / 来源）。
    with RAGSpine.local(workspace) as rag:
        for spec in _QUESTIONS:
            direct = rag.ask(str(spec["question"]))
            record = records[str(spec["id"])]
            assert record["mode"] == "ask" and record["error"] is None
            assert record["answer"] == (direct.answer_plain or direct.answer)
            assert record["route"] == direct.route
            assert record["fallback"] == direct.fallback  # ADR 0023 回落原因码，未回落为 None
            assert [(x["doc"], x["locator"]) for x in record["sources"]] == [
                (str(x["doc"]), str(x["locator"])) for x in direct.sources
            ]
    assert records["mix"]["page_hit"] is True and records["mix"]["content_hit"] is True
    assert records["mix"]["sources"][0] == {
        "doc": "deck.md",
        "locator": "deck.md@page=2#para1-2",
        "page": 2,
        "hit": True,
    }
    assert records["content"]["page_hit"] is None
    assert records["free"]["page_hit"] is None and records["free"]["content_hit"] is None
    page = [r["page_hit"] for r in records.values() if r["page_hit"] is not None]
    content = [r["content_hit"] for r in records.values() if r["content_hit"] is not None]
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert f"| page_hit | {sum(page) / len(page):.4f} ({sum(page)}/{len(page)}) |" in summary
    assert (
        f"| content_hit | {sum(content) / len(content):.4f} ({sum(content)}/{len(content)}) |"
        in summary
    )


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
    from ragspine.cli.batch import _CLAUDE_CLI_NOTE

    assert f"- {_CLAUDE_CLI_NOTE}" in summary
    assert "- provider: `claude-cli`" in summary and "- concurrency: `6`" in summary


@pytest.mark.parametrize(
    "changed",
    [
        ["--top-k", "5"],
        ["--profile", "balanced"],
        ["--provider", "claude-cli"],
        ["--contextual-index", "heading"],
        [],
    ],
    ids=["top_k", "profile", "provider", "contextual_index", "mode"],
)
def test_resume_refuses_changed_run_settings(tmp_path, workspace, questions, capsys, changed):
    out = tmp_path / "out"
    base = ["batch", str(questions), "--workspace", str(workspace), "--out", str(out)]
    assert main([*base, "--retrieval-only", "--limit", "1"]) == 0
    before = (out / "results.jsonl").read_text(encoding="utf-8")
    mode = [] if not changed else ["--retrieval-only"]  # 空 = 换成端到端模式
    assert main([*base, *mode, *changed, "--resume"]) == 2
    err = capsys.readouterr().err
    assert "--out" in err and "run_settings.json" in err
    assert (out / "results.jsonl").read_text(encoding="utf-8") == before
    pinned = json.loads((out / "run_settings.json").read_text(encoding="utf-8"))
    assert pinned["mode"] == "retrieval-only" and pinned["top_k"] == 10


def test_resume_survives_a_half_written_line(tmp_path, workspace, questions, capsys):
    out = tmp_path / "out"
    base = ["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"]
    assert main([*base, "--out", str(out), "--limit", "1"]) == 0
    with (out / "results.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"id": "roe", "question": "rec')  # 进程被杀时留下的半行，没有换行
    capsys.readouterr()
    assert main([*base, "--out", str(out), "--resume"]) == 0
    assert "warning" in capsys.readouterr().err
    lines = (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines[1] == '{"id": "roe", "question": "rec'  # 坏行原样留着，续写从新的一行开始
    parsed = [json.loads(line) for i, line in enumerate(lines) if i != 1]
    assert [r["id"] for r in parsed] == ["mix", "roe", "content", "free"]


def test_ask_mode_checks_index_compatibility_up_front(tmp_path, questions, capsys):
    ws = tmp_path / "ws-pc"
    deck = tmp_path / "deck.md"
    deck.write_text(_DECK, encoding="utf-8")
    parent_child = {"indexing": {"chunker": "parent_child", "max_chars": 16, "overlap_chars": 0}}
    RAGSpine.local(ws, config=parent_child).ingest(deck)
    out = tmp_path / "out"
    assert main(["batch", str(questions), "--workspace", str(ws), "--out", str(out)]) == 2
    assert "ReindexRequiredError" in capsys.readouterr().err
    assert not (out / "results.jsonl").exists()


def test_unavailable_provider_is_exit_2(workspace, questions, monkeypatch, capsys):
    def broken(*args: object, **kwargs: object) -> MockProvider:
        raise RuntimeError("claude binary not found")

    monkeypatch.setattr("ragspine.agent.claude_cli_provider.ClaudeCliProvider", broken)
    rc = main(["batch", str(questions), "--workspace", str(workspace), "--provider", "claude-cli"])
    assert rc == 2
    assert "claude binary not found" in capsys.readouterr().err


def test_every_question_failing_is_a_nonzero_exit(tmp_path, workspace, questions, monkeypatch):
    def boom(self: RAGSpine, question: str) -> object:
        raise RuntimeError("provider down")

    monkeypatch.setattr(RAGSpine, "ask", boom)
    out = tmp_path / "out"
    assert main(["batch", str(questions), "--workspace", str(workspace), "--out", str(out)]) == 1
    records = _records(out)
    assert len(records) == 4
    assert {r["error"] for r in records} == {"RuntimeError: provider down"}


def test_ask_mode_writes_each_question_as_it_completes(tmp_path, workspace, questions, monkeypatch):
    """首题卡住时，其余已完成的题先落盘（as_completed，不按提交顺序压着）。"""
    import time

    out = tmp_path / "out"
    results = out / "results.jsonl"
    real_ask = RAGSpine.ask
    saw_others_first: list[bool] = []

    def slow_first(self: RAGSpine, question: str):
        if question == _QUESTIONS[0]["question"]:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if results.is_file() and len(results.read_text(encoding="utf-8").splitlines()) == 3:
                    break
                time.sleep(0.02)
            saw_others_first.append(len(results.read_text(encoding="utf-8").splitlines()) == 3)
        return real_ask(self, question)

    monkeypatch.setattr(RAGSpine, "ask", slow_first)
    rc = main(
        ["batch", str(questions), "--workspace", str(workspace), "--out", str(out)]
        + ["--concurrency", "4"]
    )
    assert rc == 0 and saw_others_first == [True]
    records = _records(out)
    assert records[-1]["id"] == "mix"
    assert sorted(r["id"] for r in records) == ["content", "free", "mix", "roe"]


def test_summary_shows_the_actual_vector_channel(tmp_path, workspace, questions):
    """配置 persist_vectors=True 但向量库为空：summary 显示实际退化的 bm25_only，而不是配置值。"""
    out = tmp_path / "out"
    rc = main(
        ["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"]
        + ["--profile", "balanced", "--embedding", "deterministic", "--persist-vectors"]
        + ["--out", str(out)]
    )
    assert rc == 0
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert (
        "- vector_channel: `bm25_only（empty_index, n_vectors=0；persist_vectors=True）`" in summary
    )


def test_summary_shows_a_filled_persisted_vector_channel(tmp_path, questions):
    pytest.importorskip("sqlite_vec")
    ws = tmp_path / "ws-vec"
    deck = tmp_path / "deck.md"
    deck.write_text(_DECK, encoding="utf-8")
    ingest = RAGSpine.local(
        ws, preset="balanced", config={"storage": {"persist_vectors": True}}
    ).ingest(deck)
    assert ingest.vector_report is not None
    n_vectors = ingest.vector_report.total
    out = tmp_path / "out"
    rc = main(
        ["batch", str(questions), "--workspace", str(ws), "--retrieval-only"]
        + ["--profile", "balanced", "--persist-vectors", "--out", str(out)]
    )
    assert rc == 0
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert f"- vector_channel: `hybrid（n_vectors={n_vectors}；persist_vectors=True）`" in summary


def test_summary_vector_channel_for_the_economy_default(tmp_path, workspace, questions):
    out = tmp_path / "out"
    assert (
        main(
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
        == 0
    )
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "- vector_channel: `bm25_only（未开向量通道；persist_vectors=False）`" in summary


def test_resume_reruns_questions_that_errored(tmp_path, workspace, questions, monkeypatch):
    import ragspine.cli.batch as batch

    out = tmp_path / "out"
    base = ["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"]
    real = batch.retrieve_hits

    def flaky(retriever, question, *, top_k):
        if question == "record ROE achieved":
            raise RuntimeError("transient")
        return real(retriever, question, top_k=top_k)

    monkeypatch.setattr(batch, "retrieve_hits", flaky)
    assert main([*base, "--out", str(out)]) == 0
    first = {r["id"]: r for r in _records(out)}
    assert first["roe"]["error"] == "RuntimeError: transient" and first["roe"]["rank"] is None

    asked: list[str] = []

    def spy(retriever, question, *, top_k):
        asked.append(question)
        return real(retriever, question, top_k=top_k)

    monkeypatch.setattr(batch, "retrieve_hits", spy)
    assert main([*base, "--out", str(out), "--resume"]) == 0
    assert asked == ["record ROE achieved"]
    final = batch.read_records(out / "results.jsonl")
    assert final["roe"]["error"] is None and final["roe"]["page_rank"] == 1
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "已判定 3 题，无法判定 1 题，出错 0 题。" in summary


def test_v2_gold_question_set_runs(tmp_path, workspace):
    """nl-answers-gold-v2 题集直接可跑（schema 识别走 nl_gold 的 GOLD_SCHEMA_VERSIONS）。"""
    gold = {
        "schema_version": "nl-answers-gold-v2",
        "changelog": [
            {"case_id": "mix", "change": "pin", "old": "p1", "new": "p2", "evidence": "page 2"}
        ],
        "cases": [
            {
                "case_id": "mix",
                "case_class": "positive",
                "question": {"en": "Agency share of VONB"},
                "expected": {
                    "status": "answered",
                    "required_claims": [{"kind": "quote", "page_index": 1, "quote": "72%"}],
                },
            }
        ],
    }
    path = tmp_path / "gold-v2.json"
    path.write_text(json.dumps(gold), encoding="utf-8")
    out = tmp_path / "out"
    rc = main(
        ["batch", str(path), "--workspace", str(workspace), "--retrieval-only", "--out", str(out)]
    )
    assert rc == 0
    [record] = _records(out)
    assert record["id"] == "mix:en" and record["page_groups"] == [[2]]
    assert record["basis"] == "page" and record["rank"] == 1 and record["page_rank"] == 1


@pytest.mark.parametrize(
    ("flag", "expected"), [([], "off"), (["--contextual-index", "heading"], "heading")]
)
def test_contextual_index_reaches_the_retriever_assembly(
    tmp_path, workspace, questions, monkeypatch, flag, expected
):
    seen: list[ServiceConfig] = []

    @contextmanager
    def fake_open(config: ServiceConfig, provider: LLMProvider) -> Iterator[_FixedRetriever]:
        seen.append(config)
        yield _FixedRetriever()

    monkeypatch.setattr("ragspine.session.open_narrative_retriever", fake_open)
    out = tmp_path / "out"
    base = ["batch", str(questions), "--workspace", str(workspace), "--out", str(out)]
    assert main([*base, "--retrieval-only", *flag]) == 0
    assert seen and {c.contextual_index for c in seen} == {expected}
    pinned = json.loads((out / "run_settings.json").read_text(encoding="utf-8"))
    assert pinned["contextual_index"] == expected


@pytest.mark.parametrize("mode", ["heading", "full"])
def test_contextual_index_under_economy_runs_and_is_shown(tmp_path, workspace, questions, mode):
    """economy 预设下也生效（只影响 BM25 索引文本）；summary 显示实际生效值。"""
    out = tmp_path / "out"
    rc = main(
        ["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"]
        + ["--contextual-index", mode, "--out", str(out)]
    )
    assert rc == 0
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert f"- contextual_index: `{mode}`" in summary
    assert "- retrieval_mode: `economy`" in summary
    records = {r["id"]: r for r in _records(out)}
    assert records["mix"]["page_rank"] == 1


def test_summary_shows_contextual_index_default_off(tmp_path, workspace, questions):
    out = tmp_path / "out"
    base = ["batch", str(questions), "--workspace", str(workspace), "--retrieval-only"]
    assert main([*base, "--out", str(out), "--limit", "1"]) == 0
    assert "- contextual_index: `off`" in (out / "summary.md").read_text(encoding="utf-8")
    # 显式 off 与缺省是同一实际值：续跑不算配置变化。
    assert main([*base, "--out", str(out), "--resume", "--contextual-index", "off"]) == 0
