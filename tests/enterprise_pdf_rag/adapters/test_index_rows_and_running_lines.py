"""Index-text layout in lite (ADR 0027 / 0028 amendments): a long verbatim-rows statement
scores as row units that repeat its header, and running headers / footers score nothing.

The synthetic report prints ``Acme Insurance Group Interim Report 2024`` and a page number on
every page, twelve narrative pages that mention insurance revenue in 2024, and a 32-row
unruled statement under a two-line header. The question is the short label-and-period kind
(``Insurance revenue 2024``) that ADR 0018 answers from BM25 alone.
"""

from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.document_catalog import (
    MountedDocument,
    mount_document,
    scan_catalog,
)
from enterprise_pdf_rag.adapters.draft_publication import DraftIndex, index_draft, qualify_draft
from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult
from enterprise_pdf_rag.adapters.hybrid_search import HybridSearch, lexical_rank
from enterprise_pdf_rag.adapters.ingest_mode import IngestPlan, ingest_plan
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.pdf_ingestion import ingest_pdf
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.answers.models import ClaimKind
from enterprise_pdf_rag.answers.page_window import with_page_context
from enterprise_pdf_rag.answers.ports import MemberText
from enterprise_pdf_rag.answers.prompt import ModelClaim
from enterprise_pdf_rag.answers.verify import verify_claims
from enterprise_pdf_rag.processing.context_builder import PageContextBlock, build_context_block
from enterprise_pdf_rag.processing.index_text import INDEX_VERSION, IndexTextOptions
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit
from ragspine.extraction.evidence.page.models import ObjectKind, PageInput, PagePartition
from ragspine.extraction.evidence.page.ports import PagePartitioner
from tests.enterprise_pdf_rag.adapters.index_layout_helpers import (
    ANSWER,
    PAGES,
    QUESTION,
    RUNNING_HEADER,
    STATEMENT_ROWS,
    report_env,
    report_pdf,
    run_report,
)
from tests.enterprise_pdf_rag.adapters.test_embedding_batch_index import _SingleOnly
from tests.enterprise_pdf_rag.adapters.test_embedding_batches import _adapter, _Endpoint
from tests.enterprise_pdf_rag.answers.fake_llm import answered

_OFF = {"table_row_index_units": False, "drop_running_lines_from_index": False}
# Every page prints one running header and one page number, each its own object.
_RUNNING_OBJECTS = 2 * PAGES


def _mounted(tmp_path: Path) -> MountedDocument:
    (entry,) = scan_catalog(tmp_path / "ingestion").documents
    return mount_document(entry, embedder=OfflineDescriptionEmbedder())


def _table(members: tuple[MemberText, ...]) -> MemberText:
    (table,) = (member for member in members if member.kind is ObjectKind.TABLE)
    return table


def _running(members: tuple[MemberText, ...]) -> list[MemberText]:
    return [member for member in members if member.units == ()]


def _seat(document: MountedDocument, member_id: str, mode: str = "auto") -> int | None:
    """The member's 1-based seat in the ranking the answer service reads."""
    outcome = HybridSearch(document, channel_limit=50).search(
        QUESTION,
        top_k=100,
        mode=mode,  # type: ignore[arg-type]
    )
    ranked = [hit.member_id for hit in outcome.hits]
    return ranked.index(member_id) + 1 if member_id in ranked else None


def _index_of(result: FolderPipelineResult) -> DraftIndex:
    (document,) = result.documents
    assert document.status == "published" and document.index is not None
    return document.index


def test_the_presets_turn_both_switches_on_in_lite_and_off_in_full() -> None:
    assert (
        ingest_plan("full").table_row_index_units,
        ingest_plan("full").drop_running_lines_from_index,
    ) == (False, False)
    assert ingest_plan("full").index_options.index_version == INDEX_VERSION
    lite = ingest_plan("lite")
    assert (lite.table_row_index_units, lite.drop_running_lines_from_index) == (True, True)
    assert lite.index_options == IndexTextOptions(table_row_units=True, drop_running_lines=True)
    off = ingest_plan("lite", table_row_index_units=False, drop_running_lines_from_index=False)
    assert off.index_options.index_version == INDEX_VERSION
    assert IngestPlan("full").index_options == IndexTextOptions()


def test_lite_seats_the_statement_row_first_and_answers_the_label_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = report_env(monkeypatch)
    report_pdf(tmp_path / "pdfs" / "report.pdf")

    # Before: one unit per member. Thirteen running headers and the narrative crowd the
    # whole 32-row statement out of the ten answer seats; the question abstains.
    before, _ = run_report(tmp_path, answers="answers-off", ingest_mode="lite", **_OFF)
    assert before.eval is not None
    (case,) = before.eval.cases
    assert case.verdict == "abstained"
    assert (_index_of(before).row_unit_tables, _index_of(before).unscored_running_members) == (0, 0)
    off = _mounted(tmp_path)
    table = _table(off.member_texts())
    assert table.units is None
    assert _seat(off, table.member_id) == 13
    assert _seat(off, table.member_id, "rrf") == 27
    layout_calls = tasks["page-layout"]
    assert layout_calls == PAGES

    # After (the lite preset): the statement's own row unit seats it first; answered from
    # the row, cited as ``fragments.row-N`` on the statement page, verified.
    after, prompts = run_report(tmp_path, answers="answers-on", ingest_mode="lite")
    assert tasks["page-layout"] == layout_calls  # the layout replays: zero new calls
    assert after.eval is not None
    (case,) = after.eval.cases
    assert (case.verdict, case.answer, case.cited_pages, case.claim_count) == (
        "answered",
        ANSWER,
        (PAGES,),
        1,
    )
    assert "fragments.row-" in prompts[-1]
    indexed = _index_of(after)
    assert (indexed.row_unit_tables, indexed.row_units) == (1, len(STATEMENT_ROWS))
    assert indexed.unscored_running_members == _RUNNING_OBJECTS
    # Only the row units are new texts: every other index text replays its cached vector,
    # and the running objects are embedded not at all.
    assert indexed.member_count == _index_of(before).member_count
    assert indexed.embedded_objects == len(STATEMENT_ROWS)
    on = _mounted(tmp_path)
    members = on.member_texts()
    table = _table(members)
    assert table.units is not None and len(table.units) == len(STATEMENT_ROWS)
    assert table.text == _table(off.member_texts()).text  # the page-window line is unchanged
    assert _seat(on, table.member_id) == 1
    assert _seat(on, table.member_id, "rrf") == 1
    # Each unit repeats the two header rows verbatim above its own row.
    head = "For the six months ended 30 June\nUS$m\t2024\t2023"
    revenue = next(unit for unit in table.units if "\nInsurance revenue\t" in unit)
    assert revenue.endswith(f"{head}\nInsurance revenue\t{ANSWER}\t9,016")

    # Switching back republishes the first release: no layout call, no embedding request.
    again, _ = run_report(tmp_path, answers="answers-again", ingest_mode="lite", **_OFF)
    assert tasks["page-layout"] == layout_calls
    assert _index_of(again).embedding_requests == 0
    assert _index_of(again).indexed_processing_id == _index_of(before).indexed_processing_id
    # Both releases stay in the store side by side; the pointer names the latest publish.
    (document,) = again.documents
    assert document.ingestion is not None
    store = ProcessingStore(Path(document.ingestion.processing_store))
    for release in (after, again):
        assert store.load(_index_of(release).indexed_processing_id).retrieval is not None
    assert store.current_id() == _index_of(again).indexed_processing_id


# ---- running headers / footers: unscored, still members --------------------------------------


def test_a_running_header_scores_nothing_yet_stays_quotable_and_in_its_page_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_env(monkeypatch)
    report_pdf(tmp_path / "pdfs" / "report.pdf")
    run_report(tmp_path, answers="answers", ingest_mode="lite")
    document = _mounted(tmp_path)
    members = document.member_texts()
    running = _running(members)
    assert len(running) == _RUNNING_OBJECTS
    assert {member.body for member in running} == {RUNNING_HEADER} | {
        str(page) for page in range(1, PAGES + 1)
    }
    unscored = {member.member_id for member in running}
    # Neither channel scores them: BM25 never sees their words, the vector index holds none.
    search = HybridSearch(document, channel_limit=50)
    assert not unscored & {
        hit.member_id for hit in lexical_rank(search.index, RUNNING_HEADER, limit=100)
    }
    assert not unscored & {hit.member_id for hit in document.search(RUNNING_HEADER, limit=100)}
    # Everything that is not running keeps its one unit (the table its row units).
    assert all(
        member.units is None
        for member in members
        if member.member_id not in unscored and member.kind is not ObjectKind.TABLE
    )

    # It is still a member: resolved, rendered and quotable verbatim.
    header = next(
        member for member in running if member.body == RUNNING_HEADER and member.page_index == 0
    )
    block = build_context_block(
        document.resolve(PinnedRetrievalHit(document.retrieval_snapshot_id, header.member_id, 0.0))
    )
    path = next(
        line.split(": ", 1)[0]
        for line in block.prompt_text().splitlines()
        if line.startswith("fragments.")
    )
    claim = ModelClaim(
        claim_id="c1",
        member_id=header.member_id,
        kind="quote",
        field_path=path,
        text="Interim Report 2024",
    )
    verification = verify_claims(
        answered("Interim Report 2024", claim),
        {header.member_id: block},
        chart_evidence=lambda member_id: (_ for _ in ()).throw(AssertionError(member_id)),
    )
    (verified,) = verification.verified
    assert verified.kind is ClaimKind.QUOTE and verified.citations[0].page_index == 0
    # And its page window still prints it beside a hit on that page.
    statement = _table(members)
    table_block = build_context_block(
        document.resolve(
            PinnedRetrievalHit(document.retrieval_snapshot_id, statement.member_id, 1.0)
        )
    )
    woven = with_page_context((table_block,), members, max_chars=20_000)
    page = woven[1]
    assert isinstance(page, PageContextBlock)
    assert RUNNING_HEADER in page.prompt_text() and page.page_index == PAGES - 1


class _OnnxStyle:
    """Stands in for a layout that marks no role: the model's regions, another producer."""

    def __init__(self, inner: PagePartitioner) -> None:
        self._inner = inner
        self.fingerprint = "page-layout-onnx-style-stub-v1:" + inner.fingerprint

    def partition(self, page: PageInput) -> PagePartition:
        proposed = self._inner.partition(page)
        return replace(
            proposed,
            producer="page-layout-onnx-style-stub-v1",
            objects=tuple(replace(item, interpretation="region") for item in proposed.objects),
        )


@pytest.mark.parametrize("layout", ["model", "deterministic-text-pages", "onnx-style"])
def test_running_lines_are_unscored_whatever_proposed_the_objects(
    layout: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_env(monkeypatch)
    report_pdf(tmp_path / "pdfs" / "report.pdf")
    options: dict[str, object] = {}
    if layout == "deterministic-text-pages":
        options["layout_policy"] = layout
    if layout == "onnx-style":
        from enterprise_pdf_rag.adapters import ingest_mode, pdf_ingestion

        monkeypatch.setattr(
            pdf_ingestion,
            "make_partitioner",
            lambda *args: _OnnxStyle(ingest_mode.make_partitioner(*args)),
        )
    result, _ = run_report(tmp_path, answers="answers", ingest_mode="lite", **options)
    (document,) = result.documents
    assert document.ingestion is not None
    if layout == "deterministic-text-pages":
        assert document.ingestion.pages_partitioned_deterministically > 0
    if layout == "onnx-style":
        manifest = ProcessingStore(Path(document.ingestion.processing_store)).load(
            document.ingestion.processing_id
        )
        assert all("onnx-style" in page.partition.producer for page in manifest.pages)
    assert _index_of(result).unscored_running_members == _RUNNING_OBJECTS
    running = _running(_mounted(tmp_path).member_texts())
    assert {member.body for member in running} == {RUNNING_HEADER} | {
        str(page) for page in range(1, PAGES + 1)
    }


# ---- embeddings: batched units share the single path's bytes ---------------------------------


def test_row_units_batch_like_single_texts_and_a_reindex_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_env(monkeypatch)
    pdf = report_pdf(tmp_path / "report.pdf")
    options = ingest_plan("lite").index_options
    stores = []
    for name in ("single", "batch"):
        summary = ingest_pdf(
            pdf=pdf,
            stage="semantics",
            max_live_calls=50,
            output_dir=tmp_path / name,
            ingest_mode="lite",
        )
        source_store, processing_store = Path(summary.source_store), Path(summary.processing_store)
        qualify_draft(
            source_store=source_store,
            processing_store=processing_store,
            processing_id=summary.processing_id,
        )
        stores.append((source_store, processing_store, summary.processing_id))

    def index(store: tuple[Path, Path, str], embedder: object) -> DraftIndex:
        return index_draft(
            source_store=store[0],
            processing_store=store[1],
            processing_id=store[2],
            embedder=embedder,  # type: ignore[arg-type]
            index_options=options,
        )

    single_endpoint, batch_endpoint = _Endpoint(), _Endpoint()
    single = index(stores[0], _SingleOnly(_adapter(single_endpoint)))
    batched = index(stores[1], _adapter(batch_endpoint, batch_max_items=16))
    texts = single.member_count - _RUNNING_OBJECTS - 1 + len(STATEMENT_ROWS)
    assert (single.embedding_requests, single.embedded_objects) == (texts, texts)
    assert (batched.embedding_requests, batched.embedded_objects) == (-(-texts // 16), texts)
    assert batched.retrieval_snapshot_id == single.retrieval_snapshot_id
    assert (batched.row_units, batched.unscored_running_members) == (
        len(STATEMENT_ROWS),
        _RUNNING_OBJECTS,
    )
    again = index(stores[1], _adapter(_Endpoint()))
    assert (again.embedding_requests, again.embedded_objects) == (0, 0)
    assert again.retrieval_snapshot_id == batched.retrieval_snapshot_id
    # The unit index re-verifies on load: every member's vectors are its artifact's own.
    outputs = ProcessingStore(stores[1][1], verify_every_request=True)
    manifest = outputs.load(again.indexed_processing_id)
    assert manifest.retrieval is not None
    plan, index_ = outputs.load_retrieval(manifest.retrieval)
    assert plan.index_version == options.index_version
    assert len(index_.entries) == single.member_count - _RUNNING_OBJECTS - 1 + len(STATEMENT_ROWS)
    assert Counter(entry.member_id for entry in index_.entries).most_common(1)[0][1] == len(
        STATEMENT_ROWS
    )
