"""A local SQLite journal of every answer: the exact prompt sent, and how it turned out.

One row per question, written twice. The first write lands *before* the model is called
and holds the final prompt verbatim — system rules and user message, the two strings the
transport is about to send — plus everything retrieval had already decided: the filters,
the fused ranking with each channel's seat and score, the members that reached the prompt
and the page windows printed beside them. The second write closes that same row with the
model's raw output, the verified claims, the rejections and the answer the caller got.

A journal write never changes an answer: every failure is a warning and the chain
continues. Unlike ``ragspine``'s privacy-aware traces (codes, counts and timings only)
this file keeps the evidence text verbatim, because retracing *why* one answer came out
the way it did is the only reason it exists. It is a local file under the ingestion root,
never served, shipped or sent anywhere.
"""

import json
import logging
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from enterprise_pdf_rag.answers.models import (
    AnswerResult,
    AnswerStatus,
    FusedHit,
    MemberFilters,
    PageWindowStat,
    RejectedClaim,
    VerifiedClaim,
)
from enterprise_pdf_rag.answers.query_mode import QueryMode

_LOGGER: Final = logging.getLogger(__name__)

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    request_fingerprint TEXT,
    question TEXT NOT NULL,
    translated_question TEXT,
    document_sha256 TEXT NOT NULL,
    processing_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    filters_applied TEXT,
    filters_relaxed INTEGER NOT NULL,
    fusion_mode TEXT NOT NULL,
    page_windows TEXT NOT NULL,
    member_ids TEXT NOT NULL,
    fused TEXT NOT NULL,
    prompt_system TEXT NOT NULL,
    prompt_user TEXT NOT NULL,
    prompt_chars INTEGER NOT NULL,
    model_output_raw TEXT,
    llm_live_calls INTEGER,
    cache_hit INTEGER,
    status TEXT,
    abstain_reason TEXT,
    abstain_detail TEXT,
    claims_verified TEXT,
    claims_rejected TEXT,
    answer_text TEXT,
    elapsed_ms INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS answers_request_fingerprint ON answers (request_fingerprint);
CREATE INDEX IF NOT EXISTS answers_started_at ON answers (started_at);
CREATE INDEX IF NOT EXISTS answers_document_sha256 ON answers (document_sha256);
"""

_INSERT: Final = """
INSERT INTO answers (
    started_at, question, translated_question, document_sha256, processing_id, snapshot_id,
    filters_applied, filters_relaxed, fusion_mode, page_windows, member_ids, fused,
    prompt_system, prompt_user, prompt_chars
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE: Final = """
UPDATE answers SET
    finished_at = ?, request_fingerprint = ?, model_output_raw = ?, llm_live_calls = ?,
    cache_hit = ?, status = ?, abstain_reason = ?, abstain_detail = ?, claims_verified = ?,
    claims_rejected = ?, answer_text = ?, elapsed_ms = ?, error = ?
WHERE id = ?
"""

_SUMMARY_COLUMNS: Final = (
    "id, started_at, elapsed_ms, status, abstain_reason, llm_live_calls, cache_hit, "
    "prompt_chars, request_fingerprint, document_sha256, question, error"
)
_RECORD_COLUMNS: Final = (
    "finished_at, translated_question, processing_id, snapshot_id, filters_applied, "
    "filters_relaxed, fusion_mode, page_windows, member_ids, fused, prompt_system, "
    "prompt_user, model_output_raw, answer_text, claims_verified, claims_rejected"
)


@dataclass(frozen=True, slots=True)
class AnswerAuditContext:
    """Everything known about one answer before its model call is issued."""

    question: str
    document_sha256: str
    processing_id: str
    snapshot_id: str
    prompt_system: str
    prompt_user: str
    member_ids: tuple[str, ...]
    fused: tuple[FusedHit, ...]
    page_windows: tuple[PageWindowStat, ...]
    fusion_mode: QueryMode
    filters_applied: MemberFilters | None = None
    filters_relaxed: bool = False
    translated_question: str | None = None


@dataclass(frozen=True, slots=True)
class AuditSummary:
    """One journal row as a listing prints it."""

    id: int
    started_at: str
    elapsed_ms: int | None
    status: str | None
    abstain_reason: str | None
    llm_live_calls: int | None
    cache_hit: bool | None
    prompt_chars: int
    request_fingerprint: str | None
    document_sha256: str
    question: str
    error: str | None


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One journal row in full, prompt and raw model output included."""

    summary: AuditSummary
    finished_at: str | None
    translated_question: str | None
    processing_id: str
    snapshot_id: str
    filters_applied: str | None
    filters_relaxed: bool
    fusion_mode: str
    page_windows: str
    member_ids: str
    fused: str
    prompt_system: str
    prompt_user: str
    model_output_raw: str | None
    answer_text: str | None
    claims_verified: str | None
    claims_rejected: str | None


class AnswerAuditStore:
    """The journal file. Opening it creates the schema; writing to it never raises."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            # WAL so a reader (the `audit` command) never blocks the service writing.
            connection.execute("PRAGMA journal_mode=WAL")
            with connection:
                connection.executescript(_SCHEMA)

    @property
    def path(self) -> Path:
        return self._path

    def begin(self, context: AnswerAuditContext) -> int | None:
        """Open a row with the prompt as it is about to be sent; ``None`` if it cannot."""
        try:
            with closing(sqlite3.connect(self._path)) as connection, connection:
                cursor = connection.execute(
                    _INSERT,
                    (
                        _now(),
                        context.question,
                        context.translated_question,
                        context.document_sha256,
                        context.processing_id,
                        context.snapshot_id,
                        _filters(context.filters_applied),
                        int(context.filters_relaxed),
                        context.fusion_mode,
                        _page_windows(context.page_windows),
                        _dumps(list(context.member_ids)),
                        _fused(context.fused),
                        context.prompt_system,
                        context.prompt_user,
                        len(context.prompt_user),
                    ),
                )
                row_id = cursor.lastrowid
            return None if row_id is None else int(row_id)
        except (sqlite3.Error, OSError) as error:
            _LOGGER.warning("answer audit: could not open a journal row (%s)", error)
            return None

    def finish(
        self,
        row_id: int,
        result: AnswerResult | None,
        *,
        model_output_raw: str | None = None,
        error: str | None = None,
    ) -> None:
        """Close the row opened by :meth:`begin`; ``result`` is ``None`` on a raised path."""
        finished = _now()
        try:
            with closing(sqlite3.connect(self._path)) as connection, connection:
                started = connection.execute(
                    "SELECT started_at FROM answers WHERE id = ?", (row_id,)
                ).fetchone()
                connection.execute(
                    _UPDATE,
                    (
                        finished,
                        None if result is None else result.request_fingerprint,
                        model_output_raw,
                        None if result is None else result.llm_live_calls,
                        None if result is None else int(result.cache_hit),
                        None if result is None else result.status.value,
                        None
                        if result is None or result.abstain_reason is None
                        else result.abstain_reason.value,
                        None if result is None else result.abstain_detail,
                        None if result is None else _verified(result.claims),
                        None if result is None else _rejected(result.rejected),
                        None
                        if result is None or result.status is not AnswerStatus.ANSWERED
                        else result.answer,
                        _elapsed_ms(None if started is None else _text(started[0]), finished),
                        error,
                        row_id,
                    ),
                )
        except (sqlite3.Error, OSError) as failure:
            _LOGGER.warning("answer audit: could not close journal row %d (%s)", row_id, failure)


def open_audit_store(path: Path) -> AnswerAuditStore | None:
    """Open the journal, or report why it stays closed; answering runs either way."""
    try:
        return AnswerAuditStore(path)
    except (sqlite3.Error, OSError) as error:
        _LOGGER.warning("answer audit: journal disabled, %s could not be opened (%s)", path, error)
        return None


def list_answers(
    path: Path,
    *,
    last: int = 20,
    fingerprint: str | None = None,
    question_like: str | None = None,
) -> tuple[AuditSummary, ...]:
    """The most recent rows matching the filters, oldest first."""
    clauses: list[str] = []
    parameters: list[object] = []
    if fingerprint:
        clauses.append("request_fingerprint LIKE ?")
        parameters.append(fingerprint + "%")
    if question_like:
        clauses.append("question LIKE ?")
        parameters.append(f"%{question_like}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT {_SUMMARY_COLUMNS} FROM answers{where} ORDER BY id DESC LIMIT ?"
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(query, (*parameters, max(last, 0))).fetchall()
    return tuple(_summary(row) for row in reversed(rows))


def read_answer(path: Path, row_id: int) -> AuditRecord | None:
    """One row in full, or ``None`` when the journal holds no such id."""
    query = f"SELECT {_SUMMARY_COLUMNS}, {_RECORD_COLUMNS} FROM answers WHERE id = ?"
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(query, (row_id,)).fetchone()
    if row is None:
        return None
    rest = row[12:]
    return AuditRecord(
        _summary(row[:12]),
        _optional_text(rest[0]),
        _optional_text(rest[1]),
        _text(rest[2]),
        _text(rest[3]),
        _optional_text(rest[4]),
        bool(_optional_int(rest[5])),
        _text(rest[6]),
        _text(rest[7]),
        _text(rest[8]),
        _text(rest[9]),
        _text(rest[10]),
        _text(rest[11]),
        _optional_text(rest[12]),
        _optional_text(rest[13]),
        _optional_text(rest[14]),
        _optional_text(rest[15]),
    )


def format_summaries(rows: Sequence[AuditSummary]) -> str:
    """A one-line-per-answer table: when, how long, what came out, and what was asked."""
    header = (
        f"{'id':>5}  {'started_at (UTC)':<20}  {'ms':>6}  {'status':<9}  {'reason':<22}  "
        f"{'live':>4}  {'cache':<5}  {'fingerprint':<12}  {'chars':>6}  question"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        reason = row.abstain_reason or row.error or "-"
        lines.append(
            f"{row.id:>5}  {row.started_at[:19]:<20}  {_number(row.elapsed_ms):>6}  "
            f"{(row.status or 'open'):<9}  {reason[:22]:<22}  {_number(row.llm_live_calls):>4}  "
            f"{_flag(row.cache_hit):<5}  {(row.request_fingerprint or '-')[:12]:<12}  "
            f"{row.prompt_chars:>6}  {_one_line(row.question, 60)}"
        )
    if not rows:
        lines.append("(no matching answers)")
    return "\n".join(lines)


def format_record(record: AuditRecord) -> str:
    """Everything the journal holds for one answer, prompt and raw output verbatim."""
    summary = record.summary
    fields: tuple[tuple[str, str], ...] = (
        ("id", str(summary.id)),
        ("started_at", summary.started_at),
        ("finished_at", record.finished_at or "-"),
        ("elapsed_ms", _number(summary.elapsed_ms)),
        ("status", summary.status or "-"),
        ("abstain_reason", summary.abstain_reason or "-"),
        ("error", summary.error or "-"),
        ("request_fingerprint", summary.request_fingerprint or "-"),
        ("llm_live_calls", _number(summary.llm_live_calls)),
        ("cache_hit", _flag(summary.cache_hit)),
        ("document_sha256", summary.document_sha256),
        ("processing_id", record.processing_id),
        ("snapshot_id", record.snapshot_id),
        ("question", record.summary.question),
        ("translated_question", record.translated_question or "-"),
        ("filters_applied", record.filters_applied or "-"),
        ("filters_relaxed", _flag(record.filters_relaxed)),
        ("fusion_mode", record.fusion_mode),
        ("member_ids", record.member_ids),
        ("page_windows", record.page_windows),
        ("fused", record.fused),
        ("claims_verified", record.claims_verified or "-"),
        ("claims_rejected", record.claims_rejected or "-"),
        ("answer_text", record.answer_text or "-"),
    )
    width = max(len(name) for name, _ in fields)
    lines = [f"{name:<{width}}  {value}" for name, value in fields]
    lines.extend(
        (
            "",
            f"--- prompt_system ({len(record.prompt_system)} chars) ---",
            record.prompt_system,
            "",
            f"--- prompt_user ({len(record.prompt_user)} chars) ---",
            record.prompt_user,
            "",
            f"--- model_output_raw ({len(record.model_output_raw or '')} chars) ---",
            record.model_output_raw or "-",
        )
    )
    return "\n".join(lines)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _elapsed_ms(started_at: str | None, finished_at: str) -> int | None:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    return round((finished - started).total_seconds() * 1000)


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _filters(filters: MemberFilters | None) -> str | None:
    if filters is None:
        return None
    return _dumps({"periods": list(filters.periods), "regions": list(filters.regions)})


def _page_windows(windows: Sequence[PageWindowStat]) -> str:
    return _dumps(
        [
            {
                "page_index": window.page_index,
                "member_count": window.member_count,
                "chars": window.chars,
                "truncated": window.truncated,
            }
            for window in windows
        ]
    )


def _fused(hits: Sequence[FusedHit]) -> str:
    return _dumps(
        [
            {
                "member_id": hit.member_id,
                "fused_score": hit.fused_score,
                "vector_rank": hit.vector_rank,
                "lexical_rank": hit.lexical_rank,
                "vector_score": hit.vector_score,
                "bm25_score": hit.bm25_score,
            }
            for hit in hits
        ]
    )


def _verified(claims: Sequence[VerifiedClaim]) -> str:
    return _dumps(
        [
            {
                "claim_id": claim.claim_id,
                "kind": claim.kind.value,
                "text": claim.text,
                "value": None if claim.value is None else str(claim.value),
                "unit": claim.unit,
                "citations": [
                    {
                        "member_id": citation.member_id,
                        "kind": citation.kind.value,
                        "page_index": citation.page_index,
                        "page_title": citation.page_title,
                        "field_path": citation.field_path,
                        "evidence_ids": list(citation.evidence_ids),
                        "quote": citation.quote,
                        "row": citation.row,
                        "col": citation.col,
                        "header": citation.header,
                    }
                    for citation in claim.citations
                ],
            }
            for claim in claims
        ]
    )


def _rejected(claims: Sequence[RejectedClaim]) -> str:
    return _dumps(
        [
            {
                "claim_id": claim.claim_id,
                "member_id": claim.member_id,
                "field_path": claim.field_path,
                "text": claim.text,
                "reason": claim.reason.value,
                "detail": claim.detail,
            }
            for claim in claims
        ]
    )


def _summary(row: Sequence[object]) -> AuditSummary:
    return AuditSummary(
        _integer(row[0]),
        _text(row[1]),
        _optional_int(row[2]),
        _optional_text(row[3]),
        _optional_text(row[4]),
        _optional_int(row[5]),
        None if row[6] is None else bool(_optional_int(row[6])),
        _integer(row[7]),
        _optional_text(row[8]),
        _text(row[9]),
        _text(row[10]),
        _optional_text(row[11]),
    )


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int:
    return value if isinstance(value, int) else 0


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _number(value: int | None) -> str:
    return "-" if value is None else str(value)


def _flag(value: bool | None) -> str:
    return "-" if value is None else ("yes" if value else "no")


def _one_line(text: str, width: int) -> str:
    folded = " ".join(text.split())
    return folded if len(folded) <= width else folded[: width - 1] + "…"
