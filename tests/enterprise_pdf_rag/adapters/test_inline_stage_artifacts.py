"""ADR 0029 Amendment 2: a small stage output travels inside its stage-cache pointer, after the
envelope; it is still read by its digest everywhere, and the three pointer generations (digest
only, digest + envelope, digest + envelope + output) coexist, heal and conflict alike."""

import hashlib
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters import document_store
from enterprise_pdf_rag.adapters.document_store import INLINE_ARTIFACT_LIMIT, LocalDocumentStore
from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult, run_folder_pipeline
from enterprise_pdf_rag.adapters.http.processing_schemas import StageEnvelope
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.common.evidence.file_placement import recording_repairs, sharded_path
from ragspine.extraction.evidence.document.models import AssetRef
from ragspine.extraction.evidence.page.models import StageOutcome, StageState
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

FINGERPRINT = "e" * 64
PAYLOAD = b'{"model": "result"}'
# The full-mode store as ADR 0029 Amendment 1 wrote it (33d55c4 .. 3414e0c): every stage output
# an object, every pointer its envelope's digest line + the envelope. Turning the new pointers
# back into that form must give these bytes.
AMENDMENT_1_FULL_STORE_DIGEST = "620f220d255312e833b0b6e0f2242798ea24883cdfd59717eadc184361e26906"
AMENDMENT_1_FULL_STORE_FILES = 675


def _fresh_process() -> None:
    """Forget every inline location this process learnt, as a new process would."""
    document_store._INLINE_INDEXES.clear()


@pytest.fixture(autouse=True)
def _isolated_process() -> None:
    _fresh_process()


def _pointer(root: Path, fingerprint: str = FINGERPRINT) -> Path:
    return sharded_path(root / "stage-cache", fingerprint)


def _parts(data: bytes) -> tuple[bytes, bytes, bytes]:
    """Digest line, envelope, inline output (empty when absent) of a pointer."""
    head, envelope, output = [*data.split(b"\n", 2), b"", b""][:3]
    return head, envelope, output


def _ref_outcome(payload: bytes = PAYLOAD) -> StageOutcome:
    ref = AssetRef(hashlib.sha256(payload).hexdigest(), "application/json", len(payload))
    return StageOutcome("description", FINGERPRINT, StageState.SUCCEEDED, "prompt-v1", ref)


def _save(store: ProcessingStore, payload: bytes = PAYLOAD) -> StageOutcome:
    return store.cache_output("description", FINGERPRINT, "prompt-v1", payload)


def _envelope(outcome: StageOutcome) -> bytes:
    return StageEnvelope(outcome=outcome).model_dump_json().encode()


def _objects(root: Path) -> list[str]:
    return LocalDocumentStore(root, activate_on_publish=False).digests()


# ---- the new format ---------------------------------------------------------------------------


def test_a_small_output_is_written_inside_its_pointer_and_no_object_is_written(
    tmp_path: Path,
) -> None:
    outcome = _save(ProcessingStore(tmp_path))

    envelope = _envelope(outcome)
    digest = hashlib.sha256(envelope).hexdigest()
    assert outcome == _ref_outcome()
    # Digest line, envelope line, then the output's raw bytes (exactly its byte_length).
    assert _pointer(tmp_path).read_bytes() == digest.encode() + b"\n" + envelope + b"\n" + PAYLOAD
    assert _objects(tmp_path) == []
    assert not (tmp_path / "objects").exists()

    _fresh_process()
    assert ProcessingStore(tmp_path).cached(FINGERPRINT) == outcome


def test_an_inline_output_is_read_by_its_digest_on_every_path_of_a_new_process(
    tmp_path: Path,
) -> None:
    outcome = _save(ProcessingStore(tmp_path))
    assert outcome.artifact is not None
    ref = outcome.artifact

    def plain() -> LocalDocumentStore:
        return LocalDocumentStore(tmp_path, activate_on_publish=False)

    def processing() -> LocalDocumentStore:
        return ProcessingStore(tmp_path).assets

    for open_store in (plain, processing):
        _fresh_process()
        assert open_store().get(ref) == PAYLOAD
        _fresh_process()
        assert open_store().read_content(ref.sha256) == PAYLOAD
        _fresh_process()
        open_store().verify(ref)
        _fresh_process()
        assert open_store().content_path(ref.sha256) == _pointer(tmp_path)
        assert open_store().asset_path(ref) == _pointer(tmp_path)


def test_a_missing_digest_is_still_file_not_found(tmp_path: Path) -> None:
    _save(ProcessingStore(tmp_path))
    _fresh_process()

    with pytest.raises(FileNotFoundError):
        LocalDocumentStore(tmp_path, activate_on_publish=False).read_content("a" * 64)


@pytest.mark.parametrize(
    ("size", "inline"),
    [(1, True), (INLINE_ARTIFACT_LIMIT, True), (INLINE_ARTIFACT_LIMIT + 1, False), (0, False)],
    ids=["one-byte", "at-limit", "over-limit", "empty"],
)
def test_the_limit_decides_inline_or_object(tmp_path: Path, size: int, inline: bool) -> None:
    payload = b"x" * size
    store = ProcessingStore(tmp_path)

    outcome = _save(store, payload)

    assert outcome.artifact is not None
    lines = _pointer(tmp_path).read_bytes().split(b"\n", 2)
    if inline:
        assert lines[2] == payload
        assert _objects(tmp_path) == []
    else:
        # Exactly the Amendment 1 entry: the output an object, the pointer two lines.
        assert lines[2] == b""
        assert _objects(tmp_path) == [outcome.artifact.sha256]
    _fresh_process()
    reopened = ProcessingStore(tmp_path)
    assert reopened.cached(FINGERPRINT) == outcome
    assert reopened.assets.get(outcome.artifact) == payload


def test_resaving_an_intact_inline_entry_rewrites_nothing(tmp_path: Path) -> None:
    _save(ProcessingStore(tmp_path))
    written = _pointer(tmp_path).read_bytes()
    _fresh_process()

    with recording_repairs() as repairs:
        _save(ProcessingStore(tmp_path))

    assert _pointer(tmp_path).read_bytes() == written
    assert [path.name for path in _pointer(tmp_path).parent.iterdir()] == [FINGERPRINT]
    assert repairs == {}


def test_the_same_output_under_two_fingerprints_is_stored_twice(tmp_path: Path) -> None:
    """Inline outputs are not deduplicated across entries; each pointer is self-contained."""
    store = ProcessingStore(tmp_path)
    first = store.cache_output("description", "a" * 64, "p", PAYLOAD)
    second = store.cache_output("description", "b" * 64, "p", PAYLOAD)

    assert first.artifact == second.artifact
    for fingerprint in ("a" * 64, "b" * 64):
        assert _pointer(tmp_path, fingerprint).read_bytes().endswith(b"\n" + PAYLOAD)
    assert _objects(tmp_path) == []


# ---- three generations ----------------------------------------------------------------------


def _amendment_1_entry(root: Path, outcome: StageOutcome, payload: bytes) -> Path:
    """The entry ADR 0029 Amendment 1 wrote: output object + digest line + envelope."""
    LocalDocumentStore(root, activate_on_publish=False).put(payload, media_type="application/json")
    envelope = _envelope(outcome)
    pointer = _pointer(root, outcome.input_fingerprint)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_bytes(hashlib.sha256(envelope).hexdigest().encode() + b"\n" + envelope + b"\n")
    return pointer


def _digest_only_entry(
    root: Path, outcome: StageOutcome, payload: bytes, *, flat: bool = False
) -> Path:
    """The entry every release before Amendment 1 wrote: output and envelope objects."""
    store = LocalDocumentStore(root, activate_on_publish=False)
    store.put(payload, media_type="application/json")
    digest = store.put(_envelope(outcome), media_type="application/json").sha256
    pointer = (
        root / "stage-cache" / outcome.input_fingerprint
        if flat
        else _pointer(root, outcome.input_fingerprint)
    )
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(digest + "\n")
    return pointer


@pytest.mark.parametrize("generation", ["digest-only-flat", "digest-only", "amendment-1"], ids=str)
def test_an_earlier_entry_still_hits_and_is_never_rewritten(
    tmp_path: Path, generation: str
) -> None:
    outcome = _ref_outcome()
    if generation == "amendment-1":
        pointer = _amendment_1_entry(tmp_path, outcome, PAYLOAD)
    else:
        pointer = _digest_only_entry(
            tmp_path, outcome, PAYLOAD, flat=generation == "digest-only-flat"
        )
    written = pointer.read_bytes()
    objects = _objects(tmp_path)

    with recording_repairs() as repairs:
        assert ProcessingStore(tmp_path).cached(FINGERPRINT) == outcome
        assert _save(ProcessingStore(tmp_path)) == outcome

    assert pointer.read_bytes() == written
    assert _objects(tmp_path) == objects
    assert repairs == {}


def test_one_store_holding_all_three_generations_hits_every_entry(tmp_path: Path) -> None:
    payloads = {fp: f'{{"entry": "{fp[0]}"}}'.encode() for fp in ("a" * 64, "b" * 64, "c" * 64)}
    outcomes = {}
    for fingerprint, payload in payloads.items():
        ref = _ref_outcome(payload).artifact
        outcomes[fingerprint] = StageOutcome(
            "description", fingerprint, StageState.SUCCEEDED, "p", ref
        )
    _digest_only_entry(tmp_path, outcomes["a" * 64], payloads["a" * 64])
    _amendment_1_entry(tmp_path, outcomes["b" * 64], payloads["b" * 64])
    ProcessingStore(tmp_path).cache_output("description", "c" * 64, "p", payloads["c" * 64])
    _fresh_process()

    reopened = ProcessingStore(tmp_path)
    for fingerprint, outcome in outcomes.items():
        assert reopened.cached(fingerprint) == outcome
        assert outcome.artifact is not None
        assert reopened.assets.get(outcome.artifact) == payloads[fingerprint]


# ---- self-healing (ADR 0029 section 3) --------------------------------------------------------


def _damage(pointer: Path, damage: str) -> None:
    data = pointer.read_bytes()
    head, envelope, payload = data.split(b"\n", 2)
    if damage == "empty":
        pointer.write_bytes(b"")
    elif damage == "digest-line-only":
        pointer.write_bytes(head + b"\n")
    elif damage == "output-cut-off":
        # Looks exactly like an Amendment 1 pointer whose output object does not exist.
        pointer.write_bytes(head + b"\n" + envelope + b"\n")
    elif damage == "output-truncated":
        pointer.write_bytes(data[:-3])
    elif damage == "output-other-bytes":
        pointer.write_bytes(head + b"\n" + envelope + b"\n" + payload.replace(b"result", b"rasult"))
    elif damage == "output-extra-bytes":
        pointer.write_bytes(data + b"\n")
    elif damage == "cut-in-envelope":
        pointer.write_bytes(data[: len(head) + 20])
    else:
        assert damage == "not-a-digest"
        pointer.write_bytes(b"x" + data[1:])


DAMAGES = [
    "empty",
    "digest-line-only",
    "output-cut-off",
    "output-truncated",
    "output-other-bytes",
    "output-extra-bytes",
    "cut-in-envelope",
    "not-a-digest",
]


@pytest.mark.parametrize("damage", DAMAGES)
def test_a_damaged_inline_entry_is_a_miss_and_resaving_repairs_it(
    tmp_path: Path, damage: str
) -> None:
    outcome = _save(ProcessingStore(tmp_path))
    _damage(_pointer(tmp_path), damage)
    _fresh_process()

    reopened = ProcessingStore(tmp_path)
    with recording_repairs() as repairs:
        assert reopened.cached(FINGERPRINT) is None
        assert reopened.cached(FINGERPRINT) is None
        assert _save(reopened) == outcome

    _fresh_process()
    assert ProcessingStore(tmp_path).cached(FINGERPRINT) == outcome
    assert repairs["stage_cache"] == 1
    assert _objects(tmp_path) == []


@pytest.mark.parametrize("known", [True, False], ids=["located", "scanned"])
def test_inline_bytes_that_are_not_their_digest_are_never_served(
    tmp_path: Path, known: bool
) -> None:
    outcome = _save(ProcessingStore(tmp_path))
    assert outcome.artifact is not None
    _damage(_pointer(tmp_path), "output-other-bytes")
    if not known:
        _fresh_process()

    store = LocalDocumentStore(tmp_path, activate_on_publish=False)
    with pytest.raises((ValueError, FileNotFoundError)):
        store.get(outcome.artifact)


def test_an_intact_object_is_found_when_the_inline_copy_is_damaged(tmp_path: Path) -> None:
    outcome = _save(ProcessingStore(tmp_path))
    assert outcome.artifact is not None
    LocalDocumentStore(tmp_path, activate_on_publish=False).put(
        PAYLOAD, media_type="application/json"
    )
    _damage(_pointer(tmp_path), "output-truncated")

    assert LocalDocumentStore(tmp_path, activate_on_publish=False).get(outcome.artifact) == PAYLOAD


def test_an_inline_entry_naming_another_outcome_still_conflicts(tmp_path: Path) -> None:
    _save(ProcessingStore(tmp_path))

    with pytest.raises(ValueError, match="another actual output"):
        _save(ProcessingStore(tmp_path), b'{"model": "other"}')


def test_an_inline_envelope_bound_to_another_fingerprint_is_refused(tmp_path: Path) -> None:
    _save(ProcessingStore(tmp_path))
    pointer = _pointer(tmp_path, "f" * 64)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_bytes(_pointer(tmp_path).read_bytes())

    with pytest.raises(ValueError, match="binding does not match"):
        ProcessingStore(tmp_path).cached("f" * 64)


# ---- first writer wins (ADR 0020, with and without hard links) --------------------------------


@pytest.mark.parametrize("hard_links", [True, False], ids=["link", "no-link"])
@pytest.mark.parametrize("first", ["inline-output", "amendment-1"], ids=str)
def test_a_rival_pointer_conflicts_and_the_same_entry_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hard_links: bool, first: str
) -> None:
    if not hard_links:
        forbid_hard_links(monkeypatch)
    outcome = _ref_outcome()
    if first == "amendment-1":
        _amendment_1_entry(tmp_path, outcome, PAYLOAD)
    else:
        _save(ProcessingStore(tmp_path))
    pointer = _pointer(tmp_path)
    written = pointer.read_bytes()
    envelope = _envelope(outcome)
    digest = hashlib.sha256(envelope).hexdigest()

    # A concurrent writer of the very same entry, in any format, is not a conflict ...
    ProcessingStore._write_pointer(pointer, digest, immutable=True, inline=envelope)
    ProcessingStore._write_pointer(
        pointer, digest, immutable=True, inline=envelope, artifact=PAYLOAD
    )
    # ... a writer of another envelope is, and the first writer's bytes stay.
    rival = envelope.replace(b"prompt-v1", b"prompt-v2")
    with pytest.raises(ValueError, match="Conflicting immutable stage cache entry"):
        ProcessingStore._write_pointer(
            pointer,
            hashlib.sha256(rival).hexdigest(),
            immutable=True,
            inline=rival,
            artifact=PAYLOAD,
        )

    assert pointer.read_bytes() == written
    assert [path.name for path in pointer.parent.iterdir()] == [FINGERPRINT]


@pytest.mark.parametrize("hard_links", [True, False], ids=["link", "no-link"])
def test_a_damaged_inline_entry_is_replaced_without_hard_links_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hard_links: bool
) -> None:
    if not hard_links:
        forbid_hard_links(monkeypatch)
    outcome = _save(ProcessingStore(tmp_path))
    written = _pointer(tmp_path).read_bytes()
    _damage(_pointer(tmp_path), "output-truncated")
    _fresh_process()

    assert _save(ProcessingStore(tmp_path)) == outcome

    assert _pointer(tmp_path).read_bytes() == written


# ---- end to end ---------------------------------------------------------------------------------


def _deinline(root: Path, *, every: int = 1, offset: int = 0) -> dict[Path, bytes]:
    """Turn every ``every``-th pointer that carries its output into the Amendment 1 entry:
    the output written as an object, the pointer cut back to digest line + envelope."""
    changed: dict[Path, bytes] = {}
    for index, pointer in enumerate(sorted(root.rglob("stage-cache-sharded/*/*"))):
        head, envelope, payload = _parts(pointer.read_bytes())
        if (index - offset) % every or not payload:
            continue
        outcome = StageEnvelope.model_validate_json(envelope).outcome
        assert outcome.artifact is not None
        ref = LocalDocumentStore(pointer.parents[2], activate_on_publish=False).put(
            payload, media_type=outcome.artifact.media_type
        )
        assert ref == outcome.artifact
        pointer.write_bytes(head + b"\n" + envelope + b"\n")
        changed[pointer] = pointer.read_bytes()
    return changed


def _digest_only(root: Path, *, every: int = 1, offset: int = 0) -> dict[Path, bytes]:
    """Turn every ``every``-th Amendment 1 pointer into the pre-Amendment 1 digest-only one."""
    changed: dict[Path, bytes] = {}
    for index, pointer in enumerate(sorted(root.rglob("stage-cache-sharded/*/*"))):
        head, _, rest = pointer.read_bytes().partition(b"\n")
        envelope, _, payload = rest.partition(b"\n")
        if (index - offset) % every or not envelope or payload:
            continue
        ref = LocalDocumentStore(pointer.parents[2], activate_on_publish=False).put(
            envelope, media_type="application/json"
        )
        assert ref.sha256 == head.decode()
        pointer.write_text(ref.sha256 + "\n")
        changed[pointer] = pointer.read_bytes()
    return changed


def _stores(root: Path) -> list[Path]:
    return sorted(path.parent for path in root.glob("*/*/stage-cache-sharded"))


def _inline_pointers(root: Path) -> int:
    return sum(
        1
        for pointer in root.rglob("stage-cache-sharded/*/*")
        if len(pointer.read_bytes().split(b"\n", 2)) == 3
        and pointer.read_bytes().split(b"\n", 2)[2]
    )


def _artifact_objects(store: Path) -> set[str]:
    """Objects of a store that are the output some stage-cache entry of it names."""
    named = set()
    for pointer in store.rglob("stage-cache-sharded/*/*"):
        _, envelope, _ = _parts(pointer.read_bytes())
        if envelope:
            artifact = StageEnvelope.model_validate_json(envelope).outcome.artifact
            if artifact is not None:
                named.add(artifact.sha256)
    return named & set(_objects(store))


def _run(tmp_path: Path) -> FolderPipelineResult:
    return run_folder_pipeline(
        tmp_path / "pdfs",
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
    )


def test_the_full_store_is_the_amendment_1_store_with_its_outputs_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    (document,) = _run(tmp_path).documents
    assert document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    root = tmp_path / "ingestion"
    inline = _inline_pointers(root)
    assert inline > 0

    changed = _deinline(root)

    digest, count, requests = store_digest(root)
    contexts = sum(1 for _ in root.rglob("contexts/*.json"))
    assert (digest, count - contexts, requests) == (
        AMENDMENT_1_FULL_STORE_DIGEST,
        AMENDMENT_1_FULL_STORE_FILES,
        FULL_REQUESTS_DIGEST,
    )
    assert len(changed) == inline


@pytest.mark.parametrize("hard_links", [True, False], ids=["link", "no-link"])
def test_a_store_of_three_generations_reruns_with_no_call_publishes_and_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hard_links: bool
) -> None:
    if not hard_links:
        forbid_hard_links(monkeypatch)
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    questions = write_questions(tmp_path)
    first = run_mode(tmp_path, "lite", questions=questions)
    (published,) = first.documents
    assert published.publication is not None and first.eval is not None
    root = tmp_path / "ingestion"
    # A third stays inline, a third becomes Amendment 1, a third digest-only.
    _deinline(root, every=3, offset=1)
    _deinline(root, every=3, offset=2)
    _digest_only(root, every=3, offset=2)
    pointers = {pointer: pointer.read_bytes() for pointer in root.rglob("stage-cache-sharded/*/*")}
    generations = {len(data.split(b"\n", 2)[-1]) > 0 for data in pointers.values()}
    assert generations == {True, False}
    _fresh_process()
    tasks.clear()

    again = run_mode(tmp_path, "lite", questions=questions)

    (document,) = again.documents
    assert document.status == "published" and document.publication is not None
    assert (
        document.publication.published_processing_id
        == published.publication.published_processing_id
    )
    assert (sum(tasks.values()), document.live_calls, document.storage_repairs) == (0, 0, {})
    assert {pointer: pointer.read_bytes() for pointer in pointers} == pointers
    assert again.eval is not None
    assert [(case.case_id, case.verdict) for case in again.eval.cases] == [
        (case.case_id, case.verdict) for case in first.eval.cases
    ]
    assert all(case.verdict == "answered" for case in again.eval.cases)


def test_a_second_pdf_writes_no_small_stage_output_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _run(tmp_path)
    _deinline(tmp_path / "ingestion")
    old_stores = _stores(tmp_path / "ingestion")
    mixed_pdf(tmp_path / "pdfs" / "second.pdf", kinds=("text", "chart", "ruled", "text"))

    result = _run(tmp_path)

    assert {document.status for document in result.documents} == {"published"}
    new_stores = [store for store in _stores(tmp_path / "ingestion") if store not in old_stores]
    source, processing = sorted(new_stores, key=lambda store: store.name != "source")
    assert (source.name, processing.name) == ("source", "processing")
    assert _artifact_objects(processing) == set()
    # The source stage names the source manifest, which is addressed by its id everywhere.
    assert len(_artifact_objects(source)) == 1
    assert sharded_layout_only(tmp_path / "ingestion")


def test_the_seven_page_lite_ingest_writes_no_stage_output_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured on this report under Amendment 1: 120 pointers, 129 processing + 17 source
    objects, 298 files in all; 114 of those objects were outputs a pointer names. Now 16 + 17
    objects (only the source manifest is a stage output) and 185 files."""
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    run_mode(tmp_path, "lite")
    root = tmp_path / "ingestion"

    pointers = len(list(root.rglob("stage-cache-sharded/*/*")))
    objects = len(list(root.rglob("sha256-sharded/*/*")))
    # Verification receipts (ADR 0034) record file stats, not content: not counted here.
    files = sum(
        1
        for path in root.rglob("*")
        if path.is_file() and path.parent.name != "verification-receipts"
    )

    assert pointers == 120
    assert sum(len(_artifact_objects(store)) for store in _stores(root)) == 1
    assert objects <= 33
    assert files <= 185
    added = _deinline(root)
    assert sum(
        1
        for path in root.rglob("*")
        if path.is_file() and path.parent.name != "verification-receipts"
    ) - files <= len(added)
