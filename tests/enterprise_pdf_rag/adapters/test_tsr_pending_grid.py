"""A table with no ruled grid given a model-inferred grid, kept PENDING (ADR 00NN).

The structure model is a fixed stub here (the real SLANet-plus weights are not shipped): it
places the synthetic statement's cells as a model would. Every cell text must still come from
the page's own spans, a cell claim must match its text exactly, and row / column / header
relations stay closed because nothing about the grid is proved.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Never

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters import pdfspine_tsr
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import (
    DraftPublication,
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.folder_pipeline import PreflightError, run_folder_pipeline
from enterprise_pdf_rag.adapters.ingest_mode import check_unverified_table_structure, ingest_plan
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval, eligibility
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.answers.models import AbstainReason, AnswerStatus, ClaimKind
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from enterprise_pdf_rag.answers.verify import decide, verify_claims
from enterprise_pdf_rag.processing.context_builder import (
    BlockKind,
    ContextBlock,
    build_context_block,
)
from enterprise_pdf_rag.processing.retrieval import RetrievalContext
from ragspine.extraction.evidence.figures.models import Verification
from ragspine.extraction.evidence.objects.tables.table_inferred_grid import (
    TSR_PRODUCER,
    TSR_SCOPE,
    structure_producer,
)
from ragspine.extraction.evidence.objects.tables.table_models import TableIR
from ragspine.extraction.evidence.objects.tables.table_rows import TABLE_ROWS_SCOPE, TableRowsIR
from ragspine.extraction.evidence.objects.typed_ir import LiteralQualification
from ragspine.extraction.evidence.page.models import (
    ObjectKind,
    ObjectProcessingRecord,
    StageState,
)
from ragspine.extraction.tables.structure import CellBox, TableRegion, TableStructure
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import TABLE_BBOX
from tests.enterprise_pdf_rag.adapters.table_rows_helpers import (
    STATEMENT_ROWS,
    model_env,
    statement_pdf,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import authored_pdf
from tests.enterprise_pdf_rag.answers.fake_llm import answered, declined, scripted_client

_STUB_PRODUCER = f"{TSR_PRODUCER}:pdfspine/0.11.0:stub00000000"
# Where the stub model puts the statement's rows and columns (page-top-left points): the
# baselines of ``table_rows_helpers.STATEMENT`` sit inside these bands.
_ROWS = (48.0, 68.0, 84.0, 102.0, 118.0, 133.0, 149.0, 168.0, 188.0, 212.0)
_COLS = (18.0, 240.0, 312.0, 382.0)


def _box(row: int, col: int, *, rows: int = 1, cols: int = 1) -> tuple[float, float, float, float]:
    return (_COLS[col], _ROWS[row], _COLS[col + cols], _ROWS[row + rows])


def _statement_grid() -> TableStructure:
    """Row 0: a blank stub beside the title spanning both value columns; then 8 x 3 cells."""
    cells = [CellBox(0, 0, 1, 1, _box(0, 0)), CellBox(0, 1, 1, 2, _box(0, 1, cols=2))]
    cells.extend(CellBox(row, col, 1, 1, _box(row, col)) for row in range(1, 9) for col in range(3))
    return TableStructure(n_rows=9, n_cols=3, cells=tuple(cells))


@dataclass
class _StubModel:
    """A deterministic stand-in for SLANet-plus: always the same grid, coordinates only."""

    grid: TableStructure | None = field(default_factory=_statement_grid)
    producer: str = _STUB_PRODUCER
    name: str = "stub"
    regions: list[TableRegion] = field(default_factory=list)

    def recognize(self, region: TableRegion) -> TableStructure | None:
        assert region.render is not None and region.render(144).startswith(b"\x89PNG")
        self.regions.append(region)
        return self.grid


def _install(monkeypatch: pytest.MonkeyPatch, model: _StubModel) -> _StubModel:
    monkeypatch.setattr(pdfspine_tsr, "table_structure_recognizer", lambda *_: model)
    return model


def _ingest(
    pdf: Path, root: Path, *, structure: str | None, rows: bool | None = None
) -> IngestionSummary:
    return ingest_pdf(
        pdf=pdf,
        stage="semantics",
        max_live_calls=10,
        output_dir=root,
        unverified_tables_as_rows=rows,
        unverified_table_structure=structure,  # type: ignore[arg-type]
    )


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


def _no_chart(member_id: str) -> Never:
    raise AssertionError("a table claim must never read chart evidence")


def _claim(
    member_id: str,
    kind: Literal["quote", "cell"],
    path: str,
    text: str,
    *,
    row: int | None = None,
    col: int | None = None,
    header: str | None = None,
) -> ModelClaim:
    return ModelClaim(
        claim_id="c1",
        member_id=member_id,
        kind=kind,
        field_path=path,
        text=text,
        row=row,
        col=col,
        header=header,
    )


def _cell(block: ContextBlock, row: int, col: int) -> str:
    (cell,) = tuple(item for item in block.cells if (item.row, item.col) == (row, col))
    return cell.cell_id


# ---- the switch: "rows" changes nothing, "tsr" needs its model --------------------------------


def test_presets_keep_rows_and_an_unknown_policy_is_refused() -> None:
    assert ingest_plan("full").unverified_table_structure == "rows"
    assert ingest_plan("lite").unverified_table_structure == "rows"
    assert ingest_plan("full", unverified_table_structure="tsr").unverified_table_structure == "tsr"
    with pytest.raises(ValueError, match="unverified_table_structure"):
        check_unverified_table_structure("vision")


def test_rows_policy_is_byte_identical_to_leaving_the_switch_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_env(monkeypatch)
    pdf = statement_pdf(tmp_path / "statement.pdf")
    default = _ingest(pdf, tmp_path / "ingestion", structure=None, rows=True)
    explicit = _ingest(pdf, tmp_path / "ingestion", structure="rows", rows=True)
    assert explicit.processing_id == default.processing_id
    assert (default.table_tsr_grids, default.table_tsr_fallbacks) == (0, 0)


@pytest.mark.parametrize("configured", [False, True])
def test_tsr_without_its_model_refuses_to_start_and_says_what_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    model_env(monkeypatch)
    if configured:
        monkeypatch.setenv("PDFSPINE_ONNX_MODELS", str(tmp_path / "no-models"))
    else:
        monkeypatch.delenv("PDFSPINE_ONNX_MODELS", raising=False)
    pdf = statement_pdf(tmp_path / "statement.pdf")
    with pytest.raises(pdfspine_tsr.TableStructureUnavailable, match=r"slanet-plus\.onnx") as error:
        _ingest(pdf, tmp_path / "ingestion", structure="tsr")
    assert "PDFSPINE_ONNX_MODELS" in str(error.value) and "'rows'" in str(error.value)
    with pytest.raises(PreflightError, match="PDFSPINE_ONNX_MODELS"):
        run_folder_pipeline(
            pdf.parent,
            ingestion_root=tmp_path / "ingestion",
            max_live_calls_per_pdf=10,
            build_tree=False,
            embedder=OfflineDescriptionEmbedder(),
            unverified_table_structure="tsr",
        )


def test_a_fully_ruled_table_keeps_its_proved_grid_whatever_the_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_env(monkeypatch, table_bbox=TABLE_BBOX)
    model = _install(monkeypatch, _StubModel())
    pdf = authored_pdf(
        tmp_path / "ruled.pdf", page_count=1, label="Ruled", embedded_font=True, table_page=True
    )
    rows = _ingest(pdf, tmp_path / "ingestion", structure="rows")
    tsr = _ingest(pdf, tmp_path / "ingestion", structure="tsr")
    assert tsr.processing_id == rows.processing_id
    assert model.regions == [] and tsr.table_tsr_grids == 0
    context = _resolve_table(_publish(tsr), "Metric Value Revenue")
    assert isinstance(context.ir, TableIR) and context.ir.verification is Verification.VERIFIED


# ---- an unruled statement: an inferred, pending grid whose cells cite -------------------------


def test_an_unruled_statement_gets_an_inferred_grid_whose_cells_are_its_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = model_env(monkeypatch)
    model = _install(monkeypatch, _StubModel())
    summary = _ingest(
        statement_pdf(tmp_path / "statement.pdf"), tmp_path / "ingestion", structure="tsr"
    )

    record = _table_record(summary)
    stages = {stage.stage: stage for stage in record.stages}
    assert eligibility(record) == (True, None)
    for name in ("ir", "description", "qualification"):
        assert stages[name].producer.endswith(":" + _STUB_PRODUCER)
    assert "table_structure" not in stages
    assert (summary.table_tsr_grids, summary.table_tsr_fallbacks) == (1, 0)
    assert summary.table_row_transcriptions == 0
    assert len(model.regions) == 1
    calls_after_ingest = len(calls)

    published = _publish(summary)
    context = _resolve_table(published, "Revenue 2024")
    table = context.ir
    assert isinstance(table, TableIR)
    assert table.verification is Verification.PENDING and table.grid_evidence is None
    assert structure_producer(table) == _STUB_PRODUCER
    receipt = context.qualification
    assert isinstance(receipt, LiteralQualification)
    assert (receipt.scope, receipt.grid_scope, receipt.ruling_digest) == (TSR_SCOPE, None, None)
    # Resolve re-ran the same model and required the same grid.
    assert len(model.regions) >= 2

    # Every present cell is its spans' own text; the page's spans, nothing else.
    sources = LocalDocumentStore(Path(published.source_store))
    from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar

    snapshot = sources.load(published.source_manifest_id)
    spans = {span.span_id: span for span in read_text_sidecar(sources, snapshot, 1).spans}
    for cell in table.cells:
        assert " ".join(spans[span_id].text for span_id in cell.source_span_ids) == " ".join(
            (cell.text or "").split()
        )

    block = build_context_block(context)
    assert block.kind is BlockKind.TABLE and block.inferred_grid
    assert block.grid_verification is Verification.PENDING
    rendered = block.prompt_text()
    assert f"scope={TSR_SCOPE}" in rendered
    assert "table rows=9 cols=3 grid=inferred" in rendered and "not verified" in rendered
    revenue_2023 = _cell(block, 2, 2)
    assert (
        f"cells.{revenue_2023} (2,2): 1,100,200 "
        'inferred_col="Year ended 31 December / 2023" inferred_row="Revenue"'
    ) in rendered
    assert " row=2 col=2 header=" not in rendered

    blocks = {block.member_id: block}
    member = block.member_id
    answer = answered(
        "Revenue for 2023 was 1,100,200.",
        _claim(member, "cell", f"cells.{revenue_2023}", "1,100,200"),
    )
    verification = verify_claims(answer, blocks, chart_evidence=_no_chart)
    (verified,) = verification.verified
    assert verified.kind is ClaimKind.CELL
    (citation,) = verified.citations
    cell = next(item for item in table.cells if item.cell_id == revenue_2023)
    assert citation.evidence_ids == (revenue_2023, *cell.source_span_ids)
    assert citation.bbox == cell.bbox and citation.page_index == 1
    # The grid is not evidence: no row, column or header rides on the citation.
    assert (citation.row, citation.col, citation.header) == (None, None, None)
    assert decide(answer, verification, blocks_present=True, question="Revenue in 2023?") == (
        AnswerStatus.ANSWERED,
        None,
        None,
    )
    # Accounting negatives and separators stay as printed; a near miss is refused.
    negative = answered(
        "(456,789)", _claim(member, "cell", f"cells.{_cell(block, 3, 1)}", "(456,789)")
    )
    assert verify_claims(negative, blocks, chart_evidence=_no_chart).verified
    near = answered("456,789", _claim(member, "cell", f"cells.{_cell(block, 3, 1)}", "456,789"))
    (rejected,) = verify_claims(near, blocks, chart_evidence=_no_chart).rejected
    assert rejected.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    # Row, column and header claims stay closed on an inferred grid.
    for grid in (
        _claim(member, "cell", f"cells.{revenue_2023}", "1,100,200", row=2),
        _claim(member, "cell", f"cells.{revenue_2023}", "1,100,200", col=2),
        _claim(member, "cell", f"cells.{revenue_2023}", "1,100,200", header="2023"),
    ):
        relation = answered("1,100,200", grid)
        (refused,) = verify_claims(relation, blocks, chart_evidence=_no_chart).rejected
        assert refused.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
        assert refused.detail == "grid relations of this table are not verified"
    # A table is cited by cell, never quoted as a row.
    quote = answered("1,100,200", _claim(member, "quote", "fragments.row-2", "1,100,200"))
    (refused,) = verify_claims(quote, blocks, chart_evidence=_no_chart).rejected
    assert refused.reason is AbstainReason.MODEL_OUTPUT_INVALID
    # Indexing, resolving and verifying sent no LLM call.
    assert len(calls) == calls_after_ingest


def test_resolve_refuses_a_grid_the_same_model_no_longer_reproduces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_env(monkeypatch)
    model = _install(monkeypatch, _StubModel())
    published = _publish(
        _ingest(statement_pdf(tmp_path / "statement.pdf"), tmp_path / "ingestion", structure="tsr")
    )
    assert isinstance(_resolve_table(published, "Revenue").ir, TableIR)

    grid = _statement_grid()
    model.grid = TableStructure(
        grid.n_rows,
        grid.n_cols,
        tuple(
            CellBox(cell.row, 3 - cell.col, 1, 1, cell.bbox)
            if cell.row == 2 and cell.col in (1, 2)
            else cell
            for cell in grid.cells
        ),
    )
    with pytest.raises(ValueError, match="re-derive"):
        _resolve_table(published, "Revenue")
    model.grid = _statement_grid()
    model.producer = f"{TSR_PRODUCER}:pdfspine/0.11.0:otherweights0"
    with pytest.raises(ValueError, match="re-run semantics"):
        _resolve_table(published, "Revenue")
    model.producer = _STUB_PRODUCER
    assert isinstance(_resolve_table(published, "Revenue").ir, TableIR)


# ---- a grid failing its self-check falls back to ADR 0027's rows ------------------------------


@pytest.mark.parametrize(
    ("grid", "reason"),
    [
        (None, "no_structure"),
        (
            TableStructure(1, 1, (CellBox(0, 0, 1, 1, (18.0, 48.0, 382.0, 212.0)),)),
            "grid_too_small",
        ),
        (
            TableStructure(
                9,
                3,
                tuple(
                    CellBox(
                        c.row,
                        c.col,
                        1,
                        c.col_span,
                        (c.bbox[0], c.bbox[1], c.bbox[0] + 1.0, c.bbox[3]),
                    )
                    if (c.row, c.col) == (2, 1)
                    else c
                    for c in _statement_grid().cells
                ),
            ),
            "span_unassigned",
        ),
    ],
)
def test_a_failed_self_check_falls_back_to_verbatim_rows_and_says_why(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    grid: TableStructure | None,
    reason: str,
) -> None:
    model_env(monkeypatch)
    _install(monkeypatch, _StubModel(grid=grid))
    # Even with the row switch off: asking for TSR indexes the table one way or the other.
    summary = _ingest(
        statement_pdf(tmp_path / "statement.pdf"),
        tmp_path / "ingestion",
        structure="tsr",
        rows=False,
    )
    record = _table_record(summary)
    stages = {stage.stage: stage for stage in record.stages}
    structure = stages["table_structure"]
    assert structure.state is StageState.UNAVAILABLE
    assert (structure.diagnostic or "").startswith(f"tsr_fallback:{reason}:")
    assert eligibility(record) == (True, None)
    assert (summary.table_tsr_grids, summary.table_tsr_fallbacks) == (0, 1)
    assert summary.table_tsr_fallback_reasons == {reason: 1}
    assert summary.table_row_transcriptions == 1
    context = _resolve_table(_publish(summary), "Revenue 2024")
    assert isinstance(context.ir, TableRowsIR) and context.scope == TABLE_ROWS_SCOPE
    assert tuple(row.text for row in context.ir.rows) == STATEMENT_ROWS


# ---- the same question under both policies ---------------------------------------------------


def _script(prompt: str) -> ModelAnswer:
    """Answer "Revenue in 2023" from whatever the block offers: a cell, else a printed row."""
    for line in prompt.splitlines():
        if line.startswith("cells.") and 'inferred_row="Revenue"' in line and '2023"' in line:
            path, text = line.split(" ", 1)[0], line.split(": ", 1)[1].split(" inferred_")[0]
            member = next(block[:64] for block in prompt.split("| member ")[1:] if line in block)
            return answered(text, _claim(member, "cell", path, text))
        if line.startswith("fragments.row-") and line.split(": ", 1)[1].startswith("Revenue\t"):
            path, text = line.split(": ", 1)
            member = next(block[:64] for block in prompt.split("| member ")[1:] if line in block)
            # A row prints both years: which figure is 2023 is the model's reading.
            figure = text.split("\t")[2]
            return answered(figure, _claim(member, "quote", path, figure))
    return declined()


@pytest.mark.parametrize("policy", ["rows", "tsr"])
def test_run_folder_answers_the_same_figure_under_both_policies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    model_env(monkeypatch)
    _install(monkeypatch, _StubModel())
    folder = tmp_path / "pdfs"
    folder.mkdir()
    statement_pdf(folder / "acme.pdf")
    questions = tmp_path / "questions.jsonl"
    questions.write_text(
        json.dumps({"id": "q1", "question": "What was Revenue in 2023?", "doc": "acme.pdf"}) + "\n"
    )
    llm, prompts = scripted_client(tmp_path / "answer-cache", _script, max_live_calls=1)
    result = run_folder_pipeline(
        folder,
        questions=questions,
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=10,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
        answer_llm=llm,
        unverified_tables_as_rows=True,
        unverified_table_structure=policy,  # type: ignore[arg-type]
    )
    (document,) = result.documents
    assert document.status == "published" and document.ingestion is not None
    assert document.ingestion.table_tsr_grids == (1 if policy == "tsr" else 0)
    assert result.eval is not None
    (case,) = result.eval.cases
    assert case.verdict == "answered" and case.cited_pages == (2,)
    assert case.answer == "1,100,200" and len(prompts) == 1
    # The rows policy shows the figure inside a printed row; tsr shows it as a cell beside
    # its inferred row label and column header.
    assert ("grid=inferred" in prompts[0]) == (policy == "tsr")
    assert ("structure=unverified" in prompts[0]) == (policy == "rows")


def test_a_tsr_receipt_cannot_be_relabelled_as_a_plain_literal_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_env(monkeypatch)
    _install(monkeypatch, _StubModel())
    summary = _ingest(
        statement_pdf(tmp_path / "statement.pdf"), tmp_path / "ingestion", structure="tsr"
    )
    outputs = ProcessingStore(Path(summary.processing_store))
    record = _table_record(summary)
    stages = {stage.stage: stage for stage in record.stages}
    receipt = TypeAdapter(LiteralQualification).validate_json(
        outputs.assets.get(stages["qualification"].artifact)  # type: ignore[arg-type]
    )
    assert receipt.scope == TSR_SCOPE
    from dataclasses import replace

    from enterprise_pdf_rag.adapters.literal_qualification import (
        LITERAL_SCOPE,
        validate_literal_member,
    )

    published = _publish(summary)
    published_outputs = ProcessingStore(Path(published.processing_store))
    manifest = published_outputs.load(published.published_processing_id)
    assert manifest.retrieval is not None
    plan, _ = published_outputs.load_retrieval(manifest.retrieval)
    (member,) = tuple(item for item in plan.members if item.kind is ObjectKind.TABLE)
    relabelled = published_outputs.assets.put(
        TypeAdapter(LiteralQualification).dump_json(replace(receipt, scope=LITERAL_SCOPE)),
        media_type="application/json",
    )
    sources = LocalDocumentStore(Path(published.source_store), activate_on_publish=False)
    with pytest.raises(ValueError, match="qualification scope"):
        validate_literal_member(
            sources, published_outputs.assets, plan.scope, replace(member, qualification=relabelled)
        )
