"""The retrieval test bench: one row per question saying *where* an answer went wrong.

Read-only and model-free. It joins a question set to the answer journal
(``answers-audit.sqlite``, ``adapters/answer_audit``) — and, when given, the run's
``report.json`` — and reads off, per question: whether it searched every published document
(ADR 0032) and which document its ``doc`` expects, the pre-filters, where each retrieval
channel (BM25 / vector / tree) and the fused ranking placed the expected page, whether that
page reached the prompt (a seated member or its page window), how the answer came out and
whether its citations landed in the expected document, and one diagnosis in the order a RAG
failure is debugged: routing → retrieval → prompt seats → verification / generation.

Across several documents a page number names no page by itself: the expected pages are then
looked for inside the expected document only, and a question whose ``doc`` resolves to no
published document gets no page rank (``n/a``, counted) — the answer is still judged on
``expected``. ``routing_failed`` is left for a question that was never asked at all.

Linking: a journal row has no question id, so a question is matched to the **latest** row with
its exact text, narrowed to the document the run report says it was routed to when there is
one. Page ranks use ``ragspine.eval.retrieval_only`` (``gold_rank`` / ``retrieval_metrics``) and
the answer check its ``content_hit``, so the numbers mean what ``run-folder``'s report means.

Nothing is guessed: a question without ``pages`` gets no rank, one without ``expected`` no
content check, and a journal row written before the ``ranked`` column existed gets no channel
rank beyond the seated head — each is ``n/a`` and counted in the summary.

Privacy: question, expected and answer text are copied into the returned rows and the
``testbench.csv`` / ``testbench.json`` written beside ``answers.csv`` (the user's own run
artifacts), and **nowhere else** — this module logs nothing and emits no trace or event.
"""

import csv
import io
import json
import os
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Final, Literal

from enterprise_pdf_rag.adapters.document_catalog import scan_catalog
from enterprise_pdf_rag.adapters.folder_pipeline import (
    FolderPipelineResult,
    _hash_file,
    _member_pages,
    _plan_questions,
    discover_pdfs,
)
from enterprise_pdf_rag.adapters.question_docs import QuestionDocsCheck, QuestionSelectionMode
from ragspine.common.evidence.configs import ROOT_DIR
from ragspine.eval.retrieval_only import (
    BatchQuestion,
    content_hit,
    gold_rank,
    load_questions,
    recall_ks,
    retrieval_metrics,
)

NA: Final = "n/a"
INF: Final = "∞"
# The prompt's seat count in ``run-folder`` (``AnswerRequest.top_k``) and its metric cut-offs.
METRIC_TOP_K: Final = 10

Rank = int | str
Flag = bool | str
Diagnosis = Literal[
    "correct",
    "routing_failed",
    "not_retrieved",
    "retrieved_not_in_prompt",
    "in_prompt_abstained",
    "in_prompt_wrong",
    "not_in_prompt",
    "unjudged",
    "no_record",
]
# Debugging order: routing → retrieval → prompt seats → verification / generation.
DIAGNOSES: Final[dict[Diagnosis, str]] = {
    "correct": "答对: 回答包含期望答案 (没有 expected 时: 引用页命中期望页)",
    "routing_failed": "没有检索: 这道题根本没问 (没有已发布文档 / 限定题目 doc 时 doc 对不上) → 查入库 / doc 写法",
    "not_retrieved": "没召回: 没有任何通道排到期望页 → 查解析 / 分块 / 索引文本",
    "retrieved_not_in_prompt": (
        "召回了但没进 prompt: 通道排到了期望页, 融合 / 选席后没进前 k 席或页窗口 → 查融合 / 页窗口"
    ),
    "in_prompt_abstained": "进了 prompt 但弃答: 期望页在 prompt 里, 模型拒答或 claim 核验没过 → 查核验 / 生成",
    "in_prompt_wrong": "进了 prompt 但答错: 期望页在 prompt 里, 回答不含期望答案 → 查生成 / 题目口径",
    "not_in_prompt": (
        "没进 prompt: 期望答案不在 prompt 里, 但记录不足以区分没召回还是没进窗口 (旧记录 / 题目没给 pages)"
    ),
    "unjudged": "无法判定: 题目既没有 pages 也没有 expected, 只能看回答状态",
    "no_record": "无记录: 审计库里没有这道题的回答 (没问到 / 请求失败 / 换过审计库)",
}
CHANNELS: Final = ("bm25", "vector", "tree", "fused")
_CHANNEL_KEYS: Final = {"bm25": "lexical_rank", "vector": "vector_rank", "tree": "tree_rank"}


@dataclass(frozen=True, slots=True)
class BenchRow:
    """One question's way through the chain; ``n/a`` = not knowable, ``∞`` = looked, not found."""

    question_id: str
    doc: str
    # The published document the question's ``doc`` resolves to: an evaluation label only.
    expected_doc: str
    document_sha256: str
    expected_pages: str
    expected: str
    question: str
    audit_id: int | str
    link: str
    # Searched every published document of the run as one corpus (ADR 0032).
    cross_document: Flag
    routing_detail: str
    fusion_mode: str
    translated: Flag
    filters: str
    filters_relaxed: Flag
    ranking: str
    bm25_rank: Rank
    vector_rank: Rank
    tree_rank: Rank
    fused_rank: Rank
    fused_page_rank: Rank
    prompt_rank: Rank
    prompt_page_rank: Rank
    in_prompt: Flag
    page_window_hit: Flag
    status: str
    abstain_reason: str
    answer: str
    content_hit: Flag
    cited_pages: str
    cited_page_hit: Flag
    # A verified citation landed in ``expected_doc``.
    cited_doc_hit: Flag
    diagnosis: Diagnosis
    diagnosis_text: str


COLUMNS: Final = tuple(field.name for field in fields(BenchRow))


@dataclass(frozen=True, slots=True)
class RetrievalBench:
    """The bench's rows in question-set order and their summary."""

    rows: tuple[BenchRow, ...]
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Journal:
    id: int
    document_sha256: str
    filters_applied: str | None
    filters_relaxed: bool
    fusion_mode: str
    translated_question: str | None
    member_ids: list[str]
    fused: list[dict[str, Any]]
    page_windows: list[dict[str, Any]]
    ranked: list[dict[str, Any]] | None
    status: str | None
    abstain_reason: str | None
    error: str | None
    answer_text: str | None
    claims_verified: list[dict[str, Any]] | None
    prompt_user: str
    searched_documents: list[str] | None = None


@dataclass(frozen=True, slots=True)
class _Case:
    verdict: str
    document_id: str | None
    failures: tuple[str, ...]
    abstain_reason: str | None
    expected_doc: str | None = None
    routing: str | None = None


@dataclass(frozen=True, slots=True)
class _Entry:
    member_id: str
    page: int | None
    ranks: dict[str, int | None]
    document: str = ""


_COLUMNS_READ: Final = (
    "id",
    "document_sha256",
    "filters_applied",
    "filters_relaxed",
    "fusion_mode",
    "translated_question",
    "member_ids",
    "fused",
    "page_windows",
    "ranked",
    "status",
    "abstain_reason",
    "error",
    "answer_text",
    "claims_verified",
    "prompt_user",
    "searched_documents",
)


def run_retrieval_testbench(
    audit_db: Path,
    questions: Path,
    *,
    report: Path | FolderPipelineResult | None = None,
    max_questions: int | None = None,
    question_ids: Sequence[str] | None = None,
    question_selection: QuestionSelectionMode = "first",
    folder: Path | None = None,
    doc_aliases: Mapping[str, str] | None = None,
    ingestion_root: Path | None = None,
) -> RetrievalBench:
    """One ``BenchRow`` per selected question, plus the summary; no model, nothing changed.

    ``questions`` is a ``retrieval_only.load_questions`` set. The selection mirrors
    ``run_folder_pipeline``: ``question_ids`` wins, else ``max_questions`` (``"first"`` N, or
    ``"first_matched"`` given ``folder``). ``report`` (the run's ``report.json`` or its
    ``FolderPipelineResult``) says which questions went unasked, which document each answer
    named and which document each ``doc`` expects; without it, ``folder`` + ``doc_aliases``
    re-run the read-only doc resolution for the expected document.
    ``ingestion_root`` maps member ids to pages for journal rows written before the ``ranked``
    column (it scans the published catalog; no model). The returned rows carry question,
    expected and answer text: write them only to the run's own report directory, never to a
    log, trace or progress event.
    """
    selected, check = _select(
        load_questions(questions),
        max_questions=max_questions,
        question_ids=question_ids,
        selection=question_selection,
        folder=folder,
        doc_aliases=doc_aliases,
    )
    cases = _report_cases(report)
    journal = _read_journal(audit_db, {question.question for question in selected})
    legacy_pages: dict[str, int] = {}
    if ingestion_root is not None and any(item.ranked is None for item in _all(journal)):
        catalog = scan_catalog(ingestion_root.expanduser().resolve())
        legacy_pages = {member: page for member, (_, page) in _member_pages(catalog).items()}
    rows: list[BenchRow] = []
    tallies: Counter[str] = Counter()
    ranks: dict[str, list[tuple[int | None, int | None]]] = {"prompt": [], "fused": []}
    expected = _expected_docs(selected, cases, check)
    for question in selected:
        row, prompt_ranks, fused_ranks = _row(
            question,
            cases.get(question.id),
            journal,
            expected.get(question.id),
            legacy_pages,
            tallies,
        )
        rows.append(row)
        if prompt_ranks is not None:
            ranks["prompt"].append(prompt_ranks)
        if fused_ranks is not None:
            ranks["fused"].append(fused_ranks)
    return RetrievalBench(tuple(rows), _summary(rows, ranks, tallies))


def data_dir() -> Path:
    """``ROOT_DIR/data``: the only tree the bench's command line writes into."""
    return (ROOT_DIR / "data").resolve()


def default_report_dir(questions: Path) -> Path:
    """``ROOT_DIR/data/reports/<question-set stem>``: where the notebook writes answers.csv."""
    return data_dir() / "reports" / questions.stem


def write_testbench(bench: RetrievalBench, out_dir: Path) -> tuple[Path, Path]:
    """Write ``testbench.csv`` (UTF-8 with BOM, like answers.csv) and ``testbench.json``.

    Each file is written whole to a temporary name and moved into place.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "testbench.csv"
    json_path = out_dir / "testbench.json"
    _replace(csv_path, format_csv(bench).encode("utf-8-sig"))
    _replace(json_path, (format_json(bench) + "\n").encode("utf-8"))
    return csv_path, json_path


def format_csv(bench: RetrievalBench) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    for row in bench.rows:
        writer.writerow([_cell(value) for value in asdict(row).values()])
    return buffer.getvalue()


def format_json(bench: RetrievalBench) -> str:
    payload = {"rows": [asdict(row) for row in bench.rows], "summary": bench.summary}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def format_table(bench: RetrievalBench) -> str:
    """A terminal table (one line per question), the diagnosis counts and the metrics."""
    header = (
        f"{'id':<14} {'diagnosis':<24} {'bm25':>5} {'vec':>5} {'tree':>5} {'fused':>5} "
        f"{'prompt':>6} {'status':<9} {'hit':<5} {'cited':<10} doc"
    )
    lines = [header, "-" * len(header)]
    for row in bench.rows:
        lines.append(
            f"{row.question_id[:14]:<14} {row.diagnosis:<24} {_cell(row.bm25_rank):>5} "
            f"{_cell(row.vector_rank):>5} {_cell(row.tree_rank):>5} {_cell(row.fused_rank):>5} "
            f"{_cell(row.in_prompt):>6} {row.status[:9]:<9} {_cell(row.content_hit):<5} "
            f"{row.cited_pages[:10] or '-':<10} {row.doc}"
        )
    summary = bench.summary
    lines.extend(("", "诊断类别计数:"))
    for name, count in summary["diagnoses"].items():
        lines.append(f"  {name:<24} {count:>4}  {DIAGNOSES[name]}")
    lines.extend(("", "检索指标 (retrieval_only 口径):"))
    for scope, metrics in summary["metrics"].items():
        if not metrics:
            lines.append(f"  {scope:<7} n/a")
            continue
        recall = " ".join(f"r{k}={v}" for k, v in metrics["recall"].items())
        page = " ".join(f"p{k}={v}" for k, v in metrics["page_recall"].items())
        lines.append(
            f"  {scope:<7} judged={metrics['judged']} {recall} {page} mrr={metrics['mrr']}"
        )
    lines.append("通道命中率 (有 pages 且有完整排名的题):")
    for channel, stats in summary["channels"].items():
        lines.append(
            f"  {channel:<7} judged={stats['judged']} hit={stats['hit']} "
            f"hit@{METRIC_TOP_K}={stats[f'hit@{METRIC_TOP_K}']}"
        )
    if summary["n/a"]:
        lines.append("n/a 计数: " + ", ".join(f"{k}={v}" for k, v in summary["n/a"].items()))
    return "\n".join(lines)


# ---- selection, report and journal ---------------------------------------------------------


def _select(
    questions: tuple[BatchQuestion, ...],
    *,
    max_questions: int | None,
    question_ids: Sequence[str] | None,
    selection: QuestionSelectionMode,
    folder: Path | None,
    doc_aliases: Mapping[str, str] | None,
) -> tuple[tuple[BatchQuestion, ...], QuestionDocsCheck | None]:
    if max_questions is not None and max_questions < 1:
        raise ValueError("max_questions must be at least 1 (or None for every question)")
    if question_ids is not None:
        wanted = set(question_ids)
        unknown = sorted(wanted - {question.id for question in questions})
        if unknown:
            raise ValueError(f"question ids not in the question set: {', '.join(unknown)}")
        questions = tuple(question for question in questions if question.id in wanted)
        max_questions = None
    if folder is None:
        if selection == "first_matched" and max_questions is not None:
            raise ValueError("question_selection='first_matched' needs folder")
        return questions[:max_questions], None
    folder = folder.expanduser().resolve()
    limited, check = _plan_questions(
        questions,
        discover_pdfs(folder),
        folder,
        max_questions=max_questions,
        selection_mode=selection,
        doc_aliases=doc_aliases,
        digest=_hash_file,
    )
    assert isinstance(limited, tuple)
    return limited, check


def _report_cases(report: Path | FolderPipelineResult | None) -> dict[str, _Case]:
    if report is None:
        return {}
    result = (
        report
        if isinstance(report, FolderPipelineResult)
        else FolderPipelineResult.model_validate_json(report.read_text(encoding="utf-8"))
    )
    if result.eval is None:
        return {}
    return {
        case.case_id: _Case(
            case.verdict,
            case.document_id,
            case.failures,
            case.abstain_reason,
            case.expected_doc,
            case.routing,
        )
        for case in result.eval.cases
    }


def _expected_docs(
    questions: Sequence[BatchQuestion],
    cases: Mapping[str, _Case],
    check: QuestionDocsCheck | None,
) -> dict[str, str]:
    """question id → the document sha256 its ``doc`` names exactly one of; labels only."""
    expected: dict[str, str] = {}
    digests: dict[Path, str] = {}
    for question in questions:
        case = cases.get(question.id)
        if case is not None:
            if case.expected_doc:
                expected[question.id] = case.expected_doc
            continue
        if check is None or not question.doc:
            continue
        named: set[str] = set()
        for doc in question.doc:
            resolution = check.resolution(doc)
            if resolution is None or resolution.pdf is None:
                continue
            pdf = Path(check.folder) / resolution.pdf
            if pdf not in digests:
                digests[pdf] = _hash_file(pdf)
            named.add(digests[pdf])
        if len(named) == 1:
            expected[question.id] = named.pop()
    return expected


def _read_journal(path: Path, questions: set[str]) -> dict[str, list[_Journal]]:
    """Every journal row asking one of ``questions``, newest first, keyed by question text."""
    if not path.is_file():
        raise FileNotFoundError(f"no journal at {path}")
    with closing(sqlite3.connect(path)) as connection:
        present = {row[1] for row in connection.execute("PRAGMA table_info(answers)")}
        columns = ", ".join(
            name if name in present else f"NULL AS {name}" for name in _COLUMNS_READ
        )
        index = connection.execute("SELECT id, question FROM answers ORDER BY id DESC").fetchall()
        wanted = [row_id for row_id, question in index if question in questions]
        by_question: dict[str, list[_Journal]] = {}
        texts = dict(index)
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            rows = connection.execute(
                f"SELECT {columns} FROM answers WHERE id IN ({marks}) ORDER BY id DESC", chunk
            ).fetchall()
            for row in rows:
                record = _journal(row)
                by_question.setdefault(texts[record.id], []).append(record)
    for records in by_question.values():
        records.sort(key=lambda record: record.id, reverse=True)
    return by_question


def _journal(row: Sequence[object]) -> _Journal:
    values = dict(zip(_COLUMNS_READ, row, strict=True))
    ranked = _json_list(values["ranked"])
    claims = _json_list(values["claims_verified"])
    return _Journal(
        id=int(str(values["id"])),
        document_sha256=str(values["document_sha256"] or ""),
        filters_applied=_text(values["filters_applied"]),
        filters_relaxed=bool(values["filters_relaxed"]),
        fusion_mode=str(values["fusion_mode"] or ""),
        translated_question=_text(values["translated_question"]),
        member_ids=[str(item) for item in _json_list(values["member_ids"]) or []],
        fused=_json_list(values["fused"]) or [],
        page_windows=_json_list(values["page_windows"]) or [],
        ranked=ranked,
        status=_text(values["status"]),
        abstain_reason=_text(values["abstain_reason"]),
        error=_text(values["error"]),
        answer_text=_text(values["answer_text"]),
        claims_verified=claims,
        prompt_user=str(values["prompt_user"] or ""),
        searched_documents=_json_list(values["searched_documents"]),
    )


def _all(journal: Mapping[str, list[_Journal]]) -> list[_Journal]:
    return [record for records in journal.values() for record in records]


def _link(
    question: BatchQuestion, case: _Case | None, journal: Mapping[str, list[_Journal]]
) -> tuple[_Journal | None, str]:
    records = journal.get(question.question, [])
    if case is not None and case.document_id:
        routed = [record for record in records if record.document_sha256 == case.document_id]
        return (routed[0], "text+document") if routed else (None, NA)
    return (records[0], "text") if records else (None, NA)


# ---- one row --------------------------------------------------------------------------------


def _row(
    question: BatchQuestion,
    case: _Case | None,
    journal: Mapping[str, list[_Journal]],
    expected_doc: str | None,
    legacy_pages: Mapping[str, int],
    tallies: Counter[str],
) -> tuple[BenchRow, tuple[int | None, int | None] | None, tuple[int | None, int | None] | None]:
    expected = question.expected
    if not question.page_groups:
        tallies["pages"] += 1
    if not expected:
        tallies["expected"] += 1
    unrouted = _unrouted(case)
    record, link = (None, NA) if unrouted is not None else _link(question, case, journal)
    if record is None:
        if unrouted is None:
            tallies["audit_record"] += 1
        missed = (None, None) if question.page_groups else None
        if unrouted is not None:
            diagnosis: Diagnosis = "routing_failed"
        elif case is not None and case.abstain_reason == "no_relevant_member":
            diagnosis = "not_retrieved"
        else:
            diagnosis = "no_record"
        # The prompt metrics count every unanswered question as a miss, as run-folder's report
        # does; the ranking metrics only a ranking that is known to have come back empty.
        empty = missed if diagnosis == "not_retrieved" else None
        return _empty_row(question, case, unrouted, diagnosis, expected_doc), missed, empty
    cross = bool(record.searched_documents)
    # Across documents the expected pages exist only inside the expected document; without
    # one they name no page at all (ADR 0032).
    scope: tuple[str, ...] = (expected_doc,) if cross and expected_doc else ()
    groups = question.page_groups if not cross or expected_doc else ()
    if question.page_groups and not groups:
        tallies["expected_doc"] += 1
    entries, ranking = _entries(record, legacy_pages)
    if ranking != "full":
        tallies["ranked"] += 1
        if ranking == NA:
            tallies["member_pages"] += 1
    by_member = {entry.member_id: entry for entry in entries}
    prompt_hits = [
        (
            by_member[member].document if member in by_member else record.document_sha256,
            by_member[member].page if member in by_member else legacy_pages.get(member),
        )
        for member in record.member_ids
    ]
    prompt_known = all(page is not None for _, page in prompt_hits)
    window_pages = {
        int(window["page_index"]) + 1
        for window in record.page_windows
        if not scope or window.get("document_sha256", record.document_sha256) in scope
    }
    complete = ranking == "full"

    channel: dict[str, Rank] = {
        name: _channel_rank(entries, key, groups, complete, scope)
        for name, key in _CHANNEL_KEYS.items()
    }
    paged = [(entry.document, entry.page) for entry in entries if entry.page is not None]
    fused = _ranks(paged, groups, complete, scope)
    known = [(document, page) for document, page in prompt_hits if page is not None]
    prompt = _ranks(known, groups, prompt_known, scope)

    in_prompt: Flag
    window_hit: Flag = NA
    if groups:
        seen = {page for document, page in known if not scope or document in scope}
        in_prompt = (
            True if _covers(seen | window_pages, groups) else (False if prompt_known else NA)
        )
        if window_pages:
            window_hit = _covers(window_pages, groups)
    elif expected:
        in_prompt = content_hit(_without_question(record.prompt_user, question.question), expected)
    else:
        in_prompt = NA

    status = record.status or ("error" if record.error else "open")
    answer = record.answer_text or ""
    answered = record.status == "answered"
    hit: Flag = NA if not expected else (answered and content_hit(answer, expected))
    cited = _cited(record.claims_verified, record.document_sha256)
    cited_hit: Flag = (
        NA
        if not groups or cited is None
        else _covers({page for document, page in cited if not scope or document in scope}, groups)
    )
    cited_doc_hit: Flag = (
        NA
        if not expected_doc or not cited
        else any(document == expected_doc for document, _ in cited)
    )
    correct: Flag = NA
    if answered:
        correct = hit if expected else cited_hit
    diagnosis = _diagnose(
        groups=bool(groups),
        expected=bool(expected),
        answered=answered,
        correct=correct,
        in_prompt=in_prompt,
        fused_rank=fused[0],
        complete=complete,
    )
    row = BenchRow(
        question_id=question.id,
        doc=_doc(question),
        expected_doc=expected_doc or NA,
        document_sha256=record.document_sha256 or NA,
        expected_pages=_pages(question.page_groups),
        expected=expected or NA,
        question=question.question,
        audit_id=record.id,
        link=link,
        cross_document=cross,
        routing_detail="",
        fusion_mode=record.fusion_mode or NA,
        translated=record.translated_question is not None,
        filters=_filters(record.filters_applied),
        filters_relaxed=record.filters_relaxed,
        ranking=ranking,
        bm25_rank=channel["bm25"],
        vector_rank=channel["vector"],
        tree_rank=channel["tree"],
        fused_rank=fused[0],
        fused_page_rank=fused[1],
        prompt_rank=prompt[0],
        prompt_page_rank=prompt[1],
        in_prompt=in_prompt,
        page_window_hit=window_hit,
        status=status,
        abstain_reason=record.abstain_reason or record.error or "",
        answer=answer,
        content_hit=hit,
        cited_pages=NA if cited is None else ",".join(_unique_pages(cited)),
        cited_page_hit=cited_hit,
        cited_doc_hit=cited_doc_hit,
        diagnosis=diagnosis,
        diagnosis_text=DIAGNOSES[diagnosis],
    )
    prompt_ranks = (_number(prompt[0]), _number(prompt[1])) if groups and prompt_known else None
    fused_ranks = (_number(fused[0]), _number(fused[1])) if groups and complete else None
    return row, prompt_ranks, fused_ranks


def _diagnose(
    *,
    groups: bool,
    expected: bool,
    answered: bool,
    correct: Flag,
    in_prompt: Flag,
    fused_rank: Rank,
    complete: bool,
) -> Diagnosis:
    if correct is True:
        return "correct"
    if not groups and not expected:
        return "unjudged"
    if in_prompt is True:
        if not answered:
            return "in_prompt_abstained"
        return "in_prompt_wrong" if correct is False else "unjudged"
    if isinstance(fused_rank, int):
        return "retrieved_not_in_prompt"
    if groups and complete:
        return "not_retrieved"
    return "not_in_prompt"


def _unrouted(case: _Case | None) -> str | None:
    """Why this question was never asked (the run report says so), or ``None``."""
    if case is None or case.verdict != "routing_failed":
        return None
    return case.failures[0] if case.failures else "routing_failed"


def _empty_row(
    question: BatchQuestion,
    case: _Case | None,
    unrouted: str | None,
    diagnosis: Diagnosis,
    expected_doc: str | None,
) -> BenchRow:
    """A question with no journal row: never asked, retrieved nothing, or no record."""
    missed: Rank = INF if question.page_groups else NA
    asked = diagnosis == "not_retrieved"
    return BenchRow(
        question_id=question.id,
        doc=_doc(question),
        expected_doc=expected_doc or NA,
        document_sha256=NA,
        expected_pages=_pages(question.page_groups),
        expected=question.expected or NA,
        question=question.question,
        audit_id=NA,
        link=NA,
        cross_document=NA
        if case is None or unrouted is not None
        else case.routing == "cross_document",
        routing_detail=unrouted or "",
        fusion_mode=NA,
        translated=NA,
        filters=NA,
        filters_relaxed=NA,
        ranking=NA,
        bm25_rank=NA,
        vector_rank=NA,
        tree_rank=NA,
        fused_rank=missed if asked else NA,
        fused_page_rank=missed if asked else NA,
        prompt_rank=missed if diagnosis != "no_record" else NA,
        prompt_page_rank=missed if diagnosis != "no_record" else NA,
        in_prompt=NA if diagnosis == "no_record" else False,
        page_window_hit=NA,
        status="abstained" if asked else NA,
        abstain_reason=(case.abstain_reason or "") if asked and case is not None else "",
        answer="",
        content_hit=NA if not question.expected or diagnosis == "no_record" else False,
        cited_pages=NA,
        cited_page_hit=NA,
        cited_doc_hit=NA,
        diagnosis=diagnosis,
        diagnosis_text=DIAGNOSES[diagnosis],
    )


def _entries(record: _Journal, legacy_pages: Mapping[str, int]) -> tuple[list[_Entry], str]:
    """The ranking to read seats from, with 1-based pages, and how much of it the row holds."""
    if record.ranked is not None:
        source, scope = record.ranked, "full"
    else:
        source, scope = record.fused, "head"
    entries: list[_Entry] = []
    for item in source:
        member = str(item.get("member_id"))
        index = item.get("page_index")
        page = int(index) + 1 if isinstance(index, int) else legacy_pages.get(member)
        ranks = {key: _optional_int(item.get(key)) for key in _CHANNEL_KEYS.values()}
        document = item.get("document_sha256")
        entries.append(
            _Entry(
                member,
                page,
                ranks,
                document if isinstance(document, str) else record.document_sha256,
            )
        )
    if scope == "head" and not any(entry.page is not None for entry in entries):
        scope = NA
    return entries, scope


def _channel_rank(
    entries: Sequence[_Entry],
    key: str,
    groups: Sequence[frozenset[int]],
    complete: bool,
    docs: Sequence[str] = (),
) -> Rank:
    """The channel's own seat for the expected pages: per page group the best seat, worst group.

    ``docs`` keeps the expected document's pages when the ranking spans several documents.
    """
    if not groups or not any(entry.ranks[key] is not None for entry in entries):
        return NA
    worst = 0
    for group in groups:
        seats = [
            seat
            for entry in entries
            if entry.page in group
            and (not docs or entry.document in docs)
            and (seat := entry.ranks[key]) is not None
        ]
        if not seats:
            return INF if complete else NA
        worst = max(worst, min(seats))
    return worst


def _ranks(
    hits: Sequence[tuple[str, int]],
    groups: Sequence[frozenset[int]],
    complete: bool,
    docs: Sequence[str] = (),
) -> tuple[Rank, Rank]:
    if not groups:
        return NA, NA
    miss = INF if complete else NA
    rank = gold_rank(hits, groups, docs=docs)
    page_rank = gold_rank(hits, groups, docs=docs, distinct=True)
    return (miss if rank is None else rank), (miss if page_rank is None else page_rank)


def _covers(pages: set[int], groups: Sequence[frozenset[int]]) -> bool:
    return all(pages & group for group in groups)


def _cited(claims: list[dict[str, Any]] | None, document: str) -> list[tuple[str, int]] | None:
    """(document, 1-based page) of every verified citation, in first-citation order."""
    if claims is None:
        return None
    cited: list[tuple[str, int]] = []
    for claim in claims:
        for citation in claim.get("citations", ()):
            page = citation.get("page_index")
            owner = citation.get("document_sha256")
            key = (
                owner if isinstance(owner, str) else document,
                page + 1 if isinstance(page, int) else 0,
            )
            if isinstance(page, int) and key not in cited:
                cited.append(key)
    return cited


def _unique_pages(cited: Sequence[tuple[str, int]]) -> list[str]:
    pages: list[str] = []
    for _, page in cited:
        if str(page) not in pages:
            pages.append(str(page))
    return pages


def _without_question(prompt: str, question: str) -> str:
    return prompt.replace(question, "", 1)


# ---- summary --------------------------------------------------------------------------------


def _summary(
    rows: Sequence[BenchRow],
    ranks: Mapping[str, list[tuple[int | None, int | None]]],
    tallies: Counter[str],
) -> dict[str, Any]:
    ks = recall_ks(METRIC_TOP_K)
    diagnoses = Counter(row.diagnosis for row in rows)
    by_doc: dict[str, dict[str, int]] = {}
    for row in rows:
        counts = by_doc.setdefault(row.doc or "(none)", {})
        counts[row.diagnosis] = counts.get(row.diagnosis, 0) + 1
    channels: dict[str, dict[str, Any]] = {}
    complete = [row for row in rows if row.ranking == "full" and row.expected_pages != NA]
    for channel in CHANNELS:
        seats = [
            getattr(row, f"{channel}_rank")
            for row in complete
            if getattr(row, f"{channel}_rank") != NA
        ]
        found = [seat for seat in seats if isinstance(seat, int)]
        channels[channel] = {
            "judged": len(seats),
            "hit": _rate(len(found), len(seats)),
            f"hit@{METRIC_TOP_K}": _rate(sum(seat <= METRIC_TOP_K for seat in found), len(seats)),
        }
    return {
        "questions": len(rows),
        "diagnoses": {name: diagnoses[name] for name in DIAGNOSES},
        "metrics": {
            scope: retrieval_metrics(values, ks) if values else {}
            for scope, values in ranks.items()
        },
        "channels": channels,
        "by_doc": by_doc,
        "n/a": dict(sorted(tallies.items())),
    }


# ---- small helpers --------------------------------------------------------------------------


def _doc(question: BatchQuestion) -> str:
    return ";".join(question.doc)


def _pages(groups: Sequence[frozenset[int]]) -> str:
    if not groups:
        return NA
    return " | ".join(",".join(str(page) for page in sorted(group)) for group in groups)


def _filters(raw: str | None) -> str:
    if raw is None:
        return "-"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(parsed, dict):
        return raw
    parts = [
        f"{name}={','.join(str(value) for value in parsed.get(name) or ())}"
        for name in ("periods", "regions")
        if parsed.get(name)
    ]
    return "; ".join(parts) or "-"


def _number(value: Rank) -> int | None:
    return value if isinstance(value, int) else None


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _cell(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _json_list(value: object) -> list[Any] | None:
    """A journal JSON array column; ``None`` when the column is NULL / absent / not an array."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _replace(path: Path, payload: bytes) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(payload)
    os.replace(partial, path)
