"""Local content-addressed assets and immutable manifests; not a production CAS release service."""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from enterprise_pdf_rag.adapters.http.document_schemas import ManifestEnvelope
from ragspine.extraction.evidence.document.models import (
    AssetRef,
    DocumentManifest,
    DocumentSnapshot,
)


class LocalDocumentStore:
    def __init__(self, root: Path, *, activate_on_publish: bool = True) -> None:
        self.root = root
        self._activate_on_publish = activate_on_publish

    def asset_path(self, ref: AssetRef) -> Path:
        return self._object_path(ref.sha256)

    def _object_path(self, digest: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Invalid content-addressed artifact identifier")
        return self.root / "objects" / "sha256" / digest

    def put(self, data: bytes, *, media_type: str) -> AssetRef:
        ref = AssetRef(hashlib.sha256(data).hexdigest(), media_type, len(data))
        target = self.asset_path(ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.get(ref)
        finally:
            temporary.unlink()
        return ref

    def _read_digest(self, digest: str) -> bytes:
        data = self._object_path(digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Stored artifact digest mismatch; source review is unavailable")
        return data

    def get(self, ref: AssetRef) -> bytes:
        data = self._read_digest(ref.sha256)
        if len(data) != ref.byte_length:
            raise ValueError("Stored artifact length mismatch")
        return data

    def read_content(self, digest: str) -> bytes:
        """Read an immutable manifest/cache object identified by its actual digest."""
        return self._read_digest(digest)

    def content_path(self, digest: str) -> Path:
        """Where such an object lives, so a caller can watch that one file for drift."""
        return self._object_path(digest)

    def publish(self, manifest: DocumentManifest) -> str:
        for ref in manifest_assets(manifest):
            self.get(ref)
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
        payload = self._read_digest(manifest_id)
        manifest = ManifestEnvelope.model_validate_json(payload).manifest
        for ref in manifest_assets(manifest):
            self.get(ref)
        return DocumentSnapshot(manifest_id, manifest)


def manifest_assets(manifest: DocumentManifest) -> tuple[AssetRef, ...]:
    refs = [manifest.source]
    for page in manifest.pages:
        refs.extend((page.svg, page.text))
    refs.extend((manifest.region.native_svg, manifest.region.cropped_svg, manifest.region.text))
    return tuple(refs)
