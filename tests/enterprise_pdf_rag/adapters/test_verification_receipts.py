"""A full verification is persisted as a receipt and reused across processes; tampering is refused.

ADR 0034 (persisted verification receipts): after a store has statted, read and hashed every
file of one immutable snapshot itself, it records a receipt beside the objects; a later store
instance (another stage, another process) that finds the receipt intact and every file's size,
mtime and ctime unchanged skips re-reading the snapshot. Anything else — a stat that moved, a
damaged receipt, another manifest or file set, the switch off — falls back to the ADR 0024 full
verification. The final publish, and every byte a caller consumes, still reads for real.
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.document_catalog import mount_document, scan_catalog
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore, manifest_assets
from enterprise_pdf_rag.adapters.draft_publication import publish_draft
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.verification_receipt import (
    RECEIPTS_DIRECTORY,
    encode_receipt,
    file_state,
    receipt_path,
)
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.file_placement import sharded_path
from ragspine.extraction.evidence.document.models import AssetRef
from tests.enterprise_pdf_rag.adapters.no_hard_link_helpers import forbid_hard_links
from tests.enterprise_pdf_rag.adapters.test_source_verification_cache import (
    _published,
    _Reads,
    _source_snapshot,
)

# The manifest object is consumed (always read); everything else is the sweep.
_SWEEP = 1 + 2 * 4  # the PDF plus every page's SVG and text, for the four-page snapshot


def _fresh(root: Path, **options: bool) -> LocalDocumentStore:
    return LocalDocumentStore(root, activate_on_publish=False, **options)  # type: ignore[arg-type]


def _recorded(root: Path) -> str:
    """A four-page snapshot whose receipt a reader (not the writer) has recorded."""
    _, manifest_id = _source_snapshot(root)
    _fresh(root).load(manifest_id)
    assert receipt_path(root, manifest_id).is_file()
    return manifest_id


def _forge_receipt(store: LocalDocumentStore, subject: str, refs: tuple[AssetRef, ...]) -> None:
    """A receipt recording the files as they are now without hashing them: what a forger writes."""
    unique = {ref.sha256: ref for ref in refs}
    states = [
        file_state(store.root, store.content_path(digest), ref) for digest, ref in unique.items()
    ]
    receipt_path(store.root, subject).write_bytes(encode_receipt(subject, refs, states))


def _rewrite_same_size(path: Path) -> bytes:
    """Other bytes of the same length; the mtime moves forward by a whole second. The last byte
    changes, so an output carried inline at the end of a stage-cache pointer changes too."""
    original = path.read_bytes()
    forged = original[:-1] + bytes([original[-1] ^ 0x01])
    before = path.stat()
    path.write_bytes(forged)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    return forged


@pytest.fixture
def receipts_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_VERIFY_PERSISTED_RECEIPTS", "false")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("APP_VERIFY_PERSISTED_RECEIPTS")
    get_settings.cache_clear()


@pytest.fixture
def audit_setting(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_VERIFY_EVERY_REQUEST", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("APP_VERIFY_EVERY_REQUEST")
    get_settings.cache_clear()


def test_a_reader_records_a_receipt_and_a_new_instance_reads_only_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest_id = _source_snapshot(tmp_path)
    reads = _Reads(monkeypatch)

    first = _fresh(tmp_path).load(manifest_id)
    assert reads.objects(_fresh(tmp_path)) == 1 + _SWEEP
    assert receipt_path(tmp_path, manifest_id).is_file()
    # The receipt lives beside the objects, never among the content-addressed ones.
    assert receipt_path(tmp_path, manifest_id).parent.name == RECEIPTS_DIRECTORY
    assert not any(
        manifest_id + suffix in path.name
        for path in (tmp_path / "objects").rglob("*")
        for suffix in (".receipt", "-receipt")
    )

    reads.clear()
    for _ in range(3):
        assert _fresh(tmp_path).load(manifest_id) == first
    # Each new instance re-reads and re-hashes the manifest it returns, and nothing else.
    assert reads.objects(_fresh(tmp_path)) == 3


def test_reusing_a_receipt_costs_one_stat_per_object_not_yet_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id = _recorded(tmp_path)
    objects = tmp_path / "objects"
    stats: list[Path] = []
    real = Path.stat

    def stat(path: Path, **kwargs: bool) -> os.stat_result:
        if objects in path.parents:
            stats.append(path)
        return real(path, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    _fresh(tmp_path).load(manifest_id)
    assert len(stats) == _SWEEP
    assert len(set(stats)) == _SWEEP


def test_the_instance_that_wrote_a_snapshot_records_no_receipt(tmp_path: Path) -> None:
    store, manifest_id = _source_snapshot(tmp_path)
    store.load(manifest_id)

    assert not receipt_path(tmp_path, manifest_id).exists()


def test_a_second_process_reuses_the_receipt_without_reading_the_snapshot(
    tmp_path: Path,
) -> None:
    manifest_id = _recorded(tmp_path)
    script = f"""
import json
from collections import Counter
from pathlib import Path
reads = Counter()
real = Path.read_bytes
def read_bytes(path):
    if str(path).startswith({str(tmp_path)!r}):
        reads[path.parent.name] += 1
    return real(path)
Path.read_bytes = read_bytes
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore, manifest_assets
store = LocalDocumentStore(Path({str(tmp_path)!r}), activate_on_publish=False)
snapshot = store.load({manifest_id!r})
print(json.dumps({{"pages": len(snapshot.manifest.pages), "reads": sum(reads.values())}}))
"""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("APP_VERIFY")
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    # The manifest object and the receipt: two reads for a whole four-page snapshot.
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"pages": 4, "reads": 2}


def test_a_page_rewritten_with_another_length_is_refused(tmp_path: Path) -> None:
    manifest_id = _recorded(tmp_path)
    store = _fresh(tmp_path)
    page = store.load(manifest_id).manifest.pages[1]
    path = store.asset_path(page.svg)
    path.write_bytes(path.read_bytes() + b"<!-- tampered -->")

    with pytest.raises(ValueError, match="digest mismatch"):
        _fresh(tmp_path).load(manifest_id)


def test_a_page_rewritten_with_the_same_length_and_a_new_mtime_is_refused(
    tmp_path: Path,
) -> None:
    manifest_id = _recorded(tmp_path)
    store = _fresh(tmp_path)
    _rewrite_same_size(store.asset_path(store.load(manifest_id).manifest.pages[2].text))

    with pytest.raises(ValueError, match="digest mismatch"):
        _fresh(tmp_path).load(manifest_id)


def test_a_rewrite_that_restores_size_and_mtime_still_moves_the_ctime(tmp_path: Path) -> None:
    manifest_id = _recorded(tmp_path)
    store = _fresh(tmp_path)
    path = store.asset_path(store.load(manifest_id).manifest.source)
    before = path.stat()
    time.sleep(0.01)
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 0x01]) + original[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    with pytest.raises(ValueError, match="digest mismatch"):
        _fresh(tmp_path).load(manifest_id)


def test_a_missing_object_is_refused(tmp_path: Path) -> None:
    manifest_id = _recorded(tmp_path)
    store = _fresh(tmp_path)
    store.asset_path(store.load(manifest_id).manifest.pages[0].svg).unlink()

    with pytest.raises(FileNotFoundError):
        _fresh(tmp_path).load(manifest_id)


@pytest.mark.parametrize(
    "damage",
    [
        lambda data: b"",
        lambda data: data[: len(data) // 2],
        lambda data: data.replace(b'"size"', b'"sise"', 1),
        lambda data: b"0" * 64 + data[64:],
        lambda data: b"not a receipt",
    ],
    ids=["empty", "truncated", "edited-body", "edited-digest", "garbage"],
)
def test_a_damaged_receipt_is_ignored_and_recorded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: object
) -> None:
    manifest_id = _recorded(tmp_path)
    path = receipt_path(tmp_path, manifest_id)
    path.write_bytes(damage(path.read_bytes()))  # type: ignore[operator]
    reads = _Reads(monkeypatch)

    _fresh(tmp_path).load(manifest_id)
    assert reads.objects(_fresh(tmp_path)) == 1 + _SWEEP
    reads.clear()
    _fresh(tmp_path).load(manifest_id)
    assert reads.objects(_fresh(tmp_path)) == 1


def test_a_receipt_of_another_manifest_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id = _recorded(tmp_path)
    other = tmp_path / "other"
    _, other_id = _source_snapshot(other, pages=2)
    _fresh(other).load(other_id)
    # Copied under this manifest's name: intact, but it vouches for another manifest's files.
    receipt_path(tmp_path, manifest_id).write_bytes(receipt_path(other, other_id).read_bytes())
    reads = _Reads(monkeypatch)

    _fresh(tmp_path).load(manifest_id)
    assert reads.objects(_fresh(tmp_path)) == 1 + _SWEEP


def test_a_receipt_for_a_different_file_set_is_not_reused(tmp_path: Path) -> None:
    manifest_id = _recorded(tmp_path)
    store = _fresh(tmp_path)
    snapshot = store.load(manifest_id)
    refs = (snapshot.manifest.source, snapshot.manifest.pages[0].svg)
    # A self-consistent receipt for this subject that covers two files only.
    _forge_receipt(store, manifest_id, refs)
    tampered = store.asset_path(snapshot.manifest.pages[3].text)
    tampered.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="digest mismatch"):
        _fresh(tmp_path).load(manifest_id)


def test_a_tampered_manifest_object_is_refused_whatever_its_receipt_says(
    tmp_path: Path,
) -> None:
    manifest_id = _recorded(tmp_path)
    path = _fresh(tmp_path).content_path(manifest_id)
    path.write_bytes(path.read_bytes().replace(b"test.pdf", b"evil.pdf"))

    with pytest.raises(ValueError, match="digest mismatch"):
        _fresh(tmp_path).load(manifest_id)


@pytest.mark.usefixtures("receipts_off")
def test_the_switch_off_restores_a_full_verification_per_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest_id = _source_snapshot(tmp_path)
    reads = _Reads(monkeypatch)
    for _ in range(2):
        _fresh(tmp_path).load(manifest_id)
    assert reads.objects(_fresh(tmp_path)) == 2 * (1 + _SWEEP)
    assert not (tmp_path / RECEIPTS_DIRECTORY).exists()


def test_the_switch_off_ignores_an_existing_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id = _recorded(tmp_path)
    reads = _Reads(monkeypatch)
    _fresh(tmp_path, persisted_receipts=False).load(manifest_id)
    assert reads.objects(_fresh(tmp_path)) == 1 + _SWEEP


@pytest.mark.usefixtures("audit_setting")
def test_the_audit_setting_verifies_every_call_and_ignores_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest_id = _source_snapshot(tmp_path)
    _fresh(tmp_path, verify_every_load=False).load(manifest_id)
    assert receipt_path(tmp_path, manifest_id).is_file()
    reads = _Reads(monkeypatch)
    store = _fresh(tmp_path)
    store.load(manifest_id)
    store.load(manifest_id)
    # Every reference every call, the region's three re-references of page 1 included.
    assert reads.objects(store) == 2 * (1 + _SWEEP + 3)


def test_a_repaired_object_invalidates_the_receipt_until_a_reader_records_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id = _recorded(tmp_path)
    store = _fresh(tmp_path)
    page = store.load(manifest_id).manifest.pages[1]
    path = store.asset_path(page.svg)
    original = path.read_bytes()
    path.write_bytes(original[:5])  # truncated: an asynchronous flush that failed

    with pytest.raises(ValueError, match="digest mismatch"):
        _fresh(tmp_path).load(manifest_id)
    # ADR 0029: a put brings the very bytes the digest names and rewrites the damaged copy.
    _fresh(tmp_path).put(original, media_type="image/svg+xml")
    reads = _Reads(monkeypatch)
    _fresh(tmp_path).load(manifest_id)
    assert reads.objects(store) == 1 + _SWEEP
    reads.clear()
    _fresh(tmp_path).load(manifest_id)
    assert reads.objects(store) == 1


def test_a_snapshot_in_the_legacy_flat_layout_is_recorded_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest_id = _source_snapshot(tmp_path)
    flat = tmp_path / "objects" / "sha256"
    flat.mkdir(parents=True, exist_ok=True)
    for path in list((tmp_path / "objects" / "sha256-sharded").rglob("*")):
        if path.is_file():
            path.rename(flat / path.name)
    _fresh(tmp_path).load(manifest_id)
    reads = _Reads(monkeypatch)
    snapshot = _fresh(tmp_path).load(manifest_id)
    # The manifest only: tried in the sharded place first (absent), then read from the flat one.
    assert reads.objects(_fresh(tmp_path)) == 2
    assert {path.name for path in reads.paths} == {manifest_id}

    _rewrite_same_size(flat / snapshot.manifest.pages[3].svg.sha256)
    with pytest.raises(ValueError, match="digest mismatch"):
        _fresh(tmp_path).load(manifest_id)


def test_a_sharded_copy_appearing_beside_a_recorded_flat_object_invalidates_the_receipt(
    tmp_path: Path,
) -> None:
    _, manifest_id = _source_snapshot(tmp_path)
    flat = tmp_path / "objects" / "sha256"
    flat.mkdir(parents=True, exist_ok=True)
    for path in list((tmp_path / "objects" / "sha256-sharded").rglob("*")):
        if path.is_file():
            path.rename(flat / path.name)
    snapshot = _fresh(tmp_path).load(manifest_id)
    # Reads look in the sharded place first, so a file there is what a reader would consume.
    digest = snapshot.manifest.pages[0].text.sha256
    shadow = sharded_path(flat, digest)
    shadow.parent.mkdir(parents=True, exist_ok=True)
    shadow.write_bytes(b"shadowing bytes")

    with pytest.raises(ValueError, match="digest mismatch"):
        _fresh(tmp_path).load(manifest_id)


def test_receipts_work_where_the_filesystem_cannot_hard_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_hard_links(monkeypatch)
    manifest_id = _recorded(tmp_path)
    reads = _Reads(monkeypatch)
    _fresh(tmp_path).load(manifest_id)
    assert reads.objects(_fresh(tmp_path)) == 1


def test_a_receipt_that_cannot_be_written_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest_id = _source_snapshot(tmp_path)
    (tmp_path / RECEIPTS_DIRECTORY).write_text("a file where the directory should be")

    snapshot = _fresh(tmp_path).load(manifest_id)
    assert len(snapshot.manifest.pages) == 4


def test_a_processing_store_reuses_its_receipt_for_the_asset_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = _published(tmp_path, monkeypatch, page_count=2)
    processing_id = published.published_processing_id
    ProcessingStore(Path(published.processing_store)).load(processing_id)
    assert receipt_path(Path(published.processing_store), processing_id).is_file()
    outputs = ProcessingStore(Path(published.processing_store))
    manifest = outputs.load(processing_id)
    canonical = outputs.assets.asset_path(manifest.pages[0].canonical.artifact)  # type: ignore[arg-type]
    # A small stage output lives inside its stage-cache pointer (ADR 0029 Amendment 2): the
    # receipt records and stats that pointer.
    assert canonical.parent.parent.name == "stage-cache-sharded"

    reads = _Reads(monkeypatch)
    ProcessingStore(Path(published.processing_store)).load(processing_id)
    # Canonical observations are only swept, never consumed by ``load``: a receipt skips them.
    assert reads.paths[canonical] == 0
    reads.clear()
    ProcessingStore(Path(published.processing_store), persisted_receipts=False).load(processing_id)
    assert reads.paths[canonical] == 1

    _rewrite_same_size(canonical)
    with pytest.raises(ValueError, match="digest mismatch"):
        ProcessingStore(Path(published.processing_store)).load(processing_id)


def test_scan_and_mount_reuse_receipts_but_never_write_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = _published(tmp_path, monkeypatch, page_count=3)
    receipts = [
        path
        for root in (Path(published.source_store), Path(published.processing_store))
        for path in (root / RECEIPTS_DIRECTORY).glob("*")
    ]
    for path in receipts:
        path.unlink()

    (entry,) = scan_catalog(tmp_path / "p3" / "ingestion").ready
    mount_document(entry, embedder=OfflineDescriptionEmbedder())
    assert not any(
        (root / RECEIPTS_DIRECTORY).exists() and any((root / RECEIPTS_DIRECTORY).iterdir())
        for root in (Path(published.source_store), Path(published.processing_store))
    )

    store = _fresh(Path(published.source_store))
    store.load(published.source_manifest_id)  # a pipeline stage records it
    pdf = store.asset_path(store.load(published.source_manifest_id).manifest.source)
    reads = _Reads(monkeypatch)
    (entry,) = scan_catalog(tmp_path / "p3" / "ingestion").ready
    mount_document(entry, embedder=OfflineDescriptionEmbedder())
    assert reads.paths[pdf] == 0


def test_publish_reads_every_byte_even_when_a_receipt_vouches_for_a_forged_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The threat-model boundary, pinned: a forger who rewrites a page *and* its receipt is
    accepted by a reader that trusts receipts, never by the publish that moves the pointers."""
    published = _published(tmp_path, monkeypatch, page_count=3)
    source_root = Path(published.source_store)
    store = _fresh(source_root)
    snapshot = store.load(published.source_manifest_id)
    _rewrite_same_size(store.asset_path(snapshot.manifest.pages[2].svg))
    _forge_receipt(store, published.source_manifest_id, manifest_assets(snapshot.manifest))
    assert _fresh(source_root).load(published.source_manifest_id) == snapshot

    with pytest.raises(ValueError, match="digest mismatch"):
        publish_draft(
            source_store=source_root,
            processing_store=Path(published.processing_store),
            processing_id=published.published_processing_id,
        )
