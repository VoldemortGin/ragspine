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
import os
import re
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path

from enterprise_pdf_rag.adapters.http.document_schemas import ManifestEnvelope
from enterprise_pdf_rag.adapters.verification_receipt import (
    RECEIPTS_DIRECTORY,
    FileState,
    decode_external_receipt,
    encode_external_receipt,
    encode_receipt,
    external_unchanged,
    inline_pointer,
    receipt_holds,
    state_of,
    write_receipt,
)
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.file_placement import sharded_path

# The stage-pointer format and the inline-output machinery live with the backend seam now
# (sqlite object store PR-2); these names are re-exported so existing imports keep working.
from ragspine.common.evidence.object_backend.files import (  # noqa: F401
    _INLINE_INDEXES,
    INLINE_ARTIFACT_LIMIT,
    _envelope_artifact,
    _inline_index,
    _InlineIndex,
    split_stage_pointer,
)
from ragspine.common.evidence.object_backend.protocol import ObjectBackend, PinToken
from ragspine.common.evidence.object_backend.registry import open_backend
from ragspine.extraction.evidence.document.models import (
    AssetRef,
    DocumentManifest,
    DocumentSnapshot,
)

_DIGEST = re.compile(r"[0-9a-f]{64}")


class LocalDocumentStore:
    def __init__(
        self,
        root: Path,
        *,
        backend: ObjectBackend | None = None,
        activate_on_publish: bool = True,
        verify_every_load: bool | None = None,
        persisted_receipts: bool | None = None,
        record_receipts: bool = True,
    ) -> None:
        self.root = root
        # Where the bytes live (ADR 0036): the file layout, or sqlite when available and
        # selected (``APP_OBJECT_STORE_BACKEND``). A borrowed backend (``ProcessingStore``
        # shares one with its asset store) is never closed here.
        self._backend = open_backend(root) if backend is None else backend
        self._owns_backend = backend is None
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
            self.root,
            backend=self._backend,
            activate_on_publish=self._activate_on_publish,
            verify_every_load=True,
        )

    @property
    def object_backend(self) -> str:
        """Which backend holds this store's bytes: ``files`` or ``sqlite`` (ADR 0036)."""
        return self._backend.kind

    def transaction(self) -> AbstractContextManager[None]:
        """A reentrant write-transaction scope (no-op on the file layout)."""
        return self._backend.transaction()

    def close(self) -> None:
        """Release the backend (sqlite: connections + the writer lease); a borrowed one stays."""
        if self._owns_backend:
            self._backend.close()

    def asset_path(self, ref: AssetRef) -> Path:
        """``content_path`` for a reference; ``LookupError`` for a db-resident object."""
        return self.content_path(ref.sha256)

    def _flat(self) -> Path:
        return self.root / "objects" / "sha256"

    def _object_path(self, digest: str) -> Path:
        """Where an object is written: the sharded layout (ADR 0029)."""
        if _DIGEST.fullmatch(digest) is None:
            raise ValueError("Invalid content-addressed artifact identifier")
        return sharded_path(self._flat(), digest)

    def digests(self) -> list[str]:
        """Every stored object's digest, in any layout or the backend db (sorted)."""
        return self._backend.object_names()

    def put(self, data: bytes, *, media_type: str) -> AssetRef:
        """Store ``data`` under its digest. An object already there is read back and verified;
        a damaged one (missing, empty, truncated or other bytes — e.g. lost by an asynchronous
        flush) is rewritten with these bytes, which are by construction the bytes the name
        means (ADR 0029). Never writes into the legacy flat directory; where the entry lands
        (sharded file, or a db row with large objects external) is the backend's (ADR 0036)."""
        ref = AssetRef(hashlib.sha256(data).hexdigest(), media_type, len(data))
        if self._is_verified(ref):
            return ref
        if self._backend.put_object(ref.sha256, data, media_type) == "existing":
            self._verified[ref.sha256] = len(data)
        else:
            self._written.add(ref.sha256)
        return ref

    def _read_digest(self, digest: str) -> bytes:
        """The bytes of ``digest`` wherever they are stored (see the module docstring)."""
        self._object_path(digest)
        data = self._backend.get_content(digest)
        if data is None:
            raise FileNotFoundError(f"No stored object or inline output for digest {digest}")
        self._verified[digest] = len(data)
        return data

    def read_object(self, digest: str) -> bytes:
        """The object entry of ``digest`` (db row, sharded, then flat), hashed; no inline lookup."""
        self._object_path(digest)
        data = self._backend.get_object(digest)
        if data is None:
            raise FileNotFoundError(f"No stored object for digest {digest}")
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
        self._backend.note_product(digest, pointer_name)

    def note_inline_verified(self, digest: str, pointer_name: str, length: int) -> None:
        """As ``note_inline``, for bytes the caller has just read there and hashed."""
        self.note_inline(digest, pointer_name)
        self._verified[digest] = length

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
        if self._backend.kind != "files":
            # sqlite (ADR 0036): db-resident entries are always read back and re-hashed —
            # the sweep is a handful of batched row reads, so no receipt ever vouches for
            # them; only the external files (the PDF, large indexes, legacy entries) keep
            # the ADR 0034 stat-receipt shortcut.
            self._verify_snapshot_external(subject, refs)
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

    def _verify_snapshot_external(self, subject: str, refs: Sequence[AssetRef]) -> None:
        """The sqlite-backend sweep (ADR 0036): every db-resident object is read back and
        re-hashed in batches, and only external files carry a persisted stat receipt
        (``verification-receipt-sqlite-v1``, stored as the backend record
        ``verification-receipts/<subject>``). Tampering with a db row is therefore caught by
        the next sweep itself — a receipt can never vouch past it (ADR 0034, not weakened)."""
        backend = self._backend
        flat = self._flat()
        pointers = self.root / "stage-cache"
        record_name = f"{RECEIPTS_DIRECTORY}/{subject}"
        payload = backend.record(record_name)
        held = None if payload is None else decode_external_receipt(payload, subject, refs)
        attested = {} if held is None else {state.sha256: state for state in held}
        lengths: dict[str, int] = {}
        in_db: list[str] = []
        states: dict[str, FileState] = {}
        recordable = self._record_receipts
        fresh = False
        for ref in refs:
            seen = lengths.get(ref.sha256)
            if seen is not None:
                if seen != ref.byte_length:
                    raise ValueError("Stored artifact length mismatch")
                continue
            lengths[ref.sha256] = ref.byte_length
            location = backend.object_location(ref.sha256)
            if location is None:
                # In the db (or an inline product there): always read back and re-hash.
                if not self._is_verified(ref):
                    in_db.append(ref.sha256)
                continue
            previous = attested.get(ref.sha256)
            if (
                previous is not None
                and previous.byte_length == ref.byte_length
                and external_unchanged(self.root, flat, pointers, previous)
            ):
                states[ref.sha256] = previous
                self._attested[ref.sha256] = ref.byte_length
                name = inline_pointer(pointers, previous)
                if name is not None:
                    self.note_inline(ref.sha256, name)
                continue
            fresh = True
            recorded = self._read_external(ref, location)
            if recorded is None:
                recordable = False
                self.get(ref)
                continue
            states[ref.sha256] = recorded
        try:
            for digest, data in backend.verify_many(in_db):
                if len(data) != lengths[digest]:
                    raise ValueError("Stored artifact length mismatch")
                self._verified[digest] = len(data)
        except LookupError as error:
            raise FileNotFoundError(str(error)) from None
        self._attested_subjects.add(subject)
        if recordable and states and fresh and not self._written.intersection(states):
            backend.put_record(
                record_name, encode_external_receipt(subject, refs, tuple(states.values()))
            )

    def _read_external(self, ref: AssetRef, path: Path) -> FileState | None:
        """Stat, then read and hash, one external file of the sqlite backend (an object file,
        or the legacy stage-cache pointer carrying ``ref`` inline). None when the location no
        longer holds those bytes: the caller then reads the ordinary way and records nothing."""
        try:
            state = path.stat()
            data = path.read_bytes()
        except OSError:
            return None
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
        """The one **file** such an object lives in (either layout, or the stage-cache pointer
        carrying it inline), so a caller can watch that one file for drift; under the file
        layout, where it would be written when it is in none. An object living in the backend
        db (sqlite: an inlined object or inline stage output) has no file to watch —
        ``LookupError``; pin it with ``pin`` / ``pin_unchanged`` instead."""
        self._object_path(digest)
        return self._backend.content_path(digest)

    def pin(self, digest: str) -> PinToken:
        """A drift token for one stored object (the mount guard's shortcut, ADR 0036):
        backend-opaque marks, never content. ``LookupError`` when the object is absent."""
        self._object_path(digest)
        return self._backend.pin(digest)

    def pin_unchanged(self, token: PinToken) -> bool:
        """Whether the pinned object is still those bytes (cheap marks, else re-hash)."""
        return self._backend.pin_unchanged(token)

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
        self._backend.set_pointer("current-manifest", manifest_id)

    def current_manifest_id(self) -> str | None:
        """The manifest ``current-manifest`` names; None when absent or unreadable."""
        return self._backend.pointer("current-manifest")

    def load_current(self) -> DocumentSnapshot:
        current = self.current_manifest_id()
        if current is None:
            raise FileNotFoundError(str(self.root / "current-manifest"))
        return self.load(current)

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
