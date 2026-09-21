"""The local answer journal records the prompt as sent and the result that came back."""

import json
import logging
import re
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.answer_audit import (
    AnswerAuditContext,
    AnswerAuditStore,
    format_record,
    format_summaries,
    list_answers,
    open_audit_store,
    read_answer,
)
from enterprise_pdf_rag.adapters.answer_service import AnswerService, DependencyUnavailable
from enterprise_pdf_rag.answers.models import (
    AbstainReason,
    AnswerRequest,
    AnswerResult,
    AnswerStatus,
    ClaimCitation,
    ClaimKind,
    FusedHit,
    MemberFilters,
    PageWindowStat,
    RejectedClaim,
    VerifiedClaim,
)
from enterprise_pdf_rag.answers.prompt import SYSTEM_RULES, ModelAnswer
from enterprise_pdf_rag.processing.context_builder import BlockKind
from tests.enterprise_pdf_rag.answers.fake_llm import answered, chart_claim, scripted_client
from tests.enterprise_pdf_rag.answers.store_mounted_document import bar_document

_MEMBER_LINE = re.compile(r"^\[(?:m\d+ \| )?member ([0-9a-f]{64})\] kind=(\w+)", re.MULTILINE)
_QUESTION = "What was the expense ratio in 1H21?"
_SHA = "a" * 64


def _chart_script(prompt: str) -> ModelAnswer:
    chart = next(member for member, kind in _MEMBER_LINE.findall(prompt) if kind == "chart")
    return answered("The expense ratio in 1H21 was 15%.", chart_claim(chart, "p-1H21", "15%"))


def _context(question: str = _QUESTION, prompt: str = "Question:\nwhat") -> AnswerAuditContext:
    return AnswerAuditContext(
        question,
        _SHA,
        "b" * 64,
        "c" * 64,
        SYSTEM_RULES,
        prompt,
        ("m" * 64,),
        (FusedHit("c" * 64, "m" * 64, 0.5, 1, 2, 0.9, 3.25),),
        (PageWindowStat(4, 3, 512, False),),
        "rrf",
        MemberFilters(("1H21",), ("China",)),
        True,
        "expense ratio 1H21",
    )


def _result(status: AnswerStatus = AnswerStatus.ANSWERED) -> AnswerResult:
    citation = ClaimCitation(
        "m" * 64, BlockKind.CHART, 4, "points.p-1H21.value", ("svg-1",), None, "15%", page_title="P"
    )
    return AnswerResult(
        status,
        "The expense ratio in 1H21 was 15%." if status is AnswerStatus.ANSWERED else None,
        (VerifiedClaim("c1", ClaimKind.CHART_VALUE, "15%", Decimal("15"), "%", (citation,)),),
        (RejectedClaim("c2", "m" * 64, "points.p.value", "9%", AbstainReason.UNKNOWN_POINT, "no"),),
        None if status is AnswerStatus.ANSWERED else AbstainReason.NO_VERIFIED_CLAIM,
        None if status is AnswerStatus.ANSWERED else "nothing survived",
        _SHA,
        "b" * 64,
        "c" * 64,
        ("m" * 64,),
        (FusedHit("c" * 64, "m" * 64, 0.5, 1, 2, 0.9, 3.25),),
        "f" * 64,
        1,
        False,
    )


def test_a_row_is_opened_before_the_call_and_closed_with_the_result(tmp_path: Path) -> None:
    store = AnswerAuditStore(tmp_path / "journal" / "answers-audit.sqlite")
    assert store.path.is_file()
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert {
        "answers_request_fingerprint",
        "answers_started_at",
        "answers_document_sha256",
    } <= indexes

    row_id = store.begin(_context())
    assert row_id is not None

    (opened,) = list_answers(store.path)
    assert (opened.id, opened.status, opened.question) == (row_id, None, _QUESTION)
    assert opened.prompt_chars == len("Question:\nwhat") and opened.request_fingerprint is None

    store.finish(row_id, _result(), model_output_raw='{"abstain": false}')

    (closed,) = list_answers(store.path)
    assert (closed.status, closed.request_fingerprint, closed.llm_live_calls) == (
        "answered",
        "f" * 64,
        1,
    )
    assert closed.cache_hit is False and closed.elapsed_ms is not None and closed.error is None

    record = read_answer(store.path, row_id)
    assert record is not None
    assert record.prompt_system == SYSTEM_RULES and record.prompt_user == "Question:\nwhat"
    assert record.model_output_raw == '{"abstain": false}'
    assert record.answer_text == "The expense ratio in 1H21 was 15%."
    assert record.translated_question == "expense ratio 1H21"
    assert record.filters_relaxed is True and record.fusion_mode == "rrf"
    assert json.loads(record.filters_applied or "") == {
        "periods": ["1H21"],
        "regions": ["China"],
    }
    assert json.loads(record.member_ids) == ["m" * 64]
    assert json.loads(record.page_windows) == [
        {"page_index": 4, "member_count": 3, "chars": 512, "truncated": False}
    ]
    (fused,) = json.loads(record.fused)
    assert fused == {
        "member_id": "m" * 64,
        "fused_score": 0.5,
        "vector_rank": 1,
        "lexical_rank": 2,
        "vector_score": 0.9,
        "bm25_score": 3.25,
    }
    (verified,) = json.loads(record.claims_verified or "")
    assert verified["text"] == "15%" and verified["value"] == "15" and verified["unit"] == "%"
    assert verified["citations"][0]["field_path"] == "points.p-1H21.value"
    (rejected,) = json.loads(record.claims_rejected or "")
    assert rejected["reason"] == "unknown_point"
    assert read_answer(store.path, row_id + 99) is None


def test_queries_filter_by_fingerprint_and_question_and_keep_the_last_n(tmp_path: Path) -> None:
    store = AnswerAuditStore(tmp_path / "answers-audit.sqlite")
    for index in range(4):
        row_id = store.begin(_context(question=f"question {index}"))
        assert row_id is not None
        if index == 3:
            store.finish(row_id, _result())

    assert [row.question for row in list_answers(store.path, last=2)] == [
        "question 2",
        "question 3",
    ]
    assert [row.id for row in list_answers(store.path, fingerprint="ffff")] == [4]
    assert [row.question for row in list_answers(store.path, question_like="tion 1")] == [
        "question 1"
    ]
    assert list_answers(store.path, question_like="nothing here") == ()
    assert "(no matching answers)" in format_summaries(())
    printed = format_summaries(list_answers(store.path))
    assert "question 3" in printed and "answered" in printed
    record = read_answer(store.path, 4)
    assert record is not None
    assert "Question:\nwhat" in format_record(record)


def test_the_service_journals_the_exact_prompt_it_sent_and_the_result_it_returned(
    tmp_path: Path,
) -> None:
    document, _ = bar_document(tmp_path)
    client, prompts = scripted_client(tmp_path / "llm", _chart_script)
    store = AnswerAuditStore(tmp_path / "answers-audit.sqlite")
    service = AnswerService({document.source_sha256: document}, client, audit=store)

    result = service.answer(AnswerRequest(_QUESTION))

    assert result.status is AnswerStatus.ANSWERED
    (row,) = list_answers(store.path)
    record = read_answer(store.path, row.id)
    assert record is not None
    assert row.question == _QUESTION
    assert row.request_fingerprint == result.request_fingerprint
    assert (row.status, row.llm_live_calls, row.cache_hit) == ("answered", 1, False)
    assert row.document_sha256 == result.document_sha256
    assert (record.processing_id, record.snapshot_id) == (result.processing_id, result.snapshot_id)
    assert json.loads(record.member_ids) == list(result.member_ids)
    assert record.answer_text == result.answer
    assert record.fusion_mode == result.fusion_mode
    assert record.prompt_user == prompts[0] and row.prompt_chars == len(prompts[0])
    assert record.prompt_system == SYSTEM_RULES
    (claim,) = json.loads(record.claims_verified or "")
    assert claim["claim_id"] == result.claims[0].claim_id
    assert json.loads(record.model_output_raw or "")["answer"] == result.answer

    # The stored prompt is what the transport really carried: the request envelope
    # ``json_completion`` keeps beside its cache record quotes the same user message.
    envelope = json.loads(
        (tmp_path / "llm" / "contexts" / f"{result.request_fingerprint}.json").read_text(
            encoding="utf-8"
        )
    )
    messages = envelope["payload"]["messages"]
    assert messages[1]["content"] == record.prompt_user
    assert messages[0]["content"] == record.prompt_system


def test_a_failed_call_closes_the_row_with_its_error(tmp_path: Path) -> None:
    document, _ = bar_document(tmp_path)
    store = AnswerAuditStore(tmp_path / "answers-audit.sqlite")

    invalid, _ = scripted_client(tmp_path / "invalid", lambda _prompt: "not json at all")
    abstained = AnswerService({document.source_sha256: document}, invalid, audit=store).answer(
        AnswerRequest(_QUESTION)
    )
    assert abstained.abstain_reason is AbstainReason.MODEL_OUTPUT_INVALID

    exhausted, _ = scripted_client(tmp_path / "exhausted", _chart_script, max_live_calls=0)
    with pytest.raises(DependencyUnavailable, match="call_budget_exhausted"):
        AnswerService({document.source_sha256: document}, exhausted, audit=store).answer(
            AnswerRequest(_QUESTION)
        )

    rejected, raised = list_answers(store.path)
    assert (rejected.status, rejected.error) == ("abstained", "invalid_model_json")
    assert rejected.abstain_reason == "model_output_invalid"
    assert (raised.status, raised.error) == (None, "call_budget_exhausted")
    assert raised.elapsed_ms is not None
    # Even a raised answer keeps the prompt that was about to be sent.
    record = read_answer(store.path, raised.id)
    assert record is not None and record.prompt_user.startswith("Question:")
    assert record.model_output_raw is None and record.answer_text is None


def test_a_broken_journal_warns_and_never_changes_the_answer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    document, _ = bar_document(tmp_path)
    path = tmp_path / "answers-audit.sqlite"
    store = AnswerAuditStore(path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    path.write_bytes(b"definitely not a sqlite database")

    client, _ = scripted_client(tmp_path / "llm", _chart_script)
    service = AnswerService({document.source_sha256: document}, client, audit=store)
    with caplog.at_level(logging.WARNING):
        result = service.answer(AnswerRequest(_QUESTION))

    assert result.status is AnswerStatus.ANSWERED
    assert "could not open a journal row" in caplog.text
    # A row that was never opened is never closed either, and nothing raises.
    store.finish(1, _result())
    assert "could not close journal row 1" in caplog.text


def test_a_journal_that_cannot_be_opened_is_reported_and_disabled(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    occupied = tmp_path / "answers-audit.sqlite"
    occupied.write_text("a file where the journal wants a directory", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert open_audit_store(occupied / "nested.sqlite") is None
    assert "journal disabled" in caplog.text
    assert open_audit_store(tmp_path / "new" / "tree" / "journal.sqlite") is not None


def test_the_document_catalog_composition_root_honours_the_audit_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enterprise_pdf_rag.adapters.http import app as app_module
    from enterprise_pdf_rag.core.settings import get_settings

    root = tmp_path / "ingestion"
    root.mkdir()
    journal = tmp_path / "journal" / "answers-audit.sqlite"
    monkeypatch.setenv("APP_EXECUTION_MODE", "document-catalog")
    monkeypatch.setenv("APP_INGESTION_DIR", str(root))
    monkeypatch.setenv("APP_ANSWER_AUDIT_PATH", str(journal))
    monkeypatch.delenv("APP_LEGACY_DOCUMENT_ROOTS", raising=False)
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_API_KEY",
        "RERANK_BASE_URL",
        "RERANK_MODEL",
        "RERANK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    try:
        monkeypatch.setenv("APP_ANSWER_AUDIT_ENABLED", "false")
        get_settings.cache_clear()
        assert get_settings().answer_audit_file == journal
        app_module.create_configured_app()
        assert not journal.exists()

        monkeypatch.setenv("APP_ANSWER_AUDIT_ENABLED", "true")
        get_settings.cache_clear()
        app_module.create_configured_app()
        assert journal.is_file()
    finally:
        get_settings.cache_clear()


def test_the_default_journal_sits_under_the_ingestion_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enterprise_pdf_rag.core.settings import get_settings

    monkeypatch.setenv("APP_INGESTION_DIR", str(tmp_path / "ingestion"))
    monkeypatch.delenv("APP_ANSWER_AUDIT_PATH", raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.answer_audit_enabled is True
        assert settings.answer_audit_file == settings.ingestion_root / "answers-audit.sqlite"
    finally:
        get_settings.cache_clear()


def test_a_service_without_a_journal_writes_nothing(tmp_path: Path) -> None:
    document, _ = bar_document(tmp_path)
    client, _ = scripted_client(tmp_path / "llm", _chart_script)
    service = AnswerService({document.source_sha256: document}, client)

    assert service.answer(AnswerRequest(_QUESTION)).status is AnswerStatus.ANSWERED
    assert list(tmp_path.rglob("*.sqlite")) == []
