"""Local content-addressed assets and immutable manifests; not a production CAS release service.

Verification cache (ADR 00NN, source verification cache): a store instance remembers which
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
from ragspine.common.evidence.file_placement import link_new_file
from ragspine.extraction.evidence.document.models import (
    AssetRef,
    DocumentManifest,
    DocumentSnapshot,
)


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
        return self._object_path(ref.sha256)

    def _object_path(self, digest: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Invalid content-addressed artifact identifier")
        return self.root / "objects" / "sha256" / digest

    def put(self, data: bytes, *, media_type: str) -> AssetRef:
        ref = AssetRef(hashlib.sha256(data).hexdigest(), media_type, len(data))
        target = self.asset_path(ref)
        if self._is_verified(ref):
            return ref
        if target.exists():
            # Exactly what a refused link leads to below, without writing a temporary first.
            self.get(ref)
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
                self.get(ref)
        finally:
            temporary.unlink(missing_ok=True)
        return ref

    def _read_digest(self, digest: str) -> bytes:
        data = self._object_path(digest).read_bytes()
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
        """Where such an object lives, so a caller can watch that one file for drift."""
        return self._object_path(digest)

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
