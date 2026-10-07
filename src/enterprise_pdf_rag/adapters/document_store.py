"""Local content-addressed assets and immutable manifests; not a production CAS release service.

Verification cache (ADR 0024, source verification cache): a store instance remembers which
digests it has itself read back and hashed, and which source manifests it has fully verified.
A sweep whose only purpose is to verify (``verify``, ``load``, ``publish``, ``put`` of an object
already on disk) skips what this instance already verified; bytes a caller consumes (``get``,
``read_content``) are always read and hashed again. A new instance, and so a new process, starts
empty. ``verify_every_load`` (default: ``APP_VERIFY_EVERY_REQUEST``) turns the cache off.

Inline stage outputs (ADR 0029 Amendment 2): a stage output of at most
``INLINE_ARTIFACT_LIMIT`` bytes may live only inside its stage-cache pointer, after the envelope.
Every read by digest resolves it: a known inline location first, then the sharded and the flat
object, then a scan of this store's sharded stage cache for the pointers not read yet. A location
is only where to look — the bytes found there are hashed like any object's.

Persisted receipts (ADR 0034): a whole-snapshot sweep (``verify_snapshot``: ``load``, and
``ProcessingStore.load``'s asset sweep) that statted, read and hashed every file itself records
a receipt (``verification_receipt``); a later instance whose receipt still holds — intact, same
manifest and file set, every file's size / mtime / ctime unchanged — skips the sweep. Only those
sweeps use one: ``verify``, ``publish`` and ``put`` read for real. ``persisted_receipts``
(default: ``APP_VERIFY_PERSISTED_RECEIPTS``) turns them off, and so does ``verify_every_load``;
``record_receipts=False`` reads them but never writes one (the read-only catalog scan / mount).
"""

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

from enterprise_pdf_rag.adapters.http.document_schemas import ManifestEnvelope
from enterprise_pdf_rag.adapters.verification_receipt import (
    FileState,
    encode_receipt,
    inline_pointer,
    receipt_holds,
    state_of,
    write_receipt,
)
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.file_placement import (
    link_new_file,
    note_repair,
    read_stored,
    replace_file,
    sharded_directory,
    sharded_path,
    stored_names,
    stored_path,
)
from ragspine.extraction.evidence.document.models import (
    AssetRef,
    DocumentManifest,
    DocumentSnapshot,
)

_DIGEST = re.compile(r"[0-9a-f]{64}")

# A stage output of at most this many bytes is written inside its stage-cache pointer instead of
# as an object of its own (ADR 0029 Amendment 2). Measured on the synthetic reports every stage
# output is below 11 KiB (the source manifest, never inlined, grows with the page count); a real
# embedding of 1 024 to 3 072 dimensions is ≈ 20 to 60 KiB of JSON. Larger outputs stay objects.
INLINE_ARTIFACT_LIMIT = 64 * 1024


def split_stage_pointer(data: bytes) -> tuple[str, bytes | None, bytes | None]:
    """(envelope digest, envelope or None, inline output or None) of a stage-cache pointer.

    Three generations share one prefix: ``<digest>\\n`` (the envelope an object), then
    ``<envelope>\\n`` (Amendment 1), then the output's raw bytes (Amendment 2). The envelope is
    compact JSON, so it holds no raw newline; the output is everything after it. Raises
    ``ValueError`` when the first line is not a digest. Nothing here is verified.
    """
    head, _, rest = data.partition(b"\n")
    digest = head.strip().decode(errors="replace")
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError("damaged pointer")
    if not rest.strip():
        return digest, None, None
    envelope, _, output = rest.partition(b"\n")
    return digest, envelope, output or None


def _envelope_artifact(envelope: bytes) -> str | None:
    """The output digest a stage envelope names, if it parses and names one."""
    try:
        artifact = json.loads(envelope)["outcome"]["artifact"]
    except (ValueError, KeyError, TypeError):
        return None
    digest = artifact.get("sha256") if isinstance(artifact, dict) else None
    return digest if isinstance(digest, str) and _DIGEST.fullmatch(digest) else None


class _InlineIndex:
    """Where this process has seen inline outputs of one store: digest -> pointer name."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.where: dict[str, str] = {}
        # Pointer names already read by a scan and found intact; a damaged one is read again.
        self.settled: set[str] = set()


# Process-wide, so that every store instance of one root (each stage opens its own) shares what
# the others wrote or read. Locations only: never trusted, every read hashes its bytes.
_INLINE_INDEXES: dict[str, _InlineIndex] = {}
_INLINE_INDEXES_LOCK = threading.Lock()


def _inline_index(root: Path) -> _InlineIndex:
    key = os.path.abspath(root)
    with _INLINE_INDEXES_LOCK:
        index = _INLINE_INDEXES.get(key)
        if index is None:
            index = _INLINE_INDEXES[key] = _InlineIndex()
        return index


class LocalDocumentStore:
    def __init__(
        self,
        root: Path,
        *,
        activate_on_publish: bool = True,
        verify_every_load: bool | None = None,
        persisted_receipts: bool | None = None,
        record_receipts: bool = True,
    ) -> None:
        self.root = root
        self._activate_on_publish = activate_on_publish
        settings = get_settings()
        self._verify_every_load = (
            settings.verify_every_request if verify_every_load is None else verify_every_load
        )
        self._receipts = not self._verify_every_load and (
            settings.verify_persisted_receipts if persisted_receipts is None else persisted_receipts
        )
        self._record_receipts = record_receipts
        # digest -> byte length, for objects this instance read back and hashed itself. An
        # object it only wrote is not in here: the first sweep after a write reads it once.
        self._verified: dict[str, int] = {}
        # Objects this instance wrote: a sweep covering one records no receipt (ADR 0034).
        self._written: set[str] = set()
        # Snapshots whose persisted receipt held for this instance, and the objects it vouched
        # for (digest -> byte length); only ``verify(..., receipt=True)`` sweeps accept those.
        self._attested_subjects: set[str] = set()
        self._attested: dict[str, int] = {}
        self._snapshots: dict[str, DocumentSnapshot] = {}

    def auditing(self) -> "LocalDocumentStore":
        """The same store without the verification cache: every call re-verifies live bytes."""
        return LocalDocumentStore(
            self.root, activate_on_publish=self._activate_on_publish, verify_every_load=True
        )

    def asset_path(self, ref: AssetRef) -> Path:
        return self.content_path(ref.sha256)

    def _flat(self) -> Path:
        return self.root / "objects" / "sha256"

    def _object_path(self, digest: str) -> Path:
        """Where an object is written: the sharded layout (ADR 0029)."""
        if _DIGEST.fullmatch(digest) is None:
            raise ValueError("Invalid content-addressed artifact identifier")
        return sharded_path(self._flat(), digest)

    def digests(self) -> list[str]:
        """Every stored object's digest, in either layout (sorted)."""
        return [name for name in stored_names(self._flat()) if _DIGEST.fullmatch(name)]

    def put(self, data: bytes, *, media_type: str) -> AssetRef:
        """Store ``data`` under its digest. An object already there is read back and verified;
        a damaged one (missing, empty, truncated or other bytes — e.g. lost by an asynchronous
        flush) is rewritten with these bytes, which are by construction the bytes the name
        means (ADR 0029). Never writes into the legacy flat directory."""
        ref = AssetRef(hashlib.sha256(data).hexdigest(), media_type, len(data))
        if self._is_verified(ref):
            return ref
        target = self._object_path(ref.sha256)
        existing = stored_path(self._flat(), ref.sha256)
        if existing is not None and self._intact(existing, ref):
            return ref
        target.parent.mkdir(parents=True, exist_ok=True)
        self._written.add(ref.sha256)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            try:
                link_new_file(temporary, target)
            except FileExistsError:
                if self._intact(target, ref):
                    return ref
                replace_file(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        if existing is not None:
            note_repair("object")
        return ref

    def _intact(self, path: Path, ref: AssetRef) -> bool:
        """Does ``path`` hold exactly ``ref``'s bytes? Records the digest as verified if so."""
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return False
        if len(data) != ref.byte_length or hashlib.sha256(data).hexdigest() != ref.sha256:
            return False
        self._verified[ref.sha256] = len(data)
        return True

    def _read_digest(self, digest: str) -> bytes:
        """The bytes of ``digest`` wherever they are stored (see the module docstring)."""
        self._object_path(digest)
        index = _inline_index(self.root)
        data, damaged = self._inline_bytes(index, digest)
        if data is not None:
            return data
        try:
            data = self.read_object(digest)
        except FileNotFoundError:
            self._scan_inline(index)
            data, damaged_now = self._inline_bytes(index, digest)
            if data is not None:
                return data
            if damaged or damaged_now:
                raise ValueError(
                    "Stored artifact digest mismatch; source review is unavailable"
                ) from None
            raise
        return data

    def read_object(self, digest: str) -> bytes:
        """The object file of ``digest`` (sharded, then flat), hashed; no inline lookup."""
        self._object_path(digest)
        _, data = read_stored(self._flat(), digest)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Stored artifact digest mismatch; source review is unavailable")
        self._verified[digest] = len(data)
        return data

    def get_object(self, ref: AssetRef) -> bytes:
        """``get`` limited to the object files: for an entry that names an object, not a copy."""
        data = self.read_object(ref.sha256)
        if len(data) != ref.byte_length:
            raise ValueError("Stored artifact length mismatch")
        return data

    def _stage_cache_pointer(self, name: str) -> Path:
        return sharded_path(self.root / "stage-cache", name)

    def note_inline(self, digest: str, pointer_name: str) -> None:
        """Remember that the stage-cache pointer ``pointer_name`` carries ``digest`` inline."""
        index = _inline_index(self.root)
        with index.lock:
            index.where[digest] = pointer_name

    def note_inline_verified(self, digest: str, pointer_name: str, length: int) -> None:
        """As ``note_inline``, for bytes the caller has just read there and hashed."""
        self.note_inline(digest, pointer_name)
        self._verified[digest] = length

    def _inline_bytes(self, index: _InlineIndex, digest: str) -> tuple[bytes | None, bool]:
        """(the inline output of ``digest`` at its known location, whether one was there but
        did not hash to it)."""
        with index.lock:
            name = index.where.get(digest)
        if name is None:
            return None, False
        try:
            _, _, output = split_stage_pointer(self._stage_cache_pointer(name).read_bytes())
        except FileNotFoundError:
            return None, False
        except (OSError, ValueError):
            return None, True
        if output is None:
            return None, False
        if hashlib.sha256(output).hexdigest() != digest:
            return None, True
        self._verified[digest] = len(output)
        return output, False

    def _scan_inline(self, index: _InlineIndex) -> None:
        """Read the sharded stage-cache pointers no scan has settled and index their outputs.

        Only reached when a digest is in no known location and no object: a new process
        reading an inline output it has not met yet, or a missing object. Pointers already
        found intact are not read again, so a later scan costs one listing per shard.
        """
        directory = sharded_directory(self.root / "stage-cache")
        try:
            shards = [shard for shard in directory.iterdir() if shard.is_dir()]
        except FileNotFoundError:
            return
        for shard in shards:
            for pointer in shard.iterdir():
                name = pointer.name
                with index.lock:
                    if name in index.settled:
                        continue
                try:
                    digest, envelope, output = split_stage_pointer(pointer.read_bytes())
                except (OSError, ValueError):
                    continue
                named = None
                if envelope is not None:
                    if hashlib.sha256(envelope).hexdigest() != digest:
                        continue
                    named = _envelope_artifact(envelope)
                if output is not None:
                    actual = hashlib.sha256(output).hexdigest()
                    if actual != named:
                        continue
                    self._verified[actual] = len(output)
                with index.lock:
                    index.settled.add(name)
                    if output is not None and named is not None:
                        index.where[named] = name

    def get(self, ref: AssetRef) -> bytes:
        data = self._read_digest(ref.sha256)
        if len(data) != ref.byte_length:
            raise ValueError("Stored artifact length mismatch")
        return data

    def verify(self, ref: AssetRef, *, receipt: bool = False) -> None:
        """Check one object against its reference; skipped when this instance already did, and
        with ``receipt`` also when a persisted receipt vouched for it here (ADR 0034)."""
        if self._is_verified(ref) or (
            receipt and self._receipts and self._attested.get(ref.sha256) == ref.byte_length
        ):
            return
        self.get(ref)

    def _is_verified(self, ref: AssetRef) -> bool:
        return not self._verify_every_load and self._verified.get(ref.sha256) == ref.byte_length

    def verify_snapshot(self, subject: str, refs: Sequence[AssetRef]) -> None:
        """Verify every object one immutable snapshot names; ``subject`` is its manifest digest.

        Skipped for objects this instance already read back, then for the whole set when a
        persisted receipt still holds for the rest (ADR 0034): one receipt read and one ``stat``
        per object not yet read back; the inline locations it names seed this process's index.
        Otherwise every distinct object is located, statted, then read and hashed — the state
        taken *before* the read, so a receipt never vouches for a state later than the bytes it
        hashed — and, unless this instance wrote one of them, a receipt is recorded.
        """
        pending = [ref for ref in refs if not self._is_verified(ref)]
        if not pending:
            return
        if not self._receipts:
            for ref in pending:
                self.verify(ref)
            return
        if subject in self._attested_subjects:
            return
        pointers = self.root / "stage-cache"
        held = receipt_holds(
            self.root,
            self._flat(),
            pointers,
            subject,
            refs,
            unverified={ref.sha256 for ref in pending},
        )
        if held is not None:
            for state in held:
                name = inline_pointer(pointers, state)
                if name is not None:
                    self.note_inline(state.sha256, name)
            self._attested_subjects.add(subject)
            self._attested.update((ref.sha256, ref.byte_length) for ref in refs)
            return
        states: dict[str, FileState] = {}
        recordable = self._record_receipts
        for ref in refs:
            seen = states.get(ref.sha256)
            if seen is not None:
                if seen.byte_length != ref.byte_length:
                    raise ValueError("Stored artifact length mismatch")
                continue
            recorded = self._read_recorded(ref)
            if recorded is None:
                recordable = False
                self.get(ref)
                continue
            states[ref.sha256] = recorded
        if recordable and not self._written.intersection(states):
            write_receipt(self.root, subject, encode_receipt(subject, refs, tuple(states.values())))

    def _located(self, digest: str) -> tuple[Path, os.stat_result]:
        """The file ``digest`` is read from and its ``stat``, in ``_read_digest``'s order (a known
        inline location, the sharded object, the flat one, then a stage-cache scan): one ``stat``
        per place tried, no separate existence probe. ``FileNotFoundError`` when in none."""
        index = _inline_index(self.root)
        with index.lock:
            name = index.where.get(digest)
        places = [sharded_path(self._flat(), digest), self._flat() / digest]
        if name is not None:
            places.insert(0, self._stage_cache_pointer(name))
        for path in places:
            try:
                return path, path.stat()
            except FileNotFoundError:
                continue
        path = self.content_path(digest)
        return path, path.stat()

    def _read_recorded(self, ref: AssetRef) -> FileState | None:
        """Stat, then read and hash, the one file ``ref`` is read from (an object, or the
        stage-cache pointer carrying it inline). None when an inline location no longer holds
        those bytes: the caller then reads it the ordinary way and records nothing."""
        path, state = self._located(ref.sha256)
        data = path.read_bytes()
        recorded = state_of(self.root, path, ref, state)
        if inline_pointer(self.root / "stage-cache", recorded) is not None:
            try:
                data = split_stage_pointer(data)[2] or b""
            except ValueError:
                return None
            if hashlib.sha256(data).hexdigest() != ref.sha256:
                return None
        elif hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError("Stored artifact digest mismatch; source review is unavailable")
        if len(data) != ref.byte_length:
            raise ValueError("Stored artifact length mismatch")
        self._verified[ref.sha256] = len(data)
        return recorded

    def read_content(self, digest: str) -> bytes:
        """Read an immutable manifest/cache object identified by its actual digest."""
        return self._read_digest(digest)

    def content_path(self, digest: str) -> Path:
        """Where such an object lives (either layout, or the stage-cache pointer carrying it
        inline), so a caller can watch that one file for drift; where it would be written when
        it is in none."""
        target = self._object_path(digest)
        index = _inline_index(self.root)
        for scan in (False, True):
            if scan:
                self._scan_inline(index)
            with index.lock:
                name = index.where.get(digest)
            if name is not None:
                return self._stage_cache_pointer(name)
            if not scan and (existing := stored_path(self._flat(), digest)) is not None:
                return existing
        return target

    def publish(self, manifest: DocumentManifest) -> str:
        for ref in manifest_assets(manifest):
            self.verify(ref)
        payload = ManifestEnvelope(manifest=manifest).model_dump_json().encode()
        ref = self.put(payload, media_type="application/json")
        if self._activate_on_publish:
            self.activate(ref.sha256)
        return ref.sha256

    def activate(self, manifest_id: str) -> None:
        self.load(manifest_id)
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False, mode="w") as stream:
            temporary = Path(stream.name)
            stream.write(manifest_id + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, self.root / "current-manifest")
        finally:
            temporary.unlink(missing_ok=True)

    def load_current(self) -> DocumentSnapshot:
        return self.load((self.root / "current-manifest").read_text().strip())

    def load(self, manifest_id: str) -> DocumentSnapshot:
        """The manifest and every asset it names, verified; once per instance and manifest id.

        The id is the manifest's own digest and every asset is named by its digest, so a
        snapshot this instance verified stays exactly that snapshot. Later calls return it
        without touching the disk unless ``verify_every_load`` is set.
        """
        if not self._verify_every_load and manifest_id in self._snapshots:
            return self._snapshots[manifest_id]
        payload = self._read_digest(manifest_id)
        manifest = ManifestEnvelope.model_validate_json(payload).manifest
        self.verify_snapshot(manifest_id, manifest_assets(manifest))
        snapshot = DocumentSnapshot(manifest_id, manifest)
        if not self._verify_every_load:
            self._snapshots[manifest_id] = snapshot
        return snapshot


def manifest_assets(manifest: DocumentManifest) -> tuple[AssetRef, ...]:
    refs = [manifest.source]
    for page in manifest.pages:
        refs.extend((page.svg, page.text))
    refs.extend((manifest.region.native_svg, manifest.region.cropped_svg, manifest.region.text))
    return tuple(refs)
