"""ADR 0029: hash-named files live in a sharded sibling; the legacy flat layout stays readable."""

import errno
import hashlib
import os
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.common.evidence.file_placement import (
    recording_repairs,
    sharded_path,
    stored_names,
)
from ragspine.extraction.evidence.page.models import StageOutcome, StageState
from tests.enterprise_pdf_rag.adapters.no_hard_link_helpers import forbid_hard_links


def _flat(root: Path) -> Path:
    return root / "objects" / "sha256"


def _legacy_put(root: Path, data: bytes) -> str:
    """Write an object exactly where the pre-ADR-0029 code put it."""
    digest = hashlib.sha256(data).hexdigest()
    _flat(root).mkdir(parents=True, exist_ok=True)
    (_flat(root) / digest).write_bytes(data)
    return digest


def refuse_writes_under(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    """The folder is full: creating or renaming anything into it fails like Workspace files."""
    real_open, real_replace, real_mkdir = os.open, os.replace, Path.mkdir

    def full(path: object) -> bool:
        return Path(str(path)).parent == folder or Path(str(path)) == folder / "never"

    def guarded_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if flags & os.O_CREAT and full(path):
            raise OSError(errno.EIO, "MAX_CHILD_NODE_SIZE_EXCEEDED")
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    def guarded_replace(source: object, target: object) -> None:
        if full(target):
            raise OSError(errno.EIO, "MAX_CHILD_NODE_SIZE_EXCEEDED")
        real_replace(source, target)  # type: ignore[arg-type]

    def guarded_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path.parent == folder and not path.exists():
            raise OSError(errno.EIO, "MAX_CHILD_NODE_SIZE_EXCEEDED")
        real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(os, "replace", guarded_replace)
    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)


def test_sharded_path_is_one_level_of_two_hex_digits_in_a_sibling_directory(
    tmp_path: Path,
) -> None:
    name = "ab" + "0" * 62
    assert sharded_path(_flat(tmp_path), name) == (
        tmp_path / "objects" / "sha256-sharded" / "ab" / name
    )
    with pytest.raises(ValueError):
        sharded_path(_flat(tmp_path), "ZZ" + "0" * 62)


def test_put_writes_only_the_sharded_layout(tmp_path: Path) -> None:
    store = LocalDocumentStore(tmp_path)
    ref = store.put(b"object", media_type="application/json")
    assert store.asset_path(ref) == sharded_path(_flat(tmp_path), ref.sha256)
    assert store.asset_path(ref).read_bytes() == b"object"
    assert not _flat(tmp_path).exists()
    assert store.digests() == [ref.sha256]


def test_a_legacy_object_is_read_and_not_written_again(tmp_path: Path) -> None:
    digest = _legacy_put(tmp_path, b"legacy")
    store = LocalDocumentStore(tmp_path)
    assert store.read_content(digest) == b"legacy"
    ref = store.put(b"legacy", media_type="application/json")
    assert ref.sha256 == digest
    assert LocalDocumentStore(tmp_path).put(b"legacy", media_type="x").sha256 == digest
    assert not (tmp_path / "objects" / "sha256-sharded").exists()
    assert store.asset_path(ref) == _flat(tmp_path) / digest


def test_both_layouts_absent_is_a_missing_object(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        LocalDocumentStore(tmp_path).read_content("a" * 64)


def test_enumeration_covers_both_layouts(tmp_path: Path) -> None:
    legacy = _legacy_put(tmp_path, b"legacy")
    store = LocalDocumentStore(tmp_path)
    new = store.put(b"new", media_type="x").sha256
    assert store.digests() == sorted({legacy, new})
    assert stored_names(_flat(tmp_path)) == sorted({legacy, new})


def test_a_full_legacy_directory_never_blocks_a_new_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_put(tmp_path, b"legacy")
    refuse_writes_under(monkeypatch, _flat(tmp_path))
    forbid_hard_links(monkeypatch)
    store = ProcessingStore(tmp_path)
    ref = store.assets.put(b"new object", media_type="application/json")
    outcome = StageOutcome("description", "e" * 64, StageState.SUCCEEDED, "p", ref)
    store.cache(outcome)
    assert ProcessingStore(tmp_path).cached("e" * 64) == outcome


@pytest.mark.parametrize("damage", ["empty", "truncated", "other", "missing-legacy"])
def test_put_with_the_content_repairs_a_damaged_object(tmp_path: Path, damage: str) -> None:
    data = b"stage output that was lost by an asynchronous flush"
    store = LocalDocumentStore(tmp_path)
    ref = store.put(data, media_type="application/json")
    path = store.asset_path(ref)
    if damage == "missing-legacy":
        path.unlink()
        _flat(tmp_path).mkdir(parents=True)
        (_flat(tmp_path) / ref.sha256).write_bytes(data[:5])
    else:
        path.write_bytes({"empty": b"", "truncated": data[:7], "other": b"x" * len(data)}[damage])
    reopened = LocalDocumentStore(tmp_path)
    with recording_repairs() as repairs:
        assert reopened.put(data, media_type="application/json") == ref
    assert reopened.get(ref) == data
    assert repairs == {"object": 1}


def test_put_repairs_without_hard_links_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_hard_links(monkeypatch)
    store = LocalDocumentStore(tmp_path)
    ref = store.put(b"content", media_type="x")
    store.asset_path(ref).write_bytes(b"")
    assert LocalDocumentStore(tmp_path).put(b"content", media_type="x") == ref
    assert store.asset_path(ref).read_bytes() == b"content"


def test_a_read_of_a_damaged_object_is_still_refused(tmp_path: Path) -> None:
    store = LocalDocumentStore(tmp_path)
    ref = store.put(b"content", media_type="x")
    store.asset_path(ref).write_bytes(b"conten")
    with pytest.raises(ValueError, match="digest mismatch"):
        LocalDocumentStore(tmp_path).get(ref)


def _cached_stage(tmp_path: Path) -> tuple[ProcessingStore, StageOutcome]:
    store = ProcessingStore(tmp_path)
    ref = store.assets.put(b"model result", media_type="application/json")
    outcome = StageOutcome("description", "e" * 64, StageState.SUCCEEDED, "p", ref)
    store.cache(outcome)
    return store, outcome


def test_stage_cache_is_sharded_and_a_legacy_pointer_still_hits(tmp_path: Path) -> None:
    _, outcome = _cached_stage(tmp_path)
    pointer = sharded_path(tmp_path / "stage-cache", "e" * 64)
    assert pointer.is_file() and not (tmp_path / "stage-cache").exists()
    legacy = tmp_path / "stage-cache" / ("e" * 64)
    legacy.parent.mkdir()
    pointer.rename(legacy)
    assert ProcessingStore(tmp_path).cached("e" * 64) == outcome
    ProcessingStore(tmp_path).cache(outcome)
    assert not pointer.exists()


@pytest.mark.parametrize("damage", ["pointer-empty", "envelope", "artifact"])
def test_a_damaged_stage_cache_entry_is_a_miss_and_recaching_repairs_it(
    tmp_path: Path, damage: str
) -> None:
    store, outcome = _cached_stage(tmp_path)
    pointer = sharded_path(tmp_path / "stage-cache", "e" * 64)
    if damage == "pointer-empty":
        pointer.write_text("")
    elif damage == "envelope":
        # The envelope sits inline after its digest (ADR 0029 Amendment 1): cut it short.
        pointer.write_bytes(pointer.read_bytes()[:-2])
    else:
        assert outcome.artifact is not None
        store.assets.asset_path(outcome.artifact).unlink()
    reopened = ProcessingStore(tmp_path)
    with recording_repairs() as repairs:
        assert reopened.cached("e" * 64) is None
        assert reopened.cached("e" * 64) is None
        reopened.assets.put(b"model result", media_type="application/json")
        reopened.cache(outcome)
    assert ProcessingStore(tmp_path).cached("e" * 64) == outcome
    assert repairs["stage_cache"] == 1


def test_a_valid_pointer_to_another_outcome_still_conflicts(tmp_path: Path) -> None:
    store, _ = _cached_stage(tmp_path)
    other = store.assets.put(b"another result", media_type="application/json")
    with pytest.raises(ValueError, match="another actual output"):
        store.cache(StageOutcome("description", "e" * 64, StageState.SUCCEEDED, "p", other))
