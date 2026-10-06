"""ADR 0029 end to end: a legacy flat store keeps working, a full one is never written again,
and entries an asynchronous flush lost are repaired on the next run."""

import shutil
from collections import Counter
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult, run_folder_pipeline
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from ragspine.common.evidence.file_placement import SHARDED_SUFFIX
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    FULL_PUBLISHED_ID,
    FULL_TASKS,
    lite_env,
    mixed_folder,
    sharded_layout_only,
)
from tests.enterprise_pdf_rag.adapters.test_sharded_layout import refuse_writes_under


def _run(tmp_path: Path, *, budget: int = 200) -> FolderPipelineResult:
    return run_folder_pipeline(
        tmp_path / "pdfs",
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=budget,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
    )


def _flatten(root: Path) -> list[Path]:
    """Move every sharded file back where the pre-ADR-0029 code wrote it; the flat dirs."""
    flats = []
    for sharded in sorted(root.rglob("*" + SHARDED_SUFFIX)):
        flat = sharded.with_name(sharded.name.removesuffix(SHARDED_SUFFIX))
        flat.mkdir(exist_ok=True)
        for shard in sharded.iterdir():
            for path in shard.iterdir():
                path.rename(flat / path.name)
        shutil.rmtree(sharded)
        flats.append(flat)
    return flats


def _files(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


def test_a_legacy_flat_store_reruns_with_no_call_and_no_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _run(tmp_path)
    flats = _flatten(tmp_path / "ingestion")
    assert len(flats) == 4  # objects/sha256 and stage-cache, of source and of processing
    before = {flat: _files(flat) for flat in flats}
    tasks.clear()

    (document,) = _run(tmp_path).documents

    assert document.status == "published" and document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert (sum(tasks.values()), document.live_calls, document.storage_repairs) == (0, 0, {})
    assert {flat: _files(flat) for flat in flats} == before
    assert not list((tmp_path / "ingestion").rglob("*" + SHARDED_SUFFIX))


def test_a_full_legacy_directory_is_never_written_and_the_document_still_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    (partial,) = _run(tmp_path, budget=6).documents
    assert partial.live_calls == 6
    flats = _flatten(tmp_path / "ingestion")
    for flat in flats:
        refuse_writes_under(monkeypatch, flat)
    before = {flat: _files(flat) for flat in flats}

    (document,) = _run(tmp_path).documents

    assert document.status == "published" and document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert sum(tasks.values()) == sum(FULL_TASKS.values())
    assert {flat: _files(flat) for flat in flats} == before
    assert list((tmp_path / "ingestion").rglob("*" + SHARDED_SUFFIX))


def _damage(root: Path) -> Counter[str]:
    """What a failed asynchronous flush leaves behind: some of the last files written are
    missing, empty or cut short, although every write had returned successfully."""
    damaged: Counter[str] = Counter()
    for index, path in enumerate(sorted(p for p in root.rglob("*") if p.is_file())):
        relative = path.relative_to(root).as_posix()
        if "/model-cache/contexts/" in relative or index % 9:
            continue
        if "/model-cache/responses/" in relative:
            path.unlink()
            damaged["response"] += 1
        elif SHARDED_SUFFIX in relative:
            path.write_bytes(path.read_bytes()[: len(path.read_bytes()) // 2])
            damaged["sharded"] += 1
    return damaged


def test_entries_lost_by_an_asynchronous_flush_are_repaired_on_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _run(tmp_path)
    damaged = _damage(tmp_path / "ingestion")
    assert damaged["response"] and damaged["sharded"] > 20
    tasks.clear()

    (document,) = _run(tmp_path).documents

    assert document.status == "published", document.error
    assert document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert sum(document.storage_repairs.values()) > 0
    # Only a lost response costs a call, and only one each; everything else is rebuilt.
    assert 0 < sum(tasks.values()) <= damaged["response"]
    assert document.storage_repairs.get("model_cache") == sum(tasks.values())
    assert sharded_layout_only(tmp_path / "ingestion")

    tasks.clear()
    (again,) = _run(tmp_path).documents
    assert (sum(tasks.values()), again.storage_repairs) == (0, {})
    assert again.publication is not None
    assert again.publication.published_processing_id == FULL_PUBLISHED_ID
