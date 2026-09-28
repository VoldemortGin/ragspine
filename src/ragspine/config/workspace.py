"""Workspace-owned compatibility metadata for persisted narrative indexes."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ragspine.config.resolution import EffectivePlan

_LEGACY_CONTRACT_JSON = json.dumps(
    {
        "chunking": {
            "chunker": "none",
            "max_chars": 480,
            "overlap_chars": 80,
        }
    },
    sort_keys=True,
    separators=(",", ":"),
)


class ReindexRequiredError(RuntimeError):
    """The requested runtime plan is incompatible with the persisted index.

    A workspace's chunk index is never rebuilt in place, so the message names the two
    remedies that exist: reopen with the stored ``indexing`` config, or ingest the
    sources into a new empty workspace with the requested one.
    """

    def __init__(
        self,
        workspace: str | Path,
        categories: tuple[str, ...],
        *,
        stored_indexing: dict[str, object] | None = None,
        requested_indexing: dict[str, object] | None = None,
    ) -> None:
        self.categories = categories
        self.stored_indexing = stored_indexing
        self.requested_indexing = requested_indexing
        changed = ", ".join(categories)
        stored = _compact_json(stored_indexing)
        requested = _compact_json(requested_indexing)
        super().__init__(
            f'workspace index in "{Path(workspace)}" is incompatible in {changed}: '
            f"stored indexing={stored}, requested indexing={requested}. "
            "The chunk index is never rebuilt in place; either reopen the workspace with the "
            f'stored config (RAGSpine.local(workspace, config={{"indexing": {stored}}})), '
            "or ingest the sources into a new empty workspace directory with the requested "
            "indexing config"
        )


def _compact_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class WorkspaceIndexMetadata:
    """Persist and validate one narrative-index contract behind a small API."""

    def __init__(self, db_path: str | Path, workspace: str | Path) -> None:
        self._db_path = str(db_path)
        self._workspace = Path(workspace)

    def init_schema(self) -> None:
        with closing(sqlite3.connect(self._db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ragspine_index_metadata (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        fingerprint TEXT NOT NULL,
                        contract_json TEXT NOT NULL
                    )
                    """
                )

    def assert_compatible(self, plan: EffectivePlan) -> None:
        with closing(sqlite3.connect(self._db_path)) as connection:
            stored = self._read(connection)
            has_legacy_chunks = stored is None and self._has_narrative_chunks(connection)
        if stored is not None and stored[0] == plan.index_fingerprint:
            return
        if stored is None and (
            not has_legacy_chunks or self._contract_json(plan) == _LEGACY_CONTRACT_JSON
        ):
            return
        if stored is None:
            stored = ("legacy", _LEGACY_CONTRACT_JSON)
        raise self._reindex_error(stored[1], plan)

    def claim(self, plan: EffectivePlan) -> None:
        """Atomically claim an empty index or verify its existing contract."""
        contract = self._contract_json(plan)
        with closing(sqlite3.connect(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                stored = self._read(connection)
                if stored is None and self._has_narrative_chunks(connection):
                    if contract != _LEGACY_CONTRACT_JSON:
                        raise self._reindex_error(_LEGACY_CONTRACT_JSON, plan)
                elif stored is not None and stored[0] != plan.index_fingerprint:
                    raise self._reindex_error(stored[1], plan)
                connection.execute(
                    """
                    INSERT INTO ragspine_index_metadata (singleton, fingerprint, contract_json)
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO NOTHING
                    """,
                    (plan.index_fingerprint, contract),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _read(connection: sqlite3.Connection) -> tuple[str, str] | None:
        row = connection.execute(
            "SELECT fingerprint, contract_json FROM ragspine_index_metadata WHERE singleton = 1"
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    @staticmethod
    def _has_narrative_chunks(connection: sqlite3.Connection) -> bool:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'narrative_chunk'"
        ).fetchone()
        if table is None:
            return False
        return connection.execute("SELECT 1 FROM narrative_chunk LIMIT 1").fetchone() is not None

    @staticmethod
    def _contract_json(plan: EffectivePlan) -> str:
        return json.dumps(
            {"chunking": plan.config.indexing.model_dump(mode="json")},
            sort_keys=True,
            separators=(",", ":"),
        )

    def _reindex_error(self, stored_json: str, plan: EffectivePlan) -> ReindexRequiredError:
        try:
            stored_indexing = json.loads(stored_json).get("chunking")
        except (AttributeError, TypeError, ValueError):
            stored_indexing = None
        return ReindexRequiredError(
            self._workspace,
            self._changed_categories(stored_json, plan),
            stored_indexing=stored_indexing,
            requested_indexing=json.loads(self._contract_json(plan))["chunking"],
        )

    @classmethod
    def _changed_categories(cls, stored_json: str, plan: EffectivePlan) -> tuple[str, ...]:
        try:
            stored = json.loads(stored_json)
        except (TypeError, ValueError):
            return ("chunking",)
        current = json.loads(cls._contract_json(plan))
        return tuple(key for key in current if stored.get(key) != current[key]) or ("chunking",)


__all__ = ["ReindexRequiredError", "WorkspaceIndexMetadata"]
