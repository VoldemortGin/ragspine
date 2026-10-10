"""Backend-neutral views of a model cache for tests that run on both backends (PR-3).

A ``JsonCompletionClient`` keeps its cache either as the flat ``requests`` / ``responses`` /
``contexts`` files (``files``) or in ``<cache>/model-cache.sqlite`` (``sqlite``), which also
reads any such files left by older code. These helpers read and damage an entry the same way
whichever backend wrote it, so a test states its expectation once.

Record keys are ``<fingerprint>`` or ``<fingerprint>.retry-1`` (ADR 0021).
"""

import json
import sqlite3
import zlib
from contextlib import closing
from pathlib import Path
from typing import Any

DB_NAME = "model-cache.sqlite"


def _db(cache: Path) -> Path | None:
    """``<cache>/model-cache.sqlite``; for a document's ``processing/model-cache`` written by the
    staged backend, the document's single ``document.sqlite`` (ADR 0047: same table names)."""
    path = cache / DB_NAME
    if path.is_file():
        return path
    document = cache.parent.parent / "document.sqlite"
    if cache.name == "model-cache" and cache.parent.name == "processing" and document.is_file():
        return document
    return None


def _rows(cache: Path, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    db = _db(cache)
    if db is None:
        return []
    with closing(sqlite3.connect(db)) as connection:
        try:
            return [tuple(row) for row in connection.execute(sql, parameters).fetchall()]
        except sqlite3.OperationalError:  # tables not created yet
            return []


def _decode(blob: bytes, encoding: object) -> bytes:
    data = bytes(blob)
    return zlib.decompress(data) if encoding == "zlib" else data


# ---- reading -------------------------------------------------------------------------------


def record_keys(cache: Path) -> list[str]:
    """Every record key present: ``requests`` rows and ``requests/*.json`` files (sorted)."""
    keys = {str(row[0]) for row in _rows(cache, "SELECT record_key FROM requests")}
    keys.update(path.name.removesuffix(".json") for path in cache.glob("requests/*.json"))
    return sorted(keys)


def record_bytes(cache: Path, key: str) -> bytes | None:
    """The bytes of one record, the db row first (as the sqlite backend reads it)."""
    rows = _rows(cache, "SELECT record FROM requests WHERE record_key = ?", (key,))
    if rows:
        return bytes(rows[0][0])
    path = cache / "requests" / f"{key}.json"
    return path.read_bytes() if path.is_file() else None


def read_record(cache: Path, key: str) -> dict[str, object]:
    """One record, parsed; it must exist."""
    data = record_bytes(cache, key)
    assert data is not None, "no such record"
    loaded = json.loads(data)
    assert isinstance(loaded, dict)
    return loaded


def records(cache: Path) -> dict[str, dict[str, object]]:
    """Every record by key, parsed."""
    return {key: read_record(cache, key) for key in record_keys(cache)}


def response_digests(cache: Path) -> list[str]:
    digests = {str(row[0]) for row in _rows(cache, "SELECT digest FROM responses")}
    digests.update(path.name.removesuffix(".json") for path in cache.glob("responses/*.json"))
    return sorted(digests)


def response_bytes(cache: Path, digest: str) -> bytes | None:
    rows = _rows(cache, "SELECT encoding, bytes FROM responses WHERE digest = ?", (digest,))
    if rows:
        return _decode(rows[0][1], rows[0][0])
    path = cache / "responses" / f"{digest}.json"
    return path.read_bytes() if path.is_file() else None


def context_fingerprints(cache: Path) -> list[str]:
    found = {str(row[0]) for row in _rows(cache, "SELECT request_fingerprint FROM contexts")}
    found.update(path.name.removesuffix(".json") for path in cache.glob("contexts/*.json"))
    return sorted(found)


def context_bytes(cache: Path, fingerprint: str) -> bytes | None:
    rows = _rows(
        cache,
        "SELECT encoding, bytes FROM contexts WHERE request_fingerprint = ?",
        (fingerprint,),
    )
    if rows:
        return _decode(rows[0][1], rows[0][0])
    path = cache / "contexts" / f"{fingerprint}.json"
    return path.read_bytes() if path.is_file() else None


def claims(cache: Path) -> list[bytes]:
    """Every claim present, as the holder JSON it stores: ``claims`` rows, then claim files
    (``*.claim`` and ``*.claim.takeover-<n>``) in name order."""
    held = [str(row[0]).encode() for row in _rows(cache, "SELECT claim FROM claims")]
    held.extend(path.read_bytes() for path in sorted(cache.glob("requests/*.claim*")))
    return held


def claim_generations(cache: Path) -> dict[str, int]:
    """Key → current claim generation: a ``claims`` row's, else the highest claim file."""
    current: dict[str, int] = {}
    for path in cache.glob("requests/*.claim*"):
        key, _, rest = path.name.partition(".json.claim")
        generation = int(rest.removeprefix(".takeover-")) if rest else 0
        current[key] = max(current.get(key, 0), generation)
    for row in _rows(cache, "SELECT record_key, generation FROM claims"):
        current[str(row[0])] = int(row[1])
    return current


def entry_count(cache: Path) -> int:
    """Records + responses + contexts + claims: what a call leaves in the cache."""
    return (
        len(record_keys(cache))
        + len(response_digests(cache))
        + len(context_fingerprints(cache))
        + len(claims(cache))
    )


# ---- damaging (an asynchronous flush that failed after the write returned, ADR 0029) --------


def damage_record(cache: Path, key: str, data: bytes) -> None:
    """Overwrite the record under ``key`` wherever it lives (row or file)."""
    if _rows(cache, "SELECT 1 FROM requests WHERE record_key = ?", (key,)):
        _execute(cache, "UPDATE requests SET record = ? WHERE record_key = ?", (data, key))
        return
    path = cache / "requests" / f"{key}.json"
    assert path.is_file(), "no such record"
    path.write_bytes(data)


def damage_response(cache: Path, digest: str, data: bytes | None) -> None:
    """Truncate / replace a stored response (``None`` = lose it altogether)."""
    if _rows(cache, "SELECT 1 FROM responses WHERE digest = ?", (digest,)):
        if data is None:
            _execute(cache, "DELETE FROM responses WHERE digest = ?", (digest,))
        else:
            _execute(
                cache,
                "UPDATE responses SET bytes = ?, encoding = 'raw', byte_length = ?"
                " WHERE digest = ?",
                (data, len(data), digest),
            )
        return
    path = cache / "responses" / f"{digest}.json"
    assert path.is_file(), "no such response"
    if data is None:
        path.unlink()
    else:
        path.write_bytes(data)


def _execute(cache: Path, sql: str, parameters: tuple[object, ...]) -> None:
    db = _db(cache)
    assert db is not None
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute(sql, parameters)


def damage_context(cache: Path, fingerprint: str, data: bytes | None) -> None:
    """Replace a stored request context wherever it lives (``None`` = remove it)."""
    if _rows(cache, "SELECT 1 FROM contexts WHERE request_fingerprint = ?", (fingerprint,)):
        if data is None:
            _execute(cache, "DELETE FROM contexts WHERE request_fingerprint = ?", (fingerprint,))
        else:
            _execute(
                cache,
                "UPDATE contexts SET bytes = ?, encoding = 'raw' WHERE request_fingerprint = ?",
                (data, fingerprint),
            )
        return
    path = cache / "contexts" / f"{fingerprint}.json"
    assert path.is_file(), "no such context"
    if data is None:
        path.unlink()
    else:
        path.write_bytes(data)


def take_over_claim(cache: Path, key: str) -> None:
    """Another process judges the current holder over: advance ``key`` one generation."""
    if _rows(cache, "SELECT 1 FROM claims WHERE record_key = ?", (key,)):
        _execute(
            cache,
            "UPDATE claims SET generation = generation + 1, claim = '{}', process = 'another'"
            " WHERE record_key = ?",
            (key,),
        )
        return
    base = cache / "requests" / f"{key}.json.claim"
    assert base.is_file(), "no such claim"
    base.with_name(f"{base.name}.takeover-{claim_generations(cache)[key] + 1}").write_text("{}")


def drop_db_claims(cache: Path) -> None:
    """Remove every ``claims`` row (a no-op where there is no db), e.g. to replace one by the
    claim file an older client would have left."""
    if _db(cache) is not None:
        _execute(cache, "DELETE FROM claims", ())
