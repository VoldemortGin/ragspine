"""High-level :class:`RAGSpine` facade contracts.

These tests deliberately exercise only the public facade.  The low-level ingestion,
storage, and agent tests remain responsible for their individual domain rules.
"""

from pathlib import Path

import pytest
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches


def _make_financial_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "ACME Hong Kong"
    sheet["A1"] = "ACME Hong Kong"
    sheet["B1"] = "FY2024"
    sheet["A2"] = "REVENUE"
    sheet["B2"] = 2680
    workbook.save(path)


def _make_dual_channel_deck(path: Path) -> None:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    table = slide.shapes.add_table(2, 2, Inches(0.5), Inches(0.5), Inches(8), Inches(2)).table
    table.cell(0, 0).text = "ACME Hong Kong"
    table.cell(0, 1).text = "FY2024"
    table.cell(1, 0).text = "REVENUE"
    table.cell(1, 1).text = "2680"
    textbox = slide.shapes.add_textbox(Inches(0.5), Inches(3), Inches(8), Inches(1))
    textbox.text_frame.text = "Revenue improved after the agency channel expansion."
    presentation.save(path)


def test_local_context_owns_and_closes_workspace_resources(tmp_path):
    """A local workspace is usable in its context and rejects work after closing."""
    from ragspine import RAGSpine

    workspace = tmp_path / "knowledge-base"
    with RAGSpine.local(workspace) as rag:
        assert workspace.is_dir()
        assert (workspace / "knowledge.db").is_file()
        assert (workspace / "mapping.db").is_file()
        assert (workspace / "review.db").is_file()

    with pytest.raises(RuntimeError, match="closed|关闭"):
        rag.ask("香港 FY2024 REVENUE 是多少")


def test_local_defaults_to_lean_economy_profile(tmp_path):
    """The zero-configuration facade never opts into model-backed retrieval."""
    from ragspine import RAGSpine
    from ragspine.facade import RetrievalProfile, make_retrieval_preset

    with RAGSpine.local(tmp_path / "knowledge-base") as rag:
        assert rag.retrieval == make_retrieval_preset(RetrievalProfile.ECONOMY)


def test_balanced_profile_drives_offline_narrative_ask(tmp_path):
    """Balanced retrieval is assembled by ask and remains deterministic offline."""
    from ragspine import RAGSpine
    from ragspine.facade import RetrievalProfile

    source = tmp_path / "review.txt"
    source.write_text("营收增长的原因是代理渠道扩张。", encoding="utf-8")

    with RAGSpine.local(tmp_path / "knowledge-base", profile=RetrievalProfile.BALANCED) as rag:
        rag.ingest(source)
        result = rag.ask("营收为什么增长？")

    assert result.sources
    assert result.sources[0]["doc"] == "review.txt"


def test_explicit_retrieval_preset_overrides_named_profile(tmp_path):
    """Advanced callers can replace a profile without untyped configuration maps."""
    from ragspine import RAGSpine
    from ragspine.facade import RetrievalProfile, make_retrieval_preset

    custom = make_retrieval_preset(
        RetrievalProfile.BALANCED,
        embedding="none",
        vector_store="none",
    )
    with RAGSpine.local(
        tmp_path / "knowledge-base",
        profile=RetrievalProfile.QUALITY,
        retrieval=custom,
    ) as rag:
        assert rag.retrieval is custom


def test_ingest_then_ask_preserves_answer_provenance_across_reopen(tmp_path):
    """User-owned structured data remains answerable after reopening a workspace."""
    from ragspine import RAGSpine

    workspace = tmp_path / "knowledge-base"
    source = tmp_path / "acme_hk_fy2024.xlsx"
    _make_financial_workbook(source)

    with RAGSpine.local(workspace) as rag:
        ingest_result = rag.ingest(source)
        assert not ingest_result.failed

    with RAGSpine.local(workspace) as rag:
        answer = rag.ask("香港 FY2024 REVENUE 是多少")

    assert "2680" in answer.answer
    assert answer.sources == [
        {
            "doc": "acme_hk_fy2024.xlsx",
            "locator": "sheet=ACME Hong Kong!B2",
        }
    ]


def test_xlsx_ingest_returns_one_structured_aggregate_result(tmp_path):
    """XLSX defaults to the structured channel behind one stable result type."""
    from ragspine import RAGSpine
    from ragspine.facade import IngestResult

    source = tmp_path / "financials.xlsx"
    _make_financial_workbook(source)

    with RAGSpine.local(tmp_path / "knowledge-base") as rag:
        result = rag.ingest(source)

    assert isinstance(result, IngestResult)
    assert len(result.structured_reports) == 1
    assert result.structured_reports[0].status == "ok"
    assert result.narrative_report is None
    assert not result.failed
    assert isinstance(result.summary, str) and result.summary


def test_pptx_ingest_attempts_both_channels_without_cross_channel_failure(tmp_path):
    """PPTX keeps structured facts and narrative chunks through one dispatch."""
    from ragspine import RAGSpine
    from ragspine.facade import IngestResult

    source = tmp_path / "board_pack_FY2024.pptx"
    _make_dual_channel_deck(source)

    with RAGSpine.local(tmp_path / "knowledge-base") as rag:
        result = rag.ingest(source)

    assert isinstance(result, IngestResult)
    assert len(result.structured_reports) == 1
    assert result.structured_reports[0].status == "ok"
    assert result.structured_reports[0].n_facts_ingested >= 1
    assert result.narrative_report is not None
    assert result.narrative_report.files[0].n_chunks >= 1
    assert not result.failed


def test_ingest_routes_text_to_narrative_channel(tmp_path):
    """Plain text takes the narrative path without requiring structured extraction."""
    from ragspine import RAGSpine

    source = tmp_path / "notes.txt"
    source.write_text("Revenue narrative", encoding="utf-8")

    with RAGSpine.local(tmp_path / "knowledge-base") as rag:
        result = rag.ingest(source)

    assert result.structured_reports == ()
    assert result.narrative_report is not None
    assert result.narrative_report.files[0].n_chunks == 1
    assert not result.failed
