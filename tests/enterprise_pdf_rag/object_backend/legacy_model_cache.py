"""The model-cache write path of ``json_completion`` as it was before PR-3, frozen verbatim.

PR-3 moved these functions behind ``FileModelCacheBackend``; the equivalence tests keep
comparing the backend with this copy, so the files layout stays byte-identical to what every
earlier release wrote. The clock, process token, expiry judge and error type are looked up in
``json_completion`` at call time, so the tests' seams there apply here too. Never edit the
logic below: it is the baseline, not an implementation.
"""

import json
import os
import socket
import tempfile
from contextlib import suppress
from pathlib import Path

from ragspine.common.evidence.file_placement import fsync_directory, link_new_file, replace_file
from ragspine.common.evidence.providers import json_completion


def _immutable_write(path: Path, content: bytes, *, replace_damaged: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        try:
            link_new_file(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                if not replace_damaged:
                    raise json_completion.JsonCompletionError("cache_conflict") from None
                replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _claim_owner(fingerprint: str, lease_seconds: int) -> bytes:
    return json.dumps(
        {
            "claim": json_completion.CLAIM_FORMAT,
            "request_fingerprint": fingerprint,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "process": json_completion._PROCESS_TOKEN,
            "created_at": round(json_completion._wall_clock(), 3),
            "lease_seconds": lease_seconds,
        },
        sort_keys=True,
    ).encode()


def _write_claim(path: Path, content: bytes) -> bool:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)
    return True


def _claim_path(record_path: Path, generation: int = 0) -> Path:
    claim = record_path.with_suffix(record_path.suffix + ".claim")
    return claim if generation == 0 else claim.with_name(f"{claim.name}.takeover-{generation}")


def _latest_takeover(record_path: Path) -> int:
    generation = 0
    while _claim_path(record_path, generation + 1).exists():
        generation += 1
    return generation


def _claim_request(record_path: Path, fingerprint: str, lease_seconds: int) -> int:
    record_path.parent.mkdir(parents=True, exist_ok=True)
    content = _claim_owner(fingerprint, lease_seconds)
    if _write_claim(_claim_path(record_path), content):
        return 0
    generation = _latest_takeover(record_path)
    holder = _claim_path(record_path, generation)
    try:
        held = holder.read_bytes()
        modified = holder.stat().st_mtime
    except FileNotFoundError:
        raise json_completion.JsonCompletionError(
            "request_in_progress_or_uncertain", fingerprint
        ) from None
    if not json_completion._expired(held, modified) or not _write_claim(
        _claim_path(record_path, generation + 1), content
    ):
        raise json_completion.JsonCompletionError("request_in_progress_or_uncertain", fingerprint)
    return generation + 1


def _release_claims(record_path: Path) -> None:
    for generation in range(_latest_takeover(record_path), -1, -1):
        with suppress(OSError):
            _claim_path(record_path, generation).unlink(missing_ok=True)
