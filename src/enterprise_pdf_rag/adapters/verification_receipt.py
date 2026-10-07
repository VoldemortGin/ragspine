"""Persisted verification receipts: a full verification of one snapshot, reusable across processes.

ADR 0034. After a store instance has statted, read and hashed every file one immutable snapshot
names (its manifest digest is the receipt's subject), it records, beside the objects, which
files it verified and what ``stat`` said about each one *before* it read it. A later instance —
another stage, another process — that finds the receipt intact, for the same subject and the
same file set, and every file still where it was with the same size, mtime and ctime, accepts
the snapshot without reading it again. Anything else is no receipt at all.

File: ``<store root>/verification-receipts/<subject>`` — the SHA-256 of the body on the first
line, then the body (JSON). The body names its policy, its subject, the fingerprint of the
reference set it covers and one entry per distinct digest: the file its bytes were read from —
the object (sharded or flat), or the stage-cache pointer that carries it inline (ADR 0029
Amendment 2) — and that file's size, mtime and ctime. It is not a content-addressed object and
nothing else reads it; losing or damaging it only costs one full verification.
"""

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Collection, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from ragspine.common.evidence.file_placement import sharded_directory, sharded_path
from ragspine.extraction.evidence.document.models import AssetRef

RECEIPT_POLICY = "verification-receipt-v1"
RECEIPTS_DIRECTORY = "verification-receipts"
_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class FileState:
    """One verified object: its identity, the file it was read from and that file's ``stat``
    before the read (an inline pointer is larger than the output it carries)."""

    sha256: str
    byte_length: int
    path: str
    size: int
    mtime_ns: int
    ctime_ns: int


def receipt_path(root: Path, subject: str) -> Path:
    if _DIGEST.fullmatch(subject) is None:
        raise ValueError("A verification receipt is named by a SHA-256 manifest id")
    return root / RECEIPTS_DIRECTORY / subject


def state_of(root: Path, path: Path, ref: AssetRef, state: os.stat_result) -> FileState:
    return FileState(
        ref.sha256,
        ref.byte_length,
        path.relative_to(root).as_posix(),
        state.st_size,
        state.st_mtime_ns,
        state.st_ctime_ns,
    )


def file_state(root: Path, path: Path, ref: AssetRef) -> FileState:
    """``stat`` of ``path`` (an object of the store at ``root``) for ``ref``; raises if absent."""
    return state_of(root, path, ref, path.stat())


def refs_fingerprint(refs: Iterable[AssetRef]) -> str:
    """The identity of a reference set: its distinct (digest, length) pairs, sorted."""
    pairs = sorted({f"{ref.sha256}:{ref.byte_length}" for ref in refs})
    return hashlib.sha256("\n".join(pairs).encode()).hexdigest()


def encode_receipt(subject: str, refs: Sequence[AssetRef], states: Sequence[FileState]) -> bytes:
    body = json.dumps(
        {
            "receipt": RECEIPT_POLICY,
            "subject": subject,
            "refs": refs_fingerprint(refs),
            "files": [asdict(state) for state in sorted(states, key=lambda item: item.sha256)],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(body).hexdigest().encode() + b"\n" + body + b"\n"


def _decode(data: bytes, subject: str, refs: Sequence[AssetRef]) -> tuple[FileState, ...] | None:
    """The receipt's entries when it is intact and covers exactly ``refs`` of ``subject``."""
    head, _, body = data.partition(b"\n")
    body = body.removesuffix(b"\n")
    if hashlib.sha256(body).hexdigest().encode() != head.strip():
        return None
    try:
        payload = json.loads(body)
        if (payload["receipt"], payload["subject"], payload["refs"]) != (
            RECEIPT_POLICY,
            subject,
            refs_fingerprint(refs),
        ):
            return None
        states = tuple(FileState(**entry) for entry in payload["files"])
    except (ValueError, KeyError, TypeError):
        return None
    expected = {(ref.sha256, ref.byte_length) for ref in refs}
    if {(state.sha256, state.byte_length) for state in states} != expected or len(states) != len(
        expected
    ):
        return None
    return states


def inline_pointer(pointers: Path, state: FileState) -> str | None:
    """The stage-cache pointer name an entry was read from, if it was read from one."""
    parts = PurePosixPath(state.path).parts
    if (
        len(parts) == 3
        and parts[0] == sharded_directory(pointers).name
        and _DIGEST.fullmatch(parts[2]) is not None
        and parts[1] == parts[2][:2]
    ):
        return parts[2]
    return None


def _unchanged(root: Path, flat: Path, pointers: Path, state: FileState) -> bool:
    """The file is still a place that digest is read from, and was not touched since."""
    sharded = sharded_path(flat, state.sha256)
    path = root / state.path
    if inline_pointer(pointers, state) is not None:
        if path.parent.parent != sharded_directory(pointers):
            return False
    elif path == flat / state.sha256:
        # Reads look in the sharded place first: a file there would be what is consumed.
        if sharded.exists():
            return False
    elif path != sharded or state.size != state.byte_length:
        return False
    try:
        now = path.stat()
    except OSError:
        return False
    return (now.st_size, now.st_mtime_ns, now.st_ctime_ns) == (
        state.size,
        state.mtime_ns,
        state.ctime_ns,
    )


def receipt_holds(
    root: Path,
    flat: Path,
    pointers: Path,
    subject: str,
    refs: Sequence[AssetRef],
    *,
    unverified: Collection[str],
) -> tuple[FileState, ...] | None:
    """The entries of an intact receipt that covers exactly ``refs`` of ``subject``, when every
    file whose digest is in ``unverified`` is untouched since; else None. The caller has hashed
    the others itself. ``flat`` is the store's flat object directory, ``pointers`` its flat
    stage-cache directory (each names its sharded sibling)."""
    try:
        data = receipt_path(root, subject).read_bytes()
    except OSError:
        return None
    states = _decode(data, subject, refs)
    if states is None or not all(
        _unchanged(root, flat, pointers, state) for state in states if state.sha256 in unverified
    ):
        return None
    return states


def write_receipt(root: Path, subject: str, payload: bytes) -> None:
    """Replace the receipt atomically; a store that cannot take one simply goes without."""
    target = receipt_path(root, subject)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        return
