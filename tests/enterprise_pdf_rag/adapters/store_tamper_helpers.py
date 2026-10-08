"""Backend-agnostic tampering with stored entries, for the damage / drift tests.

The file layout keeps every entry in a file; the sqlite backend (ADR 0036) keeps small
objects, stage entries, pointers and records as db rows and only externals (the PDF, large
indexes) as files. A test that simulates a failed flush or a hostile edit uses these helpers
so the same test damages whatever the active backend actually wrote.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from ragspine.common.evidence.file_placement import stored_path

DB_NAME = "store.sqlite"


def _db(store_root: Path) -> Path:
    return store_root / DB_NAME


def _object_row(store_root: Path, digest: str) -> tuple[int] | None:
    """(external,) of the object's db row, or None when the db or the row is absent."""
    if not _db(store_root).is_file():
        return None
    with closing(sqlite3.connect(_db(store_root))) as connection:
        row = connection.execute(
            "SELECT external FROM objects WHERE digest = ?", (digest,)
        ).fetchone()
    return None if row is None else (int(row[0]),)


def object_file(store_root: Path, digest: str) -> Path | None:
    """The object's file (sharded or flat), when its bytes live in one."""
    return stored_path(store_root / "objects" / "sha256", digest)


def tamper_object(store_root: Path, digest: str, data: bytes = b"tampered") -> None:
    """Replace a stored object's bytes under its unchanged name (a lying entry)."""
    row = _object_row(store_root, digest)
    if row is not None and not row[0]:
        with closing(sqlite3.connect(_db(store_root))) as connection, connection:
            connection.execute(
                "UPDATE objects SET bytes = ?, encoding = 'raw' WHERE digest = ?",
                (data, digest),
            )
        return
    path = object_file(store_root, digest)
    assert path is not None, f"no stored entry to tamper for {digest}"
    path.write_bytes(data)


def remove_object(store_root: Path, digest: str) -> None:
    """Drop a stored object entirely (what a lost asynchronous flush leaves)."""
    row = _object_row(store_root, digest)
    if row is not None and not row[0]:
        with closing(sqlite3.connect(_db(store_root))) as connection, connection:
            connection.execute("DELETE FROM objects WHERE digest = ?", (digest,))
        return
    path = object_file(store_root, digest)
    assert path is not None, f"no stored entry to remove for {digest}"
    path.unlink()


def truncate_object(store_root: Path, digest: str) -> None:
    """Cut a stored object's bytes short (a torn write)."""
    row = _object_row(store_root, digest)
    if row is not None and not row[0]:
        with closing(sqlite3.connect(_db(store_root))) as connection, connection:
            blob = connection.execute(
                "SELECT bytes FROM objects WHERE digest = ?", (digest,)
            ).fetchone()[0]
            connection.execute(
                "UPDATE objects SET bytes = ? WHERE digest = ?",
                (bytes(blob)[: len(blob) // 2], digest),
            )
        return
    path = object_file(store_root, digest)
    assert path is not None, f"no stored entry to truncate for {digest}"
    path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])


def tamper_stage_entry(store_root: Path, fingerprint: str) -> None:
    """Make one stage entry's envelope stop hashing to its digest line."""
    db = _db(store_root)
    if db.is_file():
        with closing(sqlite3.connect(db)) as connection, connection:
            cursor = connection.execute(
                "UPDATE stage_cache SET envelope = ? WHERE fingerprint = ?",
                (b"{not the envelope}", fingerprint),
            )
            if cursor.rowcount:
                return
    pointer = stored_path(store_root / "stage-cache", fingerprint)
    assert pointer is not None, f"no stage entry to tamper for {fingerprint}"
    head = pointer.read_bytes().partition(b"\n")[0]
    pointer.write_bytes(head + b"\n" + b"{not the envelope}" + b"\n")


def remove_stage_entry(store_root: Path, fingerprint: str) -> None:
    """Drop one stage entry entirely."""
    db = _db(store_root)
    if db.is_file():
        with closing(sqlite3.connect(db)) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM stage_cache WHERE fingerprint = ?", (fingerprint,)
            )
            if cursor.rowcount:
                return
    pointer = stored_path(store_root / "stage-cache", fingerprint)
    assert pointer is not None, f"no stage entry to remove for {fingerprint}"
    pointer.unlink()
