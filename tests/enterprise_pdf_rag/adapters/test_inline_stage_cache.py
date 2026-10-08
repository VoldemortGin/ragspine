"""ADR 0029 Amendment 1: a stage-cache pointer carries its envelope inline; legacy pointers that
name an envelope object keep hitting, and both kinds heal and conflict exactly as before."""

import hashlib
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult, run_folder_pipeline
from enterprise_pdf_rag.adapters.http.processing_schemas import StageEnvelope
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.common.evidence.file_placement import recording_repairs, sharded_path
from ragspine.extraction.evidence.page.models import StageOutcome, StageState
from tests.enterprise_pdf_rag.adapters.legacy_pointer_helpers import write_pointer
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    FULL_PUBLISHED_ID,
    FULL_REQUESTS_DIGEST,
    lite_env,
    mixed_folder,
    mixed_pdf,
    run_mode,
    sharded_layout_only,
    store_digest,
    write_questions,
)
from tests.enterprise_pdf_rag.adapters.no_hard_link_helpers import forbid_hard_links

# 本文件钉的是 ADR 0029 Amendment 1 的**文件布局格式**本身(分代字节、分层路径、指针格式),所以固定
# 跑在 files 后端上;sqlite 后端的同一批语义(首写胜出 / 损坏即修 / 读穿旧代)由
# tests/enterprise_pdf_rag/object_backend/ 的一致性包与 test_sqlite_store_wiring.py 双跑钉死。
pytestmark = pytest.mark.usefixtures("files_object_backend")


FINGERPRINT = "e" * 64
# The full-mode store as every release before this amendment wrote it (fd302f8 .. 2825c57):
# the same files plus one envelope object per stage-cache pointer, each pointer naming its
# envelope by digest. Turning the new pointers back into that form must give these bytes.
LEGACY_FULL_STORE_DIGEST = "ddade1cd9c43b7ab7b28617874533fe3bf10b538b3aff556a608f80a9ba3caa2"
LEGACY_FULL_STORE_FILES = 815


def _envelope(outcome: StageOutcome) -> bytes:
    return StageEnvelope(outcome=outcome).model_dump_json().encode()


def _pointer(root: Path, fingerprint: str = FINGERPRINT) -> Path:
    return sharded_path(root / "stage-cache", fingerprint)


def _outcome(store: ProcessingStore, data: bytes = b"model result") -> StageOutcome:
    ref = store.assets.put(data, media_type="application/json")
    return StageOutcome("description", FINGERPRINT, StageState.SUCCEEDED, "prompt-v1", ref)


def _legacy_entry(root: Path, outcome: StageOutcome, *, flat: bool = False) -> Path:
    """Write a stage-cache entry exactly as the code before this amendment did."""
    digest = (
        LocalDocumentStore(root, activate_on_publish=False)
        .put(_envelope(outcome), media_type="application/json")
        .sha256
    )
    pointer = (
        root / "stage-cache" / outcome.input_fingerprint
        if flat
        else _pointer(root, outcome.input_fingerprint)
    )
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(digest + "\n")
    return pointer


def _envelope_objects(root: Path) -> int:
    """Objects of a store (either layout) that are a ``StageEnvelope``."""
    store = LocalDocumentStore(root, activate_on_publish=False)
    count = 0
    for digest in store.digests():
        try:
            StageEnvelope.model_validate_json(store.read_content(digest))
        except ValueError:
            continue
        count += 1
    return count


# ---- the new format ---------------------------------------------------------------------------


def test_a_new_entry_inlines_its_envelope_and_writes_no_envelope_object(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path)
    outcome = _outcome(store)
    assert outcome.artifact is not None

    store.cache(outcome)

    envelope = _envelope(outcome)
    digest = hashlib.sha256(envelope).hexdigest()
    # First line: the digest the envelope object used to be named by; then the envelope itself.
    assert _pointer(tmp_path).read_bytes() == digest.encode() + b"\n" + envelope + b"\n"
    assert store.assets.digests() == [outcome.artifact.sha256]
    assert ProcessingStore(tmp_path).cached(FINGERPRINT) == outcome


def test_recaching_an_intact_entry_rewrites_nothing(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path)
    outcome = _outcome(store)
    store.cache(outcome)
    written = _pointer(tmp_path).read_bytes()

    ProcessingStore(tmp_path).cache(outcome)

    assert _pointer(tmp_path).read_bytes() == written
    assert [path.name for path in _pointer(tmp_path).parent.iterdir()] == [FINGERPRINT]


# ---- legacy pointers --------------------------------------------------------------------------


@pytest.mark.parametrize("flat", [False, True], ids=["sharded", "flat"])
def test_a_legacy_pointer_still_hits_and_is_never_rewritten(tmp_path: Path, flat: bool) -> None:
    outcome = _outcome(ProcessingStore(tmp_path))
    pointer = _legacy_entry(tmp_path, outcome, flat=flat)
    written = pointer.read_bytes()
    objects = ProcessingStore(tmp_path).assets.digests()

    with recording_repairs() as repairs:
        assert ProcessingStore(tmp_path).cached(FINGERPRINT) == outcome
        ProcessingStore(tmp_path).cache(outcome)

    assert pointer.read_bytes() == written
    assert ProcessingStore(tmp_path).assets.digests() == objects
    assert _pointer(tmp_path).exists() is not flat
    assert repairs == {}


def test_a_store_of_both_formats_hits_every_entry(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path)
    legacy = _outcome(store, b"legacy result")
    _legacy_entry(tmp_path, legacy)
    ref = store.assets.put(b"new result", media_type="application/json")
    new = StageOutcome("description", "f" * 64, StageState.SUCCEEDED, "prompt-v1", ref)
    store.cache(new)

    reopened = ProcessingStore(tmp_path)
    assert (reopened.cached(FINGERPRINT), reopened.cached("f" * 64)) == (legacy, new)
    assert _envelope_objects(tmp_path) == 1


# ---- self-healing (ADR 0029 section 3) --------------------------------------------------------


def _damage(pointer: Path, store: ProcessingStore, outcome: StageOutcome, damage: str) -> None:
    data = pointer.read_bytes()
    if damage == "empty":
        pointer.write_bytes(b"")
    elif damage == "digest-line-only":
        pointer.write_bytes(data.partition(b"\n")[0] + b"\n")
    elif damage == "cut-in-half":
        pointer.write_bytes(data[: len(data) // 2])
    elif damage == "other-bytes":
        # Still valid JSON naming the same fingerprint, but not the bytes its digest names.
        pointer.write_bytes(data.replace(b"prompt-v1", b"prompt-v2"))
    elif damage == "not-a-digest":
        pointer.write_bytes(b"x" + data[1:])
    else:
        assert outcome.artifact is not None
        store.assets.asset_path(outcome.artifact).unlink()


@pytest.mark.parametrize(
    "damage",
    ["empty", "digest-line-only", "cut-in-half", "other-bytes", "not-a-digest", "artifact"],
)
def test_a_damaged_inline_entry_is_a_miss_and_recaching_repairs_it(
    tmp_path: Path, damage: str
) -> None:
    store = ProcessingStore(tmp_path)
    outcome = _outcome(store)
    store.cache(outcome)
    _damage(_pointer(tmp_path), store, outcome, damage)

    reopened = ProcessingStore(tmp_path)
    with recording_repairs() as repairs:
        assert reopened.cached(FINGERPRINT) is None
        assert reopened.cached(FINGERPRINT) is None
        reopened.assets.put(b"model result", media_type="application/json")
        reopened.cache(outcome)

    assert ProcessingStore(tmp_path).cached(FINGERPRINT) == outcome
    assert repairs["stage_cache"] == 1
    assert _envelope_objects(tmp_path) == 0


def test_a_legacy_entry_whose_envelope_is_lost_is_repaired_into_the_inline_format(
    tmp_path: Path,
) -> None:
    store = ProcessingStore(tmp_path)
    outcome = _outcome(store)
    pointer = _legacy_entry(tmp_path, outcome)
    store.assets.content_path(pointer.read_text().strip()).write_bytes(b"{")

    reopened = ProcessingStore(tmp_path)
    with recording_repairs() as repairs:
        assert reopened.cached(FINGERPRINT) is None
        reopened.cache(outcome)

    assert repairs["stage_cache"] == 1
    assert pointer.read_bytes().count(b"\n") == 2
    assert ProcessingStore(tmp_path).cached(FINGERPRINT) == outcome


def test_an_inline_envelope_bound_to_another_fingerprint_is_refused(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path)
    outcome = _outcome(store)
    envelope = _envelope(outcome)
    pointer = _pointer(tmp_path, "f" * 64)
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(hashlib.sha256(envelope).hexdigest().encode() + b"\n" + envelope + b"\n")

    with pytest.raises(ValueError, match="binding does not match"):
        ProcessingStore(tmp_path).cached("f" * 64)


@pytest.mark.parametrize("legacy", [False, True], ids=["inline", "legacy"])
def test_an_intact_entry_naming_another_outcome_still_conflicts(
    tmp_path: Path, legacy: bool
) -> None:
    store = ProcessingStore(tmp_path)
    outcome = _outcome(store)
    if legacy:
        _legacy_entry(tmp_path, outcome)
    else:
        store.cache(outcome)
    other = store.assets.put(b"another result", media_type="application/json")

    with pytest.raises(ValueError, match="another actual output"):
        store.cache(StageOutcome("description", FINGERPRINT, StageState.SUCCEEDED, "p", other))


# ---- first writer wins (ADR 0020, with and without hard links) --------------------------------


@pytest.mark.parametrize("hard_links", [True, False], ids=["link", "no-link"])
@pytest.mark.parametrize("legacy", [False, True], ids=["inline", "legacy"])
def test_a_rival_pointer_conflicts_and_the_same_entry_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hard_links: bool, legacy: bool
) -> None:
    if not hard_links:
        forbid_hard_links(monkeypatch)
    store = ProcessingStore(tmp_path)
    outcome = _outcome(store)
    if legacy:
        _legacy_entry(tmp_path, outcome)
    else:
        store.cache(outcome)
    pointer = _pointer(tmp_path)
    written = pointer.read_bytes()
    envelope = _envelope(outcome)

    # A concurrent writer of the very same entry (either format) is not a conflict ...
    write_pointer(pointer, hashlib.sha256(envelope).hexdigest(), immutable=True, inline=envelope)
    # ... a writer of another envelope is, and the first writer's bytes stay.
    rival = envelope.replace(b"prompt-v1", b"prompt-v2")
    with pytest.raises(ValueError, match="Conflicting immutable stage cache entry"):
        write_pointer(pointer, hashlib.sha256(rival).hexdigest(), immutable=True, inline=rival)

    assert pointer.read_bytes() == written
    assert [path.name for path in pointer.parent.iterdir()] == [FINGERPRINT]


# ---- end to end ---------------------------------------------------------------------------------


def _legacify(root: Path, *, every: int = 1) -> dict[Path, bytes]:
    """Turn every ``every``-th inline pointer under ``root`` into the pre-amendment form:
    its envelope written as an object, the pointer naming it by digest. Returns their bytes.
    An output carried inline (Amendment 2) is written back as its object first."""
    legacy: dict[Path, bytes] = {}
    pointers = sorted(root.rglob("stage-cache-sharded/*/*"))
    for index, pointer in enumerate(pointers):
        head, _, inline = pointer.read_bytes().partition(b"\n")
        if index % every or not inline:
            continue
        store_root = pointer.parents[2]
        envelope, _, output = inline.partition(b"\n")
        if output:
            artifact = StageEnvelope.model_validate_json(envelope).outcome.artifact
            assert artifact is not None
            assert (
                LocalDocumentStore(store_root, activate_on_publish=False).put(
                    output, media_type=artifact.media_type
                )
                == artifact
            )
        ref = LocalDocumentStore(store_root, activate_on_publish=False).put(
            envelope, media_type="application/json"
        )
        assert ref.sha256 == head.decode()
        pointer.write_text(ref.sha256 + "\n")
        legacy[pointer] = pointer.read_bytes()
    return legacy


def _content_files(root: Path) -> int:
    """Every file but the verification receipts (ADR 0034: stats, not content)."""
    return sum(
        1
        for path in root.rglob("*")
        if path.is_file() and path.parent.name != "verification-receipts"
    )


def _stores(root: Path) -> list[Path]:
    return sorted(path.parent for path in root.glob("*/*/stage-cache-sharded"))


def _run(tmp_path: Path) -> FolderPipelineResult:
    return run_folder_pipeline(
        tmp_path / "pdfs",
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
    )


def test_the_full_store_is_the_legacy_store_without_its_envelope_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    (document,) = _run(tmp_path).documents
    assert document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    root = tmp_path / "ingestion"
    assert sum(_envelope_objects(store) for store in _stores(root)) == 0
    pointers = len(list(root.rglob("stage-cache-sharded/*/*")))

    legacy = _legacify(root)

    # Same files, same bytes as every earlier release, once each envelope is put back.
    digest, count, requests = store_digest(root)
    contexts = sum(1 for _ in root.rglob("contexts/*.json"))
    assert (digest, count - contexts, requests) == (
        LEGACY_FULL_STORE_DIGEST,
        LEGACY_FULL_STORE_FILES,
        FULL_REQUESTS_DIGEST,
    )
    assert len(legacy) == pointers == sum(_envelope_objects(store) for store in _stores(root))


def test_a_store_of_both_formats_reruns_with_no_call_publishes_and_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    questions = write_questions(tmp_path)
    first = run_mode(tmp_path, "lite", questions=questions)
    (published,) = first.documents
    assert published.publication is not None and first.eval is not None
    legacy = _legacify(tmp_path / "ingestion", every=2)
    assert legacy
    tasks.clear()

    again = run_mode(tmp_path, "lite", questions=questions)

    (document,) = again.documents
    assert document.status == "published" and document.publication is not None
    assert (
        document.publication.published_processing_id
        == published.publication.published_processing_id
    )
    assert (sum(tasks.values()), document.live_calls, document.storage_repairs) == (0, 0, {})
    assert {pointer: pointer.read_bytes() for pointer in legacy} == legacy
    assert again.eval is not None
    assert [(case.case_id, case.verdict) for case in again.eval.cases] == [
        (case.case_id, case.verdict) for case in first.eval.cases
    ]
    assert all(case.verdict == "answered" for case in again.eval.cases)


def test_a_second_pdf_beside_a_legacy_store_writes_no_envelope_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _run(tmp_path)
    _legacify(tmp_path / "ingestion")
    old_stores = _stores(tmp_path / "ingestion")
    mixed_pdf(tmp_path / "pdfs" / "second.pdf", kinds=("text", "chart", "ruled", "text"))

    result = _run(tmp_path)

    assert {document.status for document in result.documents} == {"published"}
    new_stores = [store for store in _stores(tmp_path / "ingestion") if store not in old_stores]
    assert len(new_stores) == 2  # the second PDF's source and processing stores
    assert [_envelope_objects(store) for store in new_stores] == [0, 0]
    assert sharded_layout_only(tmp_path / "ingestion")


def test_the_seven_page_lite_ingest_writes_a_third_fewer_stage_cache_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured on this report before the amendment: 120 pointers + 120 envelope objects +
    114 outputs = 354 stage-cache files, 418 files in all. Now the envelopes are gone."""
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    run_mode(tmp_path, "lite")
    root = tmp_path / "ingestion"

    pointers = len(list(root.rglob("stage-cache-sharded/*/*")))
    objects = len(list(root.rglob("sha256-sharded/*/*")))
    files = _content_files(root)

    assert sum(_envelope_objects(store) for store in _stores(root)) == 0
    assert pointers == 120
    assert objects <= 266 - 120
    assert files <= 418 - 120
    _legacify(root)
    # Back to exactly the pre-amendment store: its envelope objects and (Amendment 2) its
    # output objects return.
    assert _content_files(root) == 418
