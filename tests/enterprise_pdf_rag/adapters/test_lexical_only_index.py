"""Lexical-only kinds: a table / chart member is indexed without a vector, yet still scores
in BM25, is resolvable and is answered from. A chart scores its PDF text layer, verbatim.

``APP_INDEX_LEXICAL_ONLY_KINDS`` (empty by default) names the kinds; the index version names
them too, so changing the setting re-indexes instead of reusing a release built without it.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_catalog import (
    MountedDocument,
    mount_document,
    scan_catalog,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult
from enterprise_pdf_rag.adapters.hybrid_search import HybridSearch, lexical_rank
from enterprise_pdf_rag.adapters.ingest_mode import lexical_only_kinds
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.answers.ports import MemberText
from enterprise_pdf_rag.processing.index_text import IndexTextOptions
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit
from ragspine.common.evidence.configs import get_settings
from ragspine.extraction.evidence.page.models import ObjectKind
from tests.enterprise_pdf_rag.adapters.column_page_helpers import (
    CHART_LABELS,
    COLUMNS,
    ColumnPage,
    publish_column_page,
)
from tests.enterprise_pdf_rag.adapters.index_layout_helpers import (
    ANSWER,
    PAGES,
    QUESTION,
    STATEMENT_ROWS,
    report_env,
    report_pdf,
    run_report,
)
from tests.enterprise_pdf_rag.processing.test_persistent_retrieval import RecordingEmbedding

_CHARTS = IndexTextOptions(lexical_only_kinds=frozenset({ObjectKind.CHART}))


@pytest.fixture
def lexical_only_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    yield monkeypatch
    monkeypatch.delenv("APP_INDEX_LEXICAL_ONLY_KINDS", raising=False)
    get_settings.cache_clear()


def _set_kinds(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("APP_INDEX_LEXICAL_ONLY_KINDS", raising=False)
    else:
        monkeypatch.setenv("APP_INDEX_LEXICAL_ONLY_KINDS", value)
    get_settings.cache_clear()


# ---- configuration ------------------------------------------------------------------------


def test_the_setting_is_empty_by_default_and_parses_kind_names(
    lexical_only_env: pytest.MonkeyPatch,
) -> None:
    _set_kinds(lexical_only_env, None)
    assert get_settings().index_lexical_only_kinds == ""
    assert lexical_only_kinds(get_settings().index_lexical_only_kinds) == frozenset()
    assert lexical_only_kinds(" table, Chart ,") == frozenset({ObjectKind.TABLE, ObjectKind.CHART})
    assert lexical_only_kinds(["Table"]) == frozenset({ObjectKind.TABLE})
    with pytest.raises(ValueError, match="APP_INDEX_LEXICAL_ONLY_KINDS"):
        lexical_only_kinds("table,spreadsheet")
    _set_kinds(lexical_only_env, "table,chart")
    assert get_settings().index_lexical_only_kinds == "table,chart"


# ---- charts: no vector, BM25 over the text layer ------------------------------------------


def _mount(page: ColumnPage) -> MountedDocument:
    entry = scan_catalog(page.root).entry(page.document_id)
    assert entry is not None and entry.retrieval_status == "ready", entry
    return mount_document(entry, embedder=RecordingEmbedding())


def _charts(document: MountedDocument) -> list[MemberText]:
    return [item for item in document.member_texts() if item.kind is ObjectKind.CHART]


def test_lexical_only_charts_send_no_embedding_and_score_only_in_bm25(tmp_path: Path) -> None:
    default_embedder, lexical_embedder = RecordingEmbedding(), RecordingEmbedding()
    default = publish_column_page(tmp_path / "default", embedder=default_embedder)
    lexical = publish_column_page(tmp_path / "lexical", options=_CHARTS, embedder=lexical_embedder)
    # Default: the band text and the three charts are embedded. Lexical-only: the band alone.
    assert len(default_embedder.descriptions) == 4
    assert len(lexical_embedder.descriptions) == 1
    assert CHART_LABELS[1] not in lexical_embedder.descriptions[0]  # the band's own text

    on = _mount(lexical)
    charts = _charts(on)
    assert len(charts) == 3
    chart_ids = {item.member_id for item in charts}
    # The vector channel holds none of them, BM25 ranks them first for their own labels.
    assert not chart_ids & {hit.member_id for hit in on.search("New business", limit=50)}
    lexical_hits = lexical_rank(HybridSearch(on, channel_limit=50).index, "New business", limit=10)
    assert {hit.member_id for hit in lexical_hits[:3]} == chart_ids
    # Still a member: resolved and verified from its own evidence.
    for item in charts:
        hit = PinnedRetrievalHit(on.retrieval_snapshot_id, item.member_id, 0.0)
        assert on.resolve(hit).member.member_id == item.member_id
        assert on.chart_context(hit).pin.member_id == item.member_id
    # Different index layout, different snapshot.
    assert on.retrieval_snapshot_id != _mount(default).retrieval_snapshot_id


def test_a_lexical_only_chart_scores_its_text_layer_with_a_locator(tmp_path: Path) -> None:
    page = publish_column_page(tmp_path / "lexical", options=_CHARTS)
    document = _mount(page)
    sources = LocalDocumentStore(page.root / page.document_id / "source")
    source = sources.load(document.manifest().scope.source_manifest_id)
    spans = read_text_sidecar(sources, source, page.page_index).spans
    for item in _charts(document):
        # The body is the printed lines inside the chart's rectangle, nothing else.
        assert item.body == "\n".join(CHART_LABELS)
        assert "chart figure" not in item.text
        column = COLUMNS.index(item.bbox) if item.bbox in COLUMNS else None
        assert column is not None
        inside = [
            span.text
            for span in spans
            if span.bbox[0] >= COLUMNS[column][0]
            and span.bbox[2] <= COLUMNS[column][2]
            and span.bbox[1] >= COLUMNS[column][1]
            and span.bbox[3] <= COLUMNS[column][3]
        ]
        assert sorted(item.body.splitlines()) == sorted(inside)


def test_the_default_keeps_every_chart_on_both_channels_with_its_projection(
    tmp_path: Path,
) -> None:
    page = publish_column_page(tmp_path / "default")
    document = _mount(page)
    charts = _charts(document)
    assert {item.body for item in charts} == {page.chart_body}
    chart_ids = {item.member_id for item in charts}
    assert chart_ids <= {hit.member_id for hit in document.search("New business", limit=50)}


# ---- tables: the whole folder pipeline, the setting, and index reuse ----------------------


def _single_index(result: FolderPipelineResult) -> tuple[bool, object]:
    (document,) = result.documents
    assert document.status == "published", document
    assert document.index_reused or document.index is not None, document
    return document.index_reused, document.index


def _table(members: tuple[MemberText, ...]) -> MemberText:
    (table,) = (member for member in members if member.kind is ObjectKind.TABLE)
    return table


def _mounted(tmp_path: Path) -> MountedDocument:
    (entry,) = scan_catalog(tmp_path / "ingestion").documents
    return mount_document(entry, embedder=OfflineDescriptionEmbedder())


def test_a_lexical_only_table_is_answered_from_bm25_and_the_setting_changes_the_index(
    tmp_path: Path, lexical_only_env: pytest.MonkeyPatch
) -> None:
    report_env(lexical_only_env)
    report_pdf(tmp_path / "pdfs" / "report.pdf")
    _set_kinds(lexical_only_env, None)
    before, _ = run_report(tmp_path, answers="answers-default", ingest_mode="lite")
    _, default_index = _single_index(before)
    assert default_index.lexical_only_members == 0  # type: ignore[attr-defined]

    _set_kinds(lexical_only_env, "Table")
    after, prompts = run_report(tmp_path, answers="answers-lexical", ingest_mode="lite")
    reused, indexed = _single_index(after)
    # The published release was indexed without the setting: it is rebuilt, never reused.
    assert reused is False
    assert indexed.retrieval_snapshot_id != default_index.retrieval_snapshot_id  # type: ignore[attr-defined]
    # Every other index text replays its cached vector; the table sends nothing at all.
    assert (indexed.embedding_requests, indexed.embedded_objects) == (0, 0)  # type: ignore[attr-defined]
    assert indexed.lexical_only_members == 1  # type: ignore[attr-defined]
    assert indexed.row_units == len(STATEMENT_ROWS)  # type: ignore[attr-defined]

    document = _mounted(tmp_path)
    table = _table(document.member_texts())
    # BM25 still scores it as its row units; the vector channel cannot see it.
    assert table.units is not None and len(table.units) == len(STATEMENT_ROWS)
    assert table.member_id not in {hit.member_id for hit in document.search(QUESTION, limit=100)}
    outcome = HybridSearch(document, channel_limit=50).search(QUESTION, top_k=100)
    assert outcome.hits[0].member_id == table.member_id
    assert after.eval is not None
    (case,) = after.eval.cases
    assert (case.verdict, case.answer, case.cited_pages) == ("answered", ANSWER, (PAGES,))
    assert "fragments.row-" in prompts[-1]
    store = ProcessingStore(tmp_path / "ingestion" / document.document_id / "processing")
    manifest = store.load(indexed.indexed_processing_id)  # type: ignore[attr-defined]
    assert manifest.retrieval is not None
    plan, index = store.load_retrieval(manifest.retrieval)
    assert plan.index_version.endswith("lexical-only-v1=Table")
    assert table.member_id not in {entry.member_id for entry in index.entries}

    # Same setting again: that release is reused. Back to the default: the first one returns.
    again, _ = run_report(tmp_path, answers="answers-again", ingest_mode="lite")
    assert _single_index(again)[0] is True
    _set_kinds(lexical_only_env, None)
    restored, _ = run_report(tmp_path, answers="answers-restored", ingest_mode="lite")
    reused, back = _single_index(restored)
    assert reused is False
    assert back.retrieval_snapshot_id == default_index.retrieval_snapshot_id  # type: ignore[attr-defined]
    assert back.embedding_requests == 0  # type: ignore[attr-defined]


def test_the_catalog_flags_exactly_the_members_indexed_lexical_only(tmp_path: Path) -> None:
    on = _mount(publish_column_page(tmp_path / "lexical", options=_CHARTS))
    assert {item.member_id for item in on.member_texts() if item.lexical_only} == {
        item.member_id for item in _charts(on)
    }
    off = _mount(publish_column_page(tmp_path / "default"))
    assert not any(item.lexical_only for item in off.member_texts())
