"""Local content-addressed assets and immutable manifests; not a production CAS release service.

Verification cache (ADR 0024, source verification cache): a store instance remembers which
digests it has itself read back and hashed, and which source manifests it has fully verified.
A sweep whose only purpose is to verify (``verify``, ``load``, ``publish``, ``put`` of an object
already on disk) skips what this instance already verified; bytes a caller consumes (``get``,
``read_content``) are always read and hashed again. A new instance, and so a new process, starts
empty. ``verify_every_load`` (default: ``APP_VERIFY_EVERY_REQUEST``) turns the cache off.
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from enterprise_pdf_rag.adapters.http.document_schemas import ManifestEnvelope
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.file_placement import (
    link_new_file,
    note_repair,
    read_stored,
    replace_file,
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


class LocalDocumentStore:
    def __init__(
        self,
        root: Path,
        *,
        activate_on_publish: bool = True,
        verify_every_load: bool | None = None,
    ) -> None:
        self.root = root
        self._activate_on_publish = activate_on_publish
        self._verify_every_load = (
            get_settings().verify_every_request if verify_every_load is None else verify_every_load
        )
        # digest -> byte length, for objects this instance read back and hashed itself. An
        # object it only wrote is not in here: the first sweep after a write reads it once.
        self._verified: dict[str, int] = {}
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
        self._object_path(digest)
        _, data = read_stored(self._flat(), digest)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Stored artifact digest mismatch; source review is unavailable")
        self._verified[digest] = len(data)
        return data

    def get(self, ref: AssetRef) -> bytes:
        data = self._read_digest(ref.sha256)
        if len(data) != ref.byte_length:
            raise ValueError("Stored artifact length mismatch")
        return data

    def verify(self, ref: AssetRef) -> None:
        """Check one object against its reference; skipped when this instance already did."""
        if not self._is_verified(ref):
            self.get(ref)

    def _is_verified(self, ref: AssetRef) -> bool:
        return not self._verify_every_load and self._verified.get(ref.sha256) == ref.byte_length

    def read_content(self, digest: str) -> bytes:
        """Read an immutable manifest/cache object identified by its actual digest."""
        return self._read_digest(digest)

    def content_path(self, digest: str) -> Path:
        """Where such an object lives (either layout), so a caller can watch that one file for
        drift; where it would be written when it is in neither."""
        target = self._object_path(digest)
        return stored_path(self._flat(), digest) or target

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
        for ref in manifest_assets(manifest):
            self.verify(ref)
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
