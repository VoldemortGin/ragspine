"""Published documents are listed, pinned and searched by id with no writes or model calls."""

import shutil
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_catalog import (
    CatalogEntry,
    MountedDocument,
    QueryEmbeddingUnavailable,
    catalog_trees,
    mount_catalog,
    mount_document,
    scan_catalog,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.document_tree_extraction import annotate_document_tree
from enterprise_pdf_rag.adapters.draft_publication import DraftPublication
from enterprise_pdf_rag.adapters.http.processing_schemas import ProcessingEnvelope
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.answers.member_filter import (
    candidate_members,
    member_matches,
    region_vocabulary,
)
from enterprise_pdf_rag.answers.models import MemberFilters
from enterprise_pdf_rag.answers.ports import MemberText
from enterprise_pdf_rag.processing.index_text import chart_index_text
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit, RetrievalIndex
from ragspine.extraction.evidence.document.models import AssetRef
from ragspine.extraction.evidence.figures.ports import EmbeddingPort
from ragspine.extraction.evidence.page.models import ObjectKind, StageState
from tests.enterprise_pdf_rag.adapters.column_page_helpers import (
    ColumnPage,
    publish_column_page,
)
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    TABLE_BBOX,
    ingest_generic_semantics,
    publish_generic_document,
    table_region,
)
from tests.enterprise_pdf_rag.adapters.page_metadata_helpers import publish_with_metadata
from tests.enterprise_pdf_rag.adapters.test_chart_qa_store import published_chart
from tests.enterprise_pdf_rag.processing.test_persistent_retrieval import RecordingEmbedding

_MISSING_ID = "f" * 64


class CountingEmbedder:
    """Counts every embedding call; reading the fingerprint costs nothing."""

    def __init__(self, inner: EmbeddingPort) -> None:
        self._inner = inner
        self.description_calls = 0
        self.query_calls = 0

    @property
    def fingerprint(self) -> str:
        return self._inner.fingerprint

    def embed_description(self, text: str) -> tuple[float, ...]:
        self.description_calls += 1
        return self._inner.embed_description(text)

    def embed_query(self, text: str) -> tuple[float, ...]:
        self.query_calls += 1
        return self._inner.embed_query(text)


@pytest.fixture
def two_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, DraftPublication, DraftPublication]:
    root = tmp_path / "ingestion"
    embedder = OfflineDescriptionEmbedder()
    meridian = publish_generic_document(
        tmp_path,
        monkeypatch,
        filename="meridian.pdf",
        label="Meridian revenue",
        page_count=3,
        embedder=embedder,
        output_dir=root,
    )
    orion = publish_generic_document(
        tmp_path,
        monkeypatch,
        filename="orion.pdf",
        label="Orion expense",
        page_count=2,
        embedder=embedder,
        output_dir=root,
    )
    ingest_generic_semantics(
        tmp_path,
        monkeypatch,
        filename="draft.pdf",
        label="Draft only",
        page_count=1,
        output_dir=root,
    )
    return root, meridian, orion


def _ready_entry(root: Path, publication: DraftPublication) -> CatalogEntry:
    entry = scan_catalog(root).entry(publication.source_sha256)
    assert entry is not None and entry.retrieval_status == "ready", entry
    return entry


def _store_state(root: Path) -> tuple[tuple[str, bytes | None, tuple[str, ...]], ...]:
    state = []
    for store in sorted(path for path in root.rglob("objects") if (path / "sha256").is_dir()):
        base = store.parent
        pointers = tuple(
            (name, (base / name).read_bytes() if (base / name).is_file() else None)
            for name in ("current-manifest", "current-processing")
        )
        objects = tuple(sorted(item.name for item in (store / "sha256").iterdir()))
        state.append((str(base), pointers[0][1] or pointers[1][1], objects))
    return tuple(state)


def _forge_index(publication: DraftPublication) -> None:
    """Point the discovery pointer at a manifest whose index vector no longer matches."""
    outputs = ProcessingStore(Path(publication.processing_store))
    manifest = outputs.load(publication.current_processing_id)
    assert manifest.retrieval is not None
    index = TypeAdapter(RetrievalIndex).validate_json(outputs.assets.get(manifest.retrieval.index))
    first = index.entries[0]
    forged_index = replace(
        index, entries=(replace(first, vector=(1.0, *first.vector[1:])), *index.entries[1:])
    )
    index_ref = outputs.assets.put(
        TypeAdapter(RetrievalIndex).dump_json(forged_index), media_type="application/json"
    )
    forged = replace(manifest, retrieval=replace(manifest.retrieval, index=index_ref))
    forged_ref = outputs.assets.put(
        ProcessingEnvelope(manifest=forged).model_dump_json().encode(),
        media_type="application/json",
    )
    (outputs.root / "current-processing").write_text(forged_ref.sha256 + "\n")


def test_scan_lists_two_ready_documents_and_one_draft(
    two_published: tuple[Path, DraftPublication, DraftPublication], tmp_path: Path
) -> None:
    root, meridian, orion = two_published
    catalog = scan_catalog(root)

    assert catalog.catalog_policy == "published-current-processing-with-retrieval-v1"
    assert catalog.ingestion_root == str(root.resolve())
    assert catalog.legacy_roots == ()
    assert len(catalog.ready) == 2 and len(catalog.documents) == 2
    assert [entry.document_id for entry in catalog.documents] == sorted(
        (meridian.source_sha256, orion.source_sha256)
    )
    for publication, pages in ((meridian, 3), (orion, 2)):
        entry = catalog.entry(publication.source_sha256)
        assert entry is not None
        assert entry.origin == "ingestion" and entry.reason is None
        assert entry.source_sha256 == entry.document_id == publication.source_sha256
        assert entry.source_manifest_id == publication.source_manifest_id
        assert entry.current_processing_id == publication.current_processing_id
        assert entry.retrieval_snapshot_id == publication.retrieval_snapshot_id
        assert entry.member_count == publication.member_count == pages
        assert entry.embedding_fingerprint == "offline-demo/token-hash-64-v1"
        assert entry.embedding_dimensions == (64,)
        assert Path(entry.source_store).is_absolute()
        assert entry.source_store == publication.source_store
        assert entry.processing_store == publication.processing_store
        assert entry.source_page_count == pages
        assert entry.selected_physical_pages == tuple(range(1, pages + 1))
        assert entry.source_activated is True
    labels = {entry.document_label for entry in catalog.documents}
    assert labels == {"meridian.pdf", "orion.pdf"}
    draft_sha = sha256((tmp_path / "draft.pdf").read_bytes()).hexdigest()
    assert catalog.unpublished == (draft_sha,)
    assert catalog.entry(draft_sha) is None


def test_scan_missing_root_is_empty(tmp_path: Path) -> None:
    catalog = scan_catalog(tmp_path / "absent")
    assert catalog.documents == () and catalog.unpublished == ()
    assert catalog.ready == ()


def test_scan_marks_corrupt_pointer_visible_and_unmountable(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    root, meridian, orion = two_published
    (Path(meridian.processing_store) / "current-processing").write_text(_MISSING_ID + "\n")

    catalog = scan_catalog(root)
    broken = catalog.entry(meridian.source_sha256)
    assert broken is not None
    assert broken.retrieval_status == "corrupt"
    assert broken.reason
    assert broken.current_processing_id == _MISSING_ID
    assert catalog.ready == (catalog.entry(orion.source_sha256),)
    with pytest.raises(ValueError, match="not mountable"):
        mount_document(broken, embedder=OfflineDescriptionEmbedder())


def test_scan_marks_tampered_index_corrupt(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    root, meridian, _ = two_published
    _forge_index(meridian)

    entry = scan_catalog(root).entry(meridian.source_sha256)
    assert entry is not None
    assert entry.retrieval_status == "corrupt"
    assert entry.reason is not None
    assert "Index vector does not match its actual embedding artifact" in entry.reason
    assert entry.current_processing_id not in (None, meridian.current_processing_id)
    assert entry.retrieval_snapshot_id is None


def test_scan_marks_directory_name_mismatch_corrupt(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    root, meridian, _ = two_published
    renamed = root / ("0" * 63 + "1")
    (root / meridian.source_sha256).rename(renamed)

    entry = scan_catalog(root).entry(renamed.name)
    assert entry is not None
    assert entry.retrieval_status == "corrupt"
    assert entry.reason is not None and "does not match" in entry.reason
    assert entry.source_sha256 == meridian.source_sha256


def test_scan_discovers_legacy_root_and_flags_duplicates(
    two_published: tuple[Path, DraftPublication, DraftPublication], tmp_path: Path
) -> None:
    root, meridian, _ = two_published
    legacy = tmp_path / "legacy-source"
    shutil.copytree(meridian.source_store, legacy)
    shutil.copytree(meridian.processing_store, legacy / "release")

    alone = scan_catalog(tmp_path / "absent", legacy_roots=(legacy / "release",))
    assert alone.legacy_roots == (str((legacy / "release").resolve()),)
    assert len(alone.documents) == 1
    entry = alone.documents[0]
    assert entry.origin == "legacy" and entry.retrieval_status == "ready"
    assert entry.document_id == meridian.source_sha256
    assert entry.processing_store == str((legacy / "release").resolve())
    assert entry.source_store == str(legacy.resolve())

    both = scan_catalog(root, legacy_roots=(legacy / "release",))
    statuses = [
        (item.origin, item.retrieval_status)
        for item in both.documents
        if item.document_id == meridian.source_sha256
    ]
    assert statuses == [("ingestion", "ready"), ("legacy", "corrupt")]
    duplicate = both.documents[[item.origin for item in both.documents].index("legacy")]
    assert duplicate.reason == f"duplicate document id {meridian.source_sha256}"

    missing = scan_catalog(tmp_path / "absent", legacy_roots=(tmp_path / "no-such-store",))
    assert missing.documents[0].retrieval_status == "corrupt"


def test_scan_and_mount_are_read_only(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    root, meridian, _ = two_published
    before = _store_state(root)

    catalog = scan_catalog(root)
    mounted = mount_document(_ready_entry(root, meridian), embedder=OfflineDescriptionEmbedder())
    hits = mounted.search("Meridian revenue", limit=2)
    mounted.resolve(hits[0])
    mounted.member_texts()

    assert len(catalog.ready) == 2
    assert _store_state(root) == before


def test_mount_rejects_wrong_embedder_fingerprint_without_embedding(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    root, meridian, _ = two_published
    embedder = CountingEmbedder(RecordingEmbedding())

    with pytest.raises(ValueError, match="provider"):
        mount_document(_ready_entry(root, meridian), embedder=embedder)
    assert (embedder.description_calls, embedder.query_calls) == (0, 0)


def test_mount_and_search_stay_within_one_document(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    root, meridian, orion = two_published
    mount_a = mount_document(_ready_entry(root, meridian), embedder=OfflineDescriptionEmbedder())
    mount_b = mount_document(_ready_entry(root, orion), embedder=OfflineDescriptionEmbedder())

    assert mount_a.document_id == mount_a.source_sha256 == meridian.source_sha256
    assert mount_a.processing_id == meridian.current_processing_id
    assert mount_a.retrieval_snapshot_id == meridian.retrieval_snapshot_id
    hits = mount_a.search("Meridian revenue", limit=5)
    assert hits and all(hit.snapshot_id == meridian.retrieval_snapshot_id for hit in hits)
    context = mount_a.resolve(hits[0])
    assert "Meridian" in context.description.text
    assert context.member.page_index in range(3)

    foreign = mount_b.search("Orion expense", limit=1)[0]
    assert foreign.snapshot_id == orion.retrieval_snapshot_id
    with pytest.raises(ValueError, match="another semantic snapshot"):
        mount_a.resolve(foreign)


def test_mount_makes_no_embedding_calls_and_search_makes_one(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    root, meridian, _ = two_published
    embedder = CountingEmbedder(OfflineDescriptionEmbedder())

    mounted = mount_document(_ready_entry(root, meridian), embedder=embedder)
    assert (embedder.description_calls, embedder.query_calls) == (0, 0)
    hits = mounted.search("Meridian revenue")
    assert (embedder.description_calls, embedder.query_calls) == (0, 1)
    mounted.resolve(hits[0])
    mounted.manifest()
    assert (embedder.description_calls, embedder.query_calls) == (0, 1)


def test_mount_pins_snapshot_after_pointer_moves(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    root, meridian, _ = two_published
    mounted = mount_document(_ready_entry(root, meridian), embedder=OfflineDescriptionEmbedder())
    (Path(meridian.processing_store) / "current-processing").write_text(_MISSING_ID + "\n")

    assert mounted.search("Meridian revenue", limit=1)
    assert mounted.manifest().scope.source_sha256 == meridian.source_sha256
    rescanned = scan_catalog(root).entry(meridian.source_sha256)
    assert rescanned is not None and rescanned.retrieval_status == "corrupt"


def test_mount_without_embedder_serves_evidence_only(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    root, meridian, _ = two_published
    entry = _ready_entry(root, meridian)
    evidence_only = mount_document(entry, embedder=None)
    searching = mount_document(entry, embedder=OfflineDescriptionEmbedder())

    hit = searching.search("Meridian revenue", limit=1)[0]
    assert evidence_only.resolve(hit).member.member_id == hit.member_id
    with pytest.raises(QueryEmbeddingUnavailable):
        evidence_only.search("Meridian revenue")


def test_mount_catalog_records_failures(
    two_published: tuple[Path, DraftPublication, DraftPublication],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, meridian, orion = two_published
    other = publish_generic_document(
        tmp_path,
        monkeypatch,
        filename="vega.pdf",
        label="Vega margin",
        page_count=1,
        embedder=RecordingEmbedding(),
        output_dir=root,
    )
    (Path(orion.processing_store) / "current-processing").write_text(_MISSING_ID + "\n")

    mounted = mount_catalog(scan_catalog(root), embedder=OfflineDescriptionEmbedder())
    assert mounted.embedding_fingerprint == "offline-demo/token-hash-64-v1"
    assert set(mounted.documents) == {meridian.source_sha256}
    assert isinstance(mounted.documents[meridian.source_sha256], MountedDocument)
    assert set(mounted.failures) == {orion.source_sha256, other.source_sha256}
    assert "provider" in mounted.failures[other.source_sha256]
    assert mounted.failures[orion.source_sha256]
    unmounted = mount_catalog(scan_catalog(root), embedder=None)
    assert unmounted.embedding_fingerprint is None
    assert set(unmounted.documents) == {meridian.source_sha256, other.source_sha256}


def test_member_texts_are_sorted_embedded_descriptions_without_model_calls(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    root, meridian, _ = two_published
    embedder = CountingEmbedder(OfflineDescriptionEmbedder())
    mounted = mount_document(_ready_entry(root, meridian), embedder=embedder)

    texts = mounted.member_texts()
    assert len(texts) == meridian.member_count == 3
    assert [item.member_id for item in texts] == sorted(item.member_id for item in texts)
    assert all(item.kind is ObjectKind.TEXT and "Meridian" in item.text for item in texts)
    assert sorted(item.page_index for item in texts) == [0, 1, 2]
    for item in texts:
        context = mounted.resolve(
            PinnedRetrievalHit(meridian.retrieval_snapshot_id, item.member_id, 0.0)
        )
        assert context.description.text == item.text
    assert (embedder.description_calls, embedder.query_calls) == (0, 0)


def test_member_texts_carry_the_page_rectangle_that_orders_a_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Members expose where they sit on their page, so a page reads in one order."""
    root = tmp_path / "ingestion"
    published = publish_generic_document(
        tmp_path,
        monkeypatch,
        filename="meridian.pdf",
        label="Meridian revenue",
        page_count=2,
        embedder=OfflineDescriptionEmbedder(),
        output_dir=root,
        table_page=True,
    )
    mounted = mount_document(_ready_entry(root, published), embedder=None)

    texts = mounted.member_texts()
    assert texts and all(item.bbox is not None for item in texts)
    # Without the page-metadata stage nothing is prefixed, so the body is the whole text.
    assert all(item.header == "" and item.body == item.text for item in texts)
    boxed = [(item.bbox, item) for item in texts if item.bbox is not None]
    last_page = [pair for pair in boxed if pair[1].page_index == 1]  # the authored grid
    assert {pair[1].kind for pair in last_page} == {ObjectKind.TEXT, ObjectKind.TABLE}
    ordered = sorted(last_page, key=lambda pair: (round(pair[0][1] / 4.0), pair[0][0]))
    # The authored caption line is printed above the ruled grid's own layout region.
    assert [pair[1].kind for pair in ordered] == [ObjectKind.TEXT, ObjectKind.TABLE]
    assert ordered[1][0] == table_region(TABLE_BBOX)
    assert ordered[0][0][1] < ordered[1][0][1]


def test_member_texts_split_the_contextual_header_from_their_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ADR 0013 header is reported beside the text, never folded into the body."""
    root = tmp_path / "ingestion"
    published = publish_with_metadata(
        tmp_path,
        monkeypatch,
        filename="meridian.pdf",
        label="Meridian 1H26 Hong Kong",
        page_count=2,
        output_dir=root,
    )
    mounted = mount_document(_ready_entry(root, published), embedder=None)

    texts = mounted.member_texts()
    assert texts and all(item.header for item in texts)
    for item in texts:
        assert item.text == f"{item.header}\n{item.body}"
        hit = PinnedRetrievalHit(published.retrieval_snapshot_id, item.member_id, 1.0)
        assert item.body == mounted.resolve(hit).description.text
        assert item.bbox is not None


def _mounted_columns(page: ColumnPage) -> MountedDocument:
    """Mount the authored side-by-side page read-only; the release must scan as ready."""
    entry = scan_catalog(page.root).entry(page.document_id)
    assert entry is not None and entry.retrieval_status == "ready", entry
    return mount_document(entry, embedder=None)


def _left_to_right(texts: tuple[MemberText, ...]) -> list[MemberText]:
    """The page's chart members in the order they are printed across the page."""
    charts = [item for item in texts if item.kind is ObjectKind.CHART]
    assert all(item.bbox is not None for item in charts)
    return sorted(charts, key=lambda item: item.bbox[0] if item.bbox else 0.0)


def test_member_texts_bind_each_side_by_side_chart_to_the_heading_standing_over_it(
    tmp_path: Path,
) -> None:
    """Three charts on one page are three different markets, and the geometry says which.

    Page metadata is page-wide (ADR 0013), so all three charts carry all three headings and
    no filter can tell them apart. The fixture prints the same two labels in every column on
    purpose: the heading each chart stands under is then the only thing that distinguishes
    them, so an assertion about it cannot pass by accident.
    """
    page = publish_column_page(tmp_path / "ingestion")
    mounted = _mounted_columns(page)

    texts = mounted.member_texts()
    charts = _left_to_right(texts)
    assert len(charts) == len(page.headings)
    for chart, heading in zip(charts, page.headings, strict=True):
        # The page's own banner plus this column's heading — never a neighbour's.
        assert chart.member_regions == (page.banner, heading)
        assert all(other not in chart.member_regions for other in page.headings if other != heading)
        # Nothing is lost: the page-level tuple still carries every value the page prints.
        assert chart.regions == page.page_regions
        # The lexical channel reads one more phrase; the vectors read exactly what they did.
        assert chart.header == f"{page.header} | {heading}"
        assert chart.text == f"{chart.header}\n{chart.body}"
        assert chart.body == page.chart_body
        hit = PinnedRetrievalHit(mounted.retrieval_snapshot_id, chart.member_id, 1.0)
        assert chart.body == mounted.resolve(hit).description.text
    (band,) = (item for item in texts if item.kind is ObjectKind.TEXT)
    # A member the geometry places in no column keeps answering for the whole page.
    assert band.member_regions == ()
    assert band.regions == page.page_regions
    assert band.header == page.header and band.text == f"{band.header}\n{band.body}"
    # Narrowing who a value belongs to must not shrink what a question can be parsed against.
    assert region_vocabulary(texts) == page.page_regions


def test_a_region_filter_admits_the_chart_under_that_heading_and_not_its_neighbours(
    tmp_path: Path,
) -> None:
    """What the binding is for: the column answers for itself, the page still answers too."""
    page = publish_column_page(tmp_path / "ingestion")
    mounted = _mounted_columns(page)

    texts = mounted.member_texts()
    thailand, singapore, malaysia = _left_to_right(texts)
    (band,) = (item for item in texts if item.kind is ObjectKind.TEXT)
    filters = MemberFilters(regions=(page.headings[0],))

    assert member_matches(thailand, filters)
    assert not member_matches(singapore, filters) and not member_matches(malaysia, filters)
    # The heading band really does print all four values, so it stays a candidate.
    assert candidate_members(texts, filters) == frozenset({thailand.member_id, band.member_id})


@pytest.mark.parametrize("charts", (1, 2))
def test_a_page_that_does_not_read_as_columns_keeps_every_member_page_wide(
    tmp_path: Path, charts: int
) -> None:
    """One chart is never ambiguous; a heading over no chart means we misread the layout.

    Either way the binding must cost nothing: the page-level values stand, and every member's
    index text is byte-identical to what it was before any of this existed.
    """
    page = publish_column_page(tmp_path / "ingestion", charts=charts)
    mounted = _mounted_columns(page)

    texts = mounted.member_texts()
    assert len(_left_to_right(texts)) == charts
    for item in texts:
        assert item.member_regions == ()
        assert item.regions == page.page_regions
        assert item.header == page.header
        assert item.text == f"{page.header}\n{item.body}"
    assert all(item.body == page.chart_body for item in _left_to_right(texts))


def test_chart_and_displayed_contexts_requalify_only_pinned_chart_members(
    two_published: tuple[Path, DraftPublication, DraftPublication], tmp_path: Path
) -> None:
    root, meridian, _ = two_published
    sources, outputs, pin = published_chart(tmp_path / "chart")
    chart_sha = outputs.load(pin.processing_id).scope.source_sha256
    shutil.move(sources.root, root / chart_sha / "source")
    shutil.move(outputs.root, root / chart_sha / "processing")
    embedder = CountingEmbedder(RecordingEmbedding())
    chart_entry = scan_catalog(root).entry(chart_sha)
    assert chart_entry is not None and chart_entry.retrieval_status == "ready", chart_entry
    assert chart_entry.embedding_fingerprint == RecordingEmbedding.fingerprint
    charts = mount_document(chart_entry, embedder=embedder)
    texts = mount_document(_ready_entry(root, meridian), embedder=None)
    chart_hit = PinnedRetrievalHit(pin.snapshot_id, pin.member_id, 1.0)

    assert charts.processing_id == pin.processing_id
    context = charts.chart_context(chart_hit)
    assert context.pin == pin
    assert context.chart.points and context.svg.svg
    # The lexical corpus scores the same projection the vector channel embedded (ADR 0012):
    # the donut is findable by category / value, not only by its title.
    (donut_text,) = charts.member_texts()
    assert donut_text.text == chart_index_text(context.chart, fallback=context.description.text)
    assert donut_text.text == (
        "Distribution Mix 1H26 donut chart figure Agency VONB 72% Partnerships VONB 28%"
    )
    assert context.description.text == "Distribution Mix"
    with pytest.raises(ValueError, match="unqualified_member"):
        charts.displayed_context(chart_hit)
    text_member = texts.member_texts()[0]
    literal_hit = PinnedRetrievalHit(meridian.retrieval_snapshot_id, text_member.member_id, 1.0)
    with pytest.raises(ValueError, match="unqualified_member"):
        texts.chart_context(literal_hit)
    with pytest.raises(ValueError, match="another semantic snapshot"):
        charts.chart_context(literal_hit)
    with pytest.raises(ValueError, match="another semantic snapshot"):
        charts.displayed_context(literal_hit)
    with pytest.raises(ValueError, match="another semantic snapshot"):
        texts.chart_context(chart_hit)
    assert (embedder.description_calls, embedder.query_calls) == (0, 0)


def test_label_only_chart_member_is_not_expanded_by_the_index_text_projection(
    tmp_path: Path,
) -> None:
    """A chart without a citable value keeps its description text (ADR 0012 regression)."""
    sources, outputs, pin = published_chart(tmp_path / "chart", labels=True)
    root = tmp_path / "root"
    chart_sha = outputs.load(pin.processing_id).scope.source_sha256
    shutil.move(sources.root, root / chart_sha / "source")
    shutil.move(outputs.root, root / chart_sha / "processing")
    entry = scan_catalog(root).entry(chart_sha)
    assert entry is not None and entry.retrieval_status == "ready", entry
    mounted = mount_document(entry, embedder=None)
    hit = PinnedRetrievalHit(pin.snapshot_id, pin.member_id, 1.0)

    context = mounted.chart_context(hit)
    assert not context.chart.points or all(
        point.value.value is None for point in context.chart.points
    )
    (text,) = mounted.member_texts()
    assert text.kind is ObjectKind.CHART
    assert text.text == context.description.text == "Distribution Mix"
    assert "chart figure" not in text.text


def _count_asset_reads(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Start counting every content-addressed read any store performs from now on."""
    reads = [0]
    get = LocalDocumentStore.get
    read_content = LocalDocumentStore.read_content

    def counted_get(self: LocalDocumentStore, ref: AssetRef) -> bytes:
        reads[0] += 1
        return get(self, ref)

    def counted_read_content(self: LocalDocumentStore, digest: str) -> bytes:
        reads[0] += 1
        return read_content(self, digest)

    monkeypatch.setattr(LocalDocumentStore, "get", counted_get)
    monkeypatch.setattr(LocalDocumentStore, "read_content", counted_read_content)
    return lambda: reads[0]


def _pinned_manifest_path(publication: DraftPublication) -> Path:
    store = ProcessingStore(Path(publication.processing_store))
    return store.assets.content_path(publication.current_processing_id)


def test_a_warm_mount_re_reads_the_pinned_manifest_object_and_nothing_else(
    two_published: tuple[Path, DraftPublication, DraftPublication],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release was verified at mount; a later request only proves it has not drifted."""
    root, meridian, _ = two_published
    mounted = mount_document(_ready_entry(root, meridian), embedder=OfflineDescriptionEmbedder())
    hit = mounted.search("Meridian revenue", limit=1)[0]
    first = mounted.resolve(hit)

    reads = _count_asset_reads(monkeypatch)
    assert mounted.manifest().scope.source_sha256 == meridian.source_sha256
    assert mounted.search("Meridian revenue", limit=1)[0] == hit
    assert mounted.resolve(hit) == first
    assert reads() == 0


def test_verify_every_request_re_reads_the_whole_release_every_time(
    two_published: tuple[Path, DraftPublication, DraftPublication],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit setting keeps the old behaviour: nothing is reused between requests."""
    root, meridian, _ = two_published
    audited = mount_document(
        _ready_entry(root, meridian),
        embedder=OfflineDescriptionEmbedder(),
        verify_every_request=True,
    )
    hit = audited.search("Meridian revenue", limit=1)[0]
    audited.resolve(hit)

    reads = _count_asset_reads(monkeypatch)
    audited.manifest()
    after_manifest = reads()
    audited.resolve(hit)
    assert after_manifest > 0
    assert reads() > after_manifest


def test_a_rewritten_manifest_object_is_refused_by_the_next_request(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    """Drift is still refused, and the evidence hydrated under the old bytes goes with it."""
    root, meridian, _ = two_published
    mounted = mount_document(_ready_entry(root, meridian), embedder=OfflineDescriptionEmbedder())
    hit = mounted.search("Meridian revenue", limit=1)[0]
    mounted.resolve(hit)

    path = _pinned_manifest_path(meridian)
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(ValueError, match="digest mismatch"):
        mounted.manifest()
    with pytest.raises(ValueError, match="digest mismatch"):
        mounted.resolve(hit)
    with pytest.raises(ValueError, match="digest mismatch"):
        mounted.search("Meridian revenue", limit=1)


def test_a_same_length_rewrite_of_the_manifest_object_is_refused_too(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    """Size alone never clears a file: a touched manifest object is always re-hashed."""
    root, meridian, _ = two_published
    mounted = mount_document(_ready_entry(root, meridian), embedder=OfflineDescriptionEmbedder())
    mounted.manifest()

    path = _pinned_manifest_path(meridian)
    payload = path.read_bytes()
    path.write_bytes(payload[:-1] + bytes([payload[-1] ^ 0x20]))
    assert len(path.read_bytes()) == len(payload)

    with pytest.raises(ValueError, match="digest mismatch"):
        mounted.manifest()


def test_an_untouched_manifest_object_survives_being_restamped(
    two_published: tuple[Path, DraftPublication, DraftPublication],
) -> None:
    """Rewriting the same bytes changes the stamp but not the release, so it still mounts."""
    root, meridian, _ = two_published
    mounted = mount_document(_ready_entry(root, meridian), embedder=OfflineDescriptionEmbedder())
    mounted.manifest()

    path = _pinned_manifest_path(meridian)
    path.write_bytes(path.read_bytes())

    assert mounted.manifest().scope.source_sha256 == meridian.source_sha256


def test_one_retrieval_publication_is_parsed_once_per_store(
    two_published: tuple[Path, DraftPublication, DraftPublication],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan and index are named by content, so their validated parse is reusable."""
    _, meridian, _ = two_published
    store = ProcessingStore(Path(meridian.processing_store))
    publication = store.load(meridian.current_processing_id).retrieval
    assert publication is not None
    plan, index = store.load_retrieval(publication)

    reads = _count_asset_reads(monkeypatch)
    assert store.load_retrieval(publication) == (plan, index)
    assert reads() == 0

    audited = ProcessingStore(Path(meridian.processing_store), verify_every_request=True)
    audited.load_retrieval(publication)
    assert reads() > 0


def test_a_document_tree_is_absent_until_it_is_folded_and_then_mounts_with_its_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ingestion"
    publication = publish_with_metadata(
        tmp_path,
        monkeypatch,
        filename="meridian.pdf",
        label="Meridian revenue",
        page_count=3,
        output_dir=root,
    )
    assert _ready_entry(root, publication).tree_available is False

    folded = annotate_document_tree(
        LocalDocumentStore(Path(publication.source_store), activate_on_publish=False),
        ProcessingStore(Path(publication.processing_store)),
        processing_id=publication.current_processing_id,
        client=None,
    )
    assert folded.state is StageState.SUCCEEDED

    entry = _ready_entry(root, publication)
    assert entry.tree_available is True
    mounted = mount_document(entry, embedder=OfflineDescriptionEmbedder())
    tree = mounted.document_tree()
    assert tree is not None and tree.source_sha256 == publication.source_sha256
    assert tree.page_count == 3
    # Read once and kept: the tree is immutable with the pinned manifest.
    assert mounted.document_tree() is tree
    catalog = mount_catalog(scan_catalog(root), embedder=OfflineDescriptionEmbedder())
    assert catalog_trees(catalog) == {publication.source_sha256: tree}
