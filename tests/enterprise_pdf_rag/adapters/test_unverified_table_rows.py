"""A table whose grid no ruling proves is indexed as its verbatim printed rows (ADR 0027).

Off by default: the region stays out of the index exactly as before. On, its rows are a
retrievable, citable member whose every character is the page's own text.
"""

import json
from pathlib import Path
from typing import Never

import pdfspine
import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import (
    DraftPublication,
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.folder_pipeline import run_folder_pipeline
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval, eligibility
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.answers.models import AbstainReason, AnswerStatus, ClaimKind
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from enterprise_pdf_rag.answers.verify import decide, verify_claims
from enterprise_pdf_rag.processing.context_builder import BlockKind, build_context_block
from enterprise_pdf_rag.processing.retrieval import RetrievalContext
from ragspine.extraction.evidence.figures.models import Verification
from ragspine.extraction.evidence.objects.tables.table_models import TableIR
from ragspine.extraction.evidence.objects.tables.table_rows import (
    TABLE_ROWS_PRODUCER,
    TABLE_ROWS_SCOPE,
    TableRowsIR,
)
from ragspine.extraction.evidence.objects.typed_ir import LiteralQualification
from ragspine.extraction.evidence.page.models import (
    ObjectKind,
    ObjectProcessingRecord,
    StageState,
)
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import TABLE_BBOX
from tests.enterprise_pdf_rag.adapters.table_rows_helpers import (
    PARAGRAPH,
    STATEMENT_ROWS,
    ingest,
    model_env,
    statement_pdf,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import authored_pdf
from tests.enterprise_pdf_rag.adapters.test_pdf_password import (
    OWNER_PASSWORD,
    PASSWORD,
    set_password,
)
from tests.enterprise_pdf_rag.answers.fake_llm import answered, declined, scripted_client

_EXCLUDED = "Table transcription is not verified; only verified tables are retrievable"


def _no_chart(member_id: str) -> Never:
    raise AssertionError("a row claim must never read chart evidence")


def _table_record(summary: IngestionSummary) -> ObjectProcessingRecord:
    manifest = ProcessingStore(Path(summary.processing_store)).load(summary.processing_id)
    (record,) = tuple(
        item for page in manifest.pages for item in page.objects if item.kind is ObjectKind.TABLE
    )
    return record


def _publish(summary: IngestionSummary) -> DraftPublication:
    source_store, processing_store = Path(summary.source_store), Path(summary.processing_store)
    qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=summary.processing_id,
    )
    indexed = index_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=summary.processing_id,
        embedder=OfflineDescriptionEmbedder(),
    )
    return publish_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=indexed.indexed_processing_id,
    )


def _resolve_table(published: DraftPublication, query: str) -> RetrievalContext:
    sources = LocalDocumentStore(Path(published.source_store), activate_on_publish=False)
    outputs = ProcessingStore(Path(published.processing_store))
    manifest = outputs.load(published.published_processing_id)
    assert manifest.retrieval is not None
    retrieval = ProcessingRetrieval(sources, outputs, OfflineDescriptionEmbedder())
    plan, _ = outputs.load_retrieval(manifest.retrieval)
    (member,) = tuple(item for item in plan.members if item.kind is ObjectKind.TABLE)
    hits = retrieval.search(manifest.retrieval, query, limit=5)
    (hit,) = tuple(hit for hit in hits if hit.member_id == member.member_id)
    return retrieval.resolve(manifest.retrieval, hit)


def _row_claim(member_id: str, row: str, text: str, answer: str) -> ModelAnswer:
    return answered(
        answer,
        ModelClaim(
            claim_id="c1",
            member_id=member_id,
            kind="quote",
            field_path=f"fragments.{row}",
            text=text,
        ),
    )


# ---- the defect, pinned: off (the default) an unruled statement never reaches the index -------


def test_off_by_default_an_unruled_statement_stays_out_of_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_env(monkeypatch)
    pdf = statement_pdf(tmp_path / "statement.pdf")
    summary = ingest(pdf, tmp_path / "ingestion", rows=None)

    record = _table_record(summary)
    stages = {stage.stage: stage for stage in record.stages}
    detection = TypeAdapter(object).validate_json(
        ProcessingStore(Path(summary.processing_store)).assets.get(
            stages["table_detection"].artifact  # type: ignore[arg-type]
        )
    )
    assert isinstance(detection, dict) and detection["table"] is None
    # Exactly today's three closing stages, not one byte more.
    assert [(stage.stage, stage.state) for stage in record.stages[-3:]] == [
        ("ir", StageState.UNAVAILABLE),
        ("description", StageState.UNAVAILABLE),
        ("qualification", StageState.UNAVAILABLE),
    ]
    assert eligibility(record) == (False, _EXCLUDED)
    qualified = qualify_draft(
        source_store=Path(summary.source_store),
        processing_store=Path(summary.processing_store),
        processing_id=summary.processing_id,
    )
    assert "Table" not in qualified.kinds and qualified.skipped_reasons == {_EXCLUDED: 1}
    assert (summary.table_row_transcriptions, summary.table_row_lines) == (0, 0)
    # The explicit False is the default, byte for byte.
    assert ingest(pdf, tmp_path / "ingestion", rows=False).processing_id == summary.processing_id


# ---- on: rows are retrievable, citable and verbatim ------------------------------------------


def test_on_an_unruled_statement_is_indexed_as_verbatim_rows_and_answers_a_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = model_env(monkeypatch)
    summary = ingest(statement_pdf(tmp_path / "statement.pdf"), tmp_path / "ingestion", rows=True)

    record = _table_record(summary)
    stages = {stage.stage: stage for stage in record.stages}
    assert eligibility(record) == (True, None)
    assert TABLE_ROWS_PRODUCER in stages["description"].producer
    assert (summary.table_row_transcriptions, summary.table_row_lines) == (1, len(STATEMENT_ROWS))
    calls_after_ingest = len(calls)

    published = _publish(summary)
    context = _resolve_table(published, "Revenue 2024")
    rows = context.ir
    assert isinstance(rows, TableRowsIR)
    assert context.scope == TABLE_ROWS_SCOPE
    assert isinstance(context.qualification, LiteralQualification)
    assert context.qualification.grid_scope is None
    assert context.description.text == "\n".join(STATEMENT_ROWS)
    assert context.description.producer == TABLE_ROWS_PRODUCER
    assert tuple(row.text for row in rows.rows) == STATEMENT_ROWS

    block = build_context_block(context)
    assert block.kind is BlockKind.TABLE and block.grid_verification is Verification.PENDING
    assert block.cells == ()
    rendered = block.prompt_text()
    assert f"scope={TABLE_ROWS_SCOPE}" in rendered
    assert "structure=unverified" in rendered
    assert "fragments.row-2: Revenue\t1,234,567\t1,100,200" in rendered
    assert "cells." not in rendered

    # A cell value cited through its row: verified, located to the row's own spans.
    blocks = {block.member_id: block}
    answer = _row_claim(
        block.member_id, "row-2", "Revenue 1,234,567", "Revenue for 2024 was 1,234,567."
    )
    verification = verify_claims(answer, blocks, chart_evidence=_no_chart)
    (verified,) = verification.verified
    assert verified.kind is ClaimKind.QUOTE
    (citation,) = verified.citations
    assert citation.page_index == 1 and citation.field_path == "fragments.row-2"
    assert citation.quote == "Revenue\t1,234,567\t1,100,200"
    row = rows.rows[2]
    assert citation.evidence_ids == ("row-2", *row.source_span_ids)
    assert citation.bbox == row.bbox
    assert decide(answer, verification, blocks_present=True, question="Revenue in 2024?") == (
        AnswerStatus.ANSWERED,
        None,
        None,
    )
    # The cited spans are the page's own text, character for character.
    sources = LocalDocumentStore(Path(published.source_store))
    from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar

    snapshot = sources.load(published.source_manifest_id)
    spans = {span.span_id: span for span in read_text_sidecar(sources, snapshot, 1).spans}
    assert tuple(spans[span_id].text for span_id in row.source_span_ids) == row.texts

    # Accounting negatives, separators and the total stay as printed.
    negative = _row_claim(block.member_id, "row-3", "(456,789)", "Cost of sales was (456,789).")
    assert verify_claims(negative, blocks, chart_evidence=_no_chart).verified
    total = _row_claim(block.member_id, "row-7", "Total\t790,123", "The total was 790,123.")
    assert verify_claims(total, blocks, chart_evidence=_no_chart).verified
    # A number the row does not print is refused, and so is prose outside the claims.
    invented = _row_claim(block.member_id, "row-2", "1,234,568", "Revenue was 1,234,568.")
    (rejected,) = verify_claims(invented, blocks, chart_evidence=_no_chart).rejected
    assert rejected.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    drifting = _row_claim(block.member_id, "row-2", "1,234,567", "Revenue rose 12% to 1,234,567.")
    status, reason, _ = decide(
        drifting,
        verify_claims(drifting, blocks, chart_evidence=_no_chart),
        blocks_present=True,
    )
    assert (status, reason) == (AnswerStatus.ABSTAINED, AbstainReason.CLAIM_NOT_IN_EVIDENCE)
    # A cell claim names no row member: rows are not cells.
    cell = ModelAnswer(
        abstain=False,
        abstain_reason=None,
        answer="1,234,567",
        claims=(
            ModelClaim(
                claim_id="c1",
                member_id=block.member_id,
                kind="cell",
                field_path="cells.row-2",
                text="1,234,567",
            ),
        ),
    )
    assert not verify_claims(cell, blocks, chart_evidence=_no_chart).verified
    # Rows cost no model call: indexing, resolving and verifying read stored evidence only.
    assert len(calls) == calls_after_ingest


def test_on_a_frame_only_table_is_rows_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_env(monkeypatch)
    pdf = statement_pdf(tmp_path / "framed.pdf", frame=True)
    off = ingest(pdf, tmp_path / "ingestion", rows=False)
    assert eligibility(_table_record(off)) == (False, _EXCLUDED)

    on = ingest(pdf, tmp_path / "ingestion", rows=True)
    context = _resolve_table(_publish(on), "Revenue 2024")
    assert isinstance(context.ir, TableRowsIR)
    assert tuple(row.text for row in context.ir.rows) == STATEMENT_ROWS


def test_a_mislabelled_paragraph_becomes_its_printed_lines_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_env(monkeypatch)
    pdf = statement_pdf(tmp_path / "prose.pdf", lines=PARAGRAPH)
    context = _resolve_table(_publish(ingest(pdf, tmp_path / "ingestion", rows=True)), "growth")
    assert isinstance(context.ir, TableRowsIR)
    assert tuple(row.text for row in context.ir.rows) == tuple(text for *_, text, _ in PARAGRAPH)


def test_a_fully_ruled_table_takes_the_verified_grid_path_whatever_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_env(monkeypatch, table_bbox=TABLE_BBOX)
    pdf = authored_pdf(
        tmp_path / "ruled.pdf", page_count=1, label="Ruled", embedded_font=True, table_page=True
    )
    off = ingest(pdf, tmp_path / "ingestion", rows=False)
    on = ingest(pdf, tmp_path / "ingestion", rows=True)
    # Same manifest, same id: no fingerprint, cache key or stage byte moved.
    assert on.processing_id == off.processing_id
    assert on.table_row_transcriptions == 0
    context = _resolve_table(_publish(on), "Metric Value Revenue")
    assert isinstance(context.ir, TableIR) and context.ir.verification is Verification.VERIFIED


# ---- switching on and off over an already published document -----------------------------


def test_switching_on_republishes_with_zero_model_calls_and_switching_off_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = model_env(monkeypatch)
    pdf = statement_pdf(tmp_path / "statement.pdf")
    root = tmp_path / "ingestion"
    off = ingest(pdf, root, rows=False)
    first = _publish(off)
    assert first.member_count == 2  # the two title lines; the statement was excluded
    paid = len(calls)
    current = Path(off.processing_store) / "current-processing"
    assert current.read_text().strip() == first.published_processing_id

    on = ingest(pdf, root, rows=True)
    assert on.live_call_count == 0 and len(calls) == paid  # layout and metadata replay
    assert on.processing_id != off.processing_id
    second = _publish(on)
    assert second.member_count == 3
    assert current.read_text().strip() == second.published_processing_id

    again = ingest(pdf, root, rows=False)
    assert again.live_call_count == 0 and len(calls) == paid
    assert again.processing_id == off.processing_id
    third = _publish(again)
    assert third.published_processing_id == first.published_processing_id
    assert current.read_text().strip() == first.published_processing_id


def test_an_encrypted_statement_is_read_as_rows_once_it_authenticates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_env(monkeypatch)
    plain = statement_pdf(tmp_path / "plain.pdf")
    with pdfspine.open(stream=plain.read_bytes(), filetype="pdf") as document:
        data = document.tobytes(
            encryption=pdfspine.PDF_ENCRYPT_AES_256, user_pw=PASSWORD, owner_pw=OWNER_PASSWORD
        )
    locked = tmp_path / "locked.pdf"
    locked.write_bytes(data)
    set_password(monkeypatch, PASSWORD)
    context = _resolve_table(_publish(ingest(locked, tmp_path / "ingestion", rows=True)), "Revenue")
    assert isinstance(context.ir, TableRowsIR)
    assert tuple(row.text for row in context.ir.rows) == STATEMENT_ROWS


# ---- run-folder threads the switch and the answer chain cites a row ------------------------


def test_run_folder_answers_a_statement_figure_from_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_env(monkeypatch)
    folder = tmp_path / "pdfs"
    folder.mkdir()
    statement_pdf(folder / "acme.pdf")
    questions = tmp_path / "questions.jsonl"
    questions.write_text(
        json.dumps({"id": "q1", "question": "What was Revenue in 2024?", "doc": "acme.pdf"}) + "\n"
    )

    def script(prompt: str) -> ModelAnswer:
        for line in prompt.splitlines():
            if line.startswith("fragments.row-"):
                path, text = line.split(": ", 1)
                if text.startswith("Revenue\t"):
                    member = next(
                        block[:64] for block in prompt.split("| member ")[1:] if line in block
                    )
                    return _row_claim(member, path.removeprefix("fragments."), text, "1,234,567")
        return declined()

    llm, prompts = scripted_client(tmp_path / "answer-cache", script, max_live_calls=1)
    result = run_folder_pipeline(
        folder,
        questions=questions,
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=10,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
        answer_llm=llm,
        unverified_tables_as_rows=True,
    )
    (document,) = result.documents
    assert document.status == "published"
    assert document.ingestion is not None and document.ingestion.table_row_transcriptions == 1
    assert result.eval is not None
    (case,) = result.eval.cases
    assert case.verdict == "answered" and case.cited_pages == (2,)
    assert case.answer == "1,234,567" and len(prompts) == 1
