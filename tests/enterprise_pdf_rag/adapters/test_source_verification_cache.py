"""A store verifies a content-addressed snapshot once per instance; tampering is still refused.

ADR 00NN (source verification cache): verification-only sweeps skip what the same store
instance already read back and hashed, so the reads of one ingestion run grow with pages plus
objects instead of pages times objects. Bytes a caller consumes are always re-read and hashed,
a new instance (every stage, scan and mount opens its own) verifies everything again, and
``verify_every_load`` / ``APP_VERIFY_EVERY_REQUEST`` restores verify-on-every-call.
"""

import json
import os
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.document_catalog import mount_document, scan_catalog
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import (
    DraftPublication,
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.common.evidence.configs import get_settings
from ragspine.extraction.evidence.document.models import (
    DocumentManifest,
    PageRecord,
    RegionRecord,
    TextSidecar,
    TextSpan,
)
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    ingest_generic_semantics,
)
from tests.enterprise_pdf_rag.adapters.no_hard_link_helpers import forbid_hard_links


class _Reads:
    """Every ``Path.read_bytes`` of the test, by path."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: Counter[Path] = Counter()
        real = Path.read_bytes

        def read_bytes(path: Path) -> bytes:
            self.paths[path] += 1
            return real(path)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)

    def objects(self, store: LocalDocumentStore) -> int:
        folder = store.root / "objects" / "sha256"
        return sum(count for path, count in self.paths.items() if path.parent == folder)

    def clear(self) -> None:
        self.paths.clear()


def _source_snapshot(root: Path, *, pages: int = 4) -> tuple[LocalDocumentStore, str]:
    store = LocalDocumentStore(root, activate_on_publish=False)
    pdf = store.put(b"%PDF-1.7 source", media_type="application/pdf")
    records = []
    for index in range(pages):
        svg = store.put(
            f'<svg xmlns="http://www.w3.org/2000/svg" id="p{index}"/>'.encode(),
            media_type="image/svg+xml",
        )
        text = TextSidecar(
            "source-text-v1",
            pdf.sha256,
            index,
            (TextSpan(f"s{index}", f"Observed {index}", (1.0, 1.0, 2.0, 2.0)),),
        )
        text_ref = store.put(json.dumps(asdict(text)).encode(), media_type="application/json")
        records.append(PageRecord(index, 100.0, 100.0, 0, svg, text_ref, 1, ()))
    first = records[0]
    manifest_id = store.publish(
        DocumentManifest(
            "source-ingestion-v1",
            "test.pdf",
            pdf,
            "test",
            tuple(records),
            RegionRecord(0, (0.0, 0.0, 100.0, 100.0), first.svg, first.svg, first.text, ()),
        )
    )
    return store, manifest_id


@pytest.fixture
def audit_setting(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_VERIFY_EVERY_REQUEST", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("APP_VERIFY_EVERY_REQUEST")
    get_settings.cache_clear()


def test_one_instance_verifies_a_snapshot_once_and_then_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest_id = _source_snapshot(tmp_path, pages=4)
    reads = _Reads(monkeypatch)
    store = LocalDocumentStore(tmp_path, activate_on_publish=False)

    first = store.load(manifest_id)
    # The manifest, the PDF and every page's SVG and text: the full sweep, exactly once each.
    assert reads.objects(store) == 1 + 1 + 2 * 4
    assert max(reads.paths.values()) == 1

    reads.clear()
    for _ in range(5):
        assert store.load(manifest_id) == first
    assert reads.objects(store) == 0


def test_a_new_instance_refuses_an_asset_tampered_after_another_one_verified_it(
    tmp_path: Path,
) -> None:
    _, manifest_id = _source_snapshot(tmp_path)
    store = LocalDocumentStore(tmp_path, activate_on_publish=False)
    snapshot = store.load(manifest_id)
    page = snapshot.manifest.pages[2]
    store.asset_path(page.text).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="digest mismatch"):
        LocalDocumentStore(tmp_path, activate_on_publish=False).load(manifest_id)
    # Bytes a caller consumes are re-read and re-hashed even on the instance that verified them.
    with pytest.raises(ValueError, match="digest mismatch"):
        store.get(page.text)


def test_verify_every_load_rechecks_the_whole_snapshot_on_every_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest_id = _source_snapshot(tmp_path, pages=3)
    store = LocalDocumentStore(tmp_path, activate_on_publish=False, verify_every_load=True)
    reads = _Reads(monkeypatch)
    snapshot = store.load(manifest_id)
    store.load(manifest_id)
    # Every reference every time, the region's three re-references of page 1 included.
    assert reads.objects(store) == 2 * (1 + 1 + 2 * 3 + 3)

    store.asset_path(snapshot.manifest.pages[1].svg).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest mismatch"):
        store.load(manifest_id)


@pytest.mark.usefixtures("audit_setting")
def test_the_audit_setting_turns_the_cache_off_for_every_new_store(tmp_path: Path) -> None:
    _, manifest_id = _source_snapshot(tmp_path)
    store = LocalDocumentStore(tmp_path, activate_on_publish=False)
    snapshot = store.load(manifest_id)
    store.asset_path(snapshot.manifest.source).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="digest mismatch"):
        store.load(manifest_id)
    outputs = ProcessingStore(tmp_path / "processing")
    ref = outputs.assets.put(b"stage", media_type="application/json")
    outputs.assets.verify(ref)
    outputs.assets.asset_path(ref).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest mismatch"):
        outputs.assets.verify(ref)


def test_put_of_an_object_already_on_disk_reads_it_once_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_hard_links(monkeypatch)
    LocalDocumentStore(tmp_path).put(b"stage output", media_type="application/json")
    reads = _Reads(monkeypatch)
    syncs: list[int] = []
    real_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        syncs.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    store = LocalDocumentStore(tmp_path)

    ref = store.put(b"stage output", media_type="application/json")
    assert (reads.objects(store), syncs) == (1, [])
    store.put(b"stage output", media_type="application/json")
    assert (reads.objects(store), syncs) == (1, [])
    assert [path.name for path in (tmp_path / "objects" / "sha256").iterdir()] == [ref.sha256]


def test_put_still_refuses_a_corrupted_object_a_new_instance_finds_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_hard_links(monkeypatch)
    store = LocalDocumentStore(tmp_path)
    ref = store.put(b"stage output", media_type="application/json")
    store.asset_path(ref).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="digest mismatch"):
        store.put(b"stage output", media_type="application/json")
    with pytest.raises(ValueError, match="digest mismatch"):
        LocalDocumentStore(tmp_path).put(b"stage output", media_type="application/json")
    assert store.asset_path(ref).read_bytes() == b"tampered"


def _published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    page_count: int,
    table_page: bool = False,
    formula_page: bool = False,
) -> DraftPublication:
    work = tmp_path / f"p{page_count}"
    work.mkdir()
    ingest, _ = ingest_generic_semantics(
        work,
        monkeypatch,
        page_count=page_count,
        output_dir=work / "ingestion",
        table_page=table_page,
        formula_page=formula_page,
    )
    source_store = Path(ingest.source_store)
    processing_store = Path(ingest.processing_store)
    qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
    )
    indexed = index_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
        embedder=OfflineDescriptionEmbedder(),
    )
    return publish_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=indexed.indexed_processing_id,
    )


def _first_page_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, page_count: int
) -> tuple[int, int]:
    """Reads of page 1's SVG and text over ingest → qualify → index → publish → scan → mount."""
    with monkeypatch.context() as scoped:
        reads = _Reads(scoped)
        published = _published(tmp_path, scoped, page_count=page_count)
        catalog = scan_catalog(tmp_path / f"p{page_count}" / "ingestion")
        (entry,) = catalog.ready
        mount_document(entry, embedder=OfflineDescriptionEmbedder())
        counts = dict(reads.paths)
    store = LocalDocumentStore(Path(published.source_store), activate_on_publish=False)
    page = store.load(published.source_manifest_id).manifest.pages[0]
    return counts.get(store.asset_path(page.svg), 0), counts.get(store.asset_path(page.text), 0)


def test_one_page_is_read_as_often_however_many_other_pages_the_document_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every whole-snapshot sweep used to re-read every page once per object: O(pages * objects).

    Page 1 carries the same object in a two- and a six-page document, so its own reads must not
    depend on how many pages (and objects) follow it.
    """
    forbid_hard_links(monkeypatch)
    small = _first_page_reads(tmp_path, monkeypatch, page_count=2)
    large = _first_page_reads(tmp_path, monkeypatch, page_count=6)

    assert large == small
    assert small[0] > 0 and small[1] > 0


def test_publish_and_mount_each_refuse_a_page_tampered_after_the_run_verified_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = _published(tmp_path, monkeypatch, page_count=3)
    store = LocalDocumentStore(Path(published.source_store), activate_on_publish=False)
    page = store.load(published.source_manifest_id).manifest.pages[1]
    catalog = scan_catalog(tmp_path / "p3" / "ingestion")
    (entry,) = catalog.ready
    original = store.asset_path(page.svg).read_bytes()
    store.asset_path(page.svg).write_bytes(original + b"<!-- tampered -->")

    with pytest.raises(ValueError, match="digest mismatch"):
        publish_draft(
            source_store=Path(published.source_store),
            processing_store=Path(published.processing_store),
            processing_id=published.published_processing_id,
        )
    with pytest.raises(ValueError, match="digest mismatch"):
        mount_document(entry, embedder=OfflineDescriptionEmbedder())
    (rescanned,) = scan_catalog(tmp_path / "p3" / "ingestion").documents
    assert rescanned.retrieval_status == "corrupt"


def test_a_processing_store_rereads_its_manifest_object_on_every_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = _published(tmp_path, monkeypatch, page_count=2)
    outputs = ProcessingStore(Path(published.processing_store))
    outputs.load(published.published_processing_id)
    path = outputs.assets.content_path(published.published_processing_id)
    path.write_bytes(path.read_bytes().replace(b'"processing-v1"', b'"processing-v2"', 1))

    with pytest.raises(ValueError, match="digest mismatch"):
        outputs.load(published.published_processing_id)


# Pinned from the code before the verification cache (commit 7391ca4): the cache changes how
# often bytes are read, never which bytes are written, so every content id stays the same.
_PINNED_IDS: dict[str, object] = {
    "source_sha256": "7490981ffa6972d10f5a6dfde462f59d76ebd754adadce40ea7335b7a5391482",
    "source_manifest_id": "ac7c1bae3439b8aaf9295e7138d6ff3a8bad941aa4e9303871082bf6b29cb0ff",
    "published_processing_id": "06ac61386d25d0032f6ee617f6774da9b39b4e41fceff285a842b551b4dc2a3e",
    "retrieval_snapshot_id": "baf1e237eeda3eac13ba543918b05fc3d24ff58ffe1adf9c964663f04b2e4831",
    "member_count": 4,
}


def test_the_published_ids_are_the_ones_the_code_before_the_cache_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_hard_links(monkeypatch)
    published = _published(tmp_path, monkeypatch, page_count=3, table_page=True)
    ids = {
        "source_sha256": published.source_sha256,
        "source_manifest_id": published.source_manifest_id,
        "published_processing_id": published.published_processing_id,
        "retrieval_snapshot_id": published.retrieval_snapshot_id,
        "member_count": published.member_count,
    }
    assert ids == _PINNED_IDS
