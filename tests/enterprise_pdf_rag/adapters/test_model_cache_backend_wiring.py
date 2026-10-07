"""The model cache behind ``JsonCompletionClient`` is a ``ModelCacheBackend`` (sqlite object
store PR-3, docs/enterprise-pdf-rag/adr/0036-sqlite-object-backend.md).

``APP_OBJECT_STORE_BACKEND=auto`` puts a client's cache in ``<cache>/model-cache.sqlite`` where
the directory takes sqlite, and keeps the flat files otherwise. Under sqlite every entry written
by earlier releases is still read (records, ``.retry-1`` records, responses, contexts, claims),
never rewritten or moved; a damaged one is replaced by a db row. One transaction per write,
none for a replay; the writer lease is held per transaction, so processes sharing one cache
(the root-level answer cache) take turns instead of locking each other out.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from enterprise_pdf_rag.adapters import document_store
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.file_placement import recording_repairs
from ragspine.common.evidence.object_backend import lease
from ragspine.common.evidence.object_backend.files import FileModelCacheBackend
from ragspine.common.evidence.object_backend.probe import clear_probe_cache, probe_directory
from ragspine.common.evidence.object_backend.protocol import BackendUnavailable
from ragspine.common.evidence.object_backend.sqlite import SqliteModelCacheBackend
from ragspine.common.evidence.providers.json_completion import (
    JsonCompletionClient,
    JsonCompletionError,
    JsonCompletionResult,
)
from ragspine.common.evidence.providers.providers import LLMConfig, ProviderRequestError
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    FULL_PUBLISHED_ID,
    lite_env,
    mixed_folder,
    run_mode,
)
from tests.enterprise_pdf_rag.adapters.model_cache_helpers import claims, record_keys

KEY = "wiring-test-key"
_ROOT = Path(__file__).resolve().parents[3]

Sender = Callable[..., bytes]


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    answer: str


def _config() -> LLMConfig:
    return LLMConfig(api_key=SecretStr(KEY), base_url="https://example.invalid", model="m")


def _reply(text: str = "ok") -> bytes:
    content = json.dumps({"answer": text})
    return json.dumps(
        {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    ).encode()


def _counting(calls: list[str]) -> Sender:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        calls.append("sent")
        return _reply()

    return sender


def _client(
    cache: Path, sender: Sender, *, budget: int = 10, retry_failed: bool = False
) -> JsonCompletionClient:
    return JsonCompletionClient(
        _config(), cache_dir=cache, max_live_calls=budget, sender=sender, retry_failed=retry_failed
    )


def _ask(client: JsonCompletionClient, prompt: str = "same") -> JsonCompletionResult[_Answer]:
    return client.complete_text_json(task="wiring", prompt=prompt, response_model=_Answer)


def _mode(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    monkeypatch.setenv("APP_OBJECT_STORE_BACKEND", kind)
    get_settings.cache_clear()
    clear_probe_cache()


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    clear_probe_cache()
    yield
    get_settings.cache_clear()
    clear_probe_cache()


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _count(cache: Path, table: str) -> int:
    with closing(sqlite3.connect(cache / "model-cache.sqlite")) as connection:
        return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


# ---- which backend a client gets ------------------------------------------------------------


def test_auto_puts_the_cache_in_one_db_and_a_client_touches_nothing_until_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "auto")
    cache = tmp_path / "model-cache"
    calls: list[str] = []
    client = _client(cache, _counting(calls))
    assert not cache.exists()  # opened on first use

    assert not _ask(client).cache_hit
    assert client.backend_kind == "sqlite"
    assert isinstance(client.backend, SqliteModelCacheBackend)
    names = {path.name for path in cache.iterdir()}
    assert "model-cache.sqlite" in names
    assert names <= {"model-cache.sqlite", "model-cache.sqlite-wal", "model-cache.sqlite-shm"}
    assert len(record_keys(cache)) == 1
    assert _ask(_client(cache, _counting(calls), budget=0)).cache_hit and calls == ["sent"]


def test_files_mode_keeps_the_flat_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mode(monkeypatch, "files")
    client = _client(tmp_path, _counting([]))
    _ask(client)
    assert client.backend_kind == "files" and isinstance(client.backend, FileModelCacheBackend)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["contexts", "requests", "responses"]


def test_a_given_backend_is_used_as_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mode(monkeypatch, "files")
    backend = SqliteModelCacheBackend(tmp_path)
    client = JsonCompletionClient(
        _config(), cache_dir=tmp_path, max_live_calls=1, sender=_counting([]), backend=backend
    )
    _ask(client)
    assert client.backend is backend and client.backend_kind == "sqlite"
    assert _count(tmp_path, "requests") == 1


class _CommitRefusingConnection:
    """A Volumes-like mount: random writes fail at COMMIT with ``disk I/O error``."""

    def __init__(self, path: str) -> None:
        self._real = sqlite3.connect(path, isolation_level=None)

    def execute(self, sql: str, parameters: Sequence[object] = (), /) -> object:
        if sql.strip().upper() == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, parameters)

    def close(self) -> None:
        self._real.close()


def _poison_probe(directory: Path) -> None:
    probe_directory(
        directory,
        refresh=True,
        connect=lambda path: _CommitRefusingConnection(path),  # type: ignore[arg-type,return-value]
    )


def test_auto_falls_back_to_files_where_sqlite_cannot_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "auto")
    cache = tmp_path / "model-cache"
    _poison_probe(cache)
    calls: list[str] = []
    client = _client(cache, _counting(calls))

    assert not _ask(client).cache_hit
    assert client.backend_kind == "files"
    assert not (cache / "model-cache.sqlite").exists() and len(record_keys(cache)) == 1


def test_explicit_sqlite_where_it_cannot_write_is_an_error_not_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "sqlite")
    cache = tmp_path / "model-cache"
    _poison_probe(cache)
    calls: list[str] = []
    with pytest.raises(BackendUnavailable, match="sqlite_write"):
        _ask(_client(cache, _counting(calls)))
    assert calls == [] and not any(cache.glob("requests"))


# ---- entries written before PR-3 (read-through, never migrated) ----------------------------


def _permanent_failure(calls: list[str]) -> Sender:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        calls.append("refused")
        raise ProviderRequestError(
            "Provider returned HTTP 403; no retry performed", status=403, category="http"
        )

    return sender


def _legacy_cache(cache: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A cache as the files layout (every release before PR-3) leaves it: a success, and a
    failure that a ``retry_failed`` run re-sent into ``.retry-1``. Prompt → fingerprint."""
    _mode(monkeypatch, "files")
    calls: list[str] = []
    answered = _ask(_client(cache, _counting(calls)), "answered").request_fingerprint
    with pytest.raises(JsonCompletionError, match="provider_http_403"):
        _ask(_client(cache, _permanent_failure(calls)), "retried")
    retried = _ask(_client(cache, _counting(calls), retry_failed=True), "retried")
    assert calls == ["sent", "refused", "sent"]
    fingerprint = retried.request_fingerprint
    assert record_keys(cache) == sorted([answered, fingerprint, f"{fingerprint}.retry-1"])
    return {"answered": answered, "retried": fingerprint}


@pytest.mark.parametrize("mode", ["sqlite", "auto"])
def test_an_old_cache_replays_under_sqlite_with_no_call_and_nothing_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    cache = tmp_path / "model-cache"
    _legacy_cache(cache, monkeypatch)
    before = _files(cache)
    _mode(monkeypatch, mode)
    calls: list[str] = []
    client = _client(cache, _counting(calls), budget=0, retry_failed=True)

    answered, retried = _ask(client, "answered"), _ask(client, "retried")

    assert client.backend_kind == "sqlite" and calls == []
    assert answered.cache_hit and retried.cache_hit and client.cache_hit_count == 2
    assert {name: data for name, data in _files(cache).items() if "sqlite" not in name} == before
    # Nothing is copied into the db: records, responses and contexts are read where they are.
    assert [_count(cache, table) for table in ("requests", "responses", "contexts")] == [0, 0, 0]
    assert _count(cache, "claims") == 0


def test_a_damaged_old_entry_is_called_again_once_and_replaced_by_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "model-cache"
    fingerprints = _legacy_cache(cache, monkeypatch)
    # Asynchronous flushes that failed (ADR 0029): the one response both success records name
    # is truncated, and the first call's record is torn.
    (response,) = (cache / "responses").iterdir()
    response.write_bytes(_reply()[:20])
    (cache / "requests" / f"{fingerprints['answered']}.json").write_bytes(b"{torn")
    torn = _files(cache)
    _mode(monkeypatch, "sqlite")
    calls: list[str] = []

    with recording_repairs() as repairs:
        client = _client(cache, _counting(calls), budget=5, retry_failed=True)
        answered, retried = _ask(client, "answered"), _ask(client, "retried")

    # The torn record is called again once; its response, now a db row, also answers the
    # ``.retry-1`` record that names it. The old files stay exactly as they were.
    assert calls == ["sent"] and client.repaired_count == 1
    assert repairs == Counter({"model_cache": 1})
    assert not answered.cache_hit and retried.cache_hit
    assert {name: data for name, data in _files(cache).items() if "sqlite" not in name} == torn
    assert [_count(cache, table) for table in ("requests", "responses")] == [1, 1]
    replay = _client(cache, _counting(calls), budget=0, retry_failed=True)
    assert all(_ask(replay, prompt).cache_hit for prompt in ("answered", "retried"))
    assert calls == ["sent"]


# ---- transactions and the writer lease --------------------------------------------------------


def _count_writer_leases(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    taken: Counter[str] = Counter()
    real = lease.acquire_lease

    def counting(base: Path, *args: object, **kwargs: object) -> int | None:
        taken[base.name] += 1
        return real(base, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lease, "acquire_lease", counting)
    return taken


def test_one_transaction_per_write_and_none_for_a_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "sqlite")
    cache = tmp_path / "model-cache"
    taken = _count_writer_leases(monkeypatch)
    client = _client(cache, _counting([]))

    for index in range(3):
        _ask(client, f"prompt {index}")
    # One to create the tables, then claim, context, response, record, release: five commits
    # per live call.
    assert taken == Counter({"model-cache.sqlite.writer": 1 + 15})
    assert not tuple(cache.glob("*.writer*"))

    taken.clear()
    # Opening an existing db and replaying from it writes nothing at all.
    replay = _client(cache, _counting([]), budget=0)
    assert all(_ask(replay, f"prompt {index}").cache_hit for index in range(3))
    assert taken == Counter()


def _child(script: str, env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=_ROOT,
        env={**os.environ, **(env or {})},
    )


_CHILD_CALLS = """
    import json, sys, time
    from pathlib import Path
    from pydantic import BaseModel, ConfigDict, SecretStr
    from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
    from ragspine.common.evidence.providers.providers import LLMConfig

    class _Answer(BaseModel):
        model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
        answer: str

    def sender(url, *, api_key, payload, timeout):
        time.sleep(0.002)
        content = json.dumps({{"answer": "ok"}})
        return json.dumps(
            {{"choices": [{{"finish_reason": "stop", "message": {{"content": content}}}}]}}
        ).encode()

    client = JsonCompletionClient(
        LLMConfig(api_key=SecretStr("k"), base_url="https://example.invalid", model="m"),
        cache_dir=Path({cache!r}),
        max_live_calls=100,
        sender=sender,
    )
    for index in range({count}):
        client.complete_text_json(
            task="wiring", prompt="{tag} " + str(index), response_model=_Answer
        )
    assert client.backend_kind == "sqlite", client.backend_kind
    """


def test_two_processes_share_one_cache_by_taking_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root-level answer cache: two live processes write one db at once; neither is
    locked out (a process-long writer lease would make the second fail ``store_busy``)."""
    _mode(monkeypatch, "sqlite")
    cache = tmp_path / "model-cache"
    children = [
        _child(_CHILD_CALLS.format(cache=str(cache), count=30, tag=tag)) for tag in ("a", "b")
    ]
    assert [child.wait(timeout=120) for child in children] == [0, 0]
    assert len(record_keys(cache)) == 60 and claims(cache) == []
    assert not tuple(cache.glob("*.writer*"))


def test_two_threads_with_their_own_clients_share_one_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "sqlite")
    cache = tmp_path / "model-cache"
    calls: list[str] = []
    clients = [_client(cache, _counting(calls), budget=40) for _ in range(2)]

    def run(pair: tuple[int, JsonCompletionClient]) -> int:
        number, client = pair
        for index in range(20):
            _ask(client, f"{number}-{index}")
        return client.live_call_count

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(run, enumerate(clients))) == [20, 20]
    assert len(calls) == 40 and len(record_keys(cache)) == 40 and claims(cache) == []


# ---- crashes --------------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="pid liveness is probed on POSIX only")
def test_a_process_killed_inside_a_transaction_blocks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "sqlite")
    cache = tmp_path / "model-cache"
    child = _child(
        f"""
        import os
        from pathlib import Path
        from ragspine.common.evidence.object_backend.sqlite import SqliteModelCacheBackend

        backend = SqliteModelCacheBackend(Path({str(cache)!r}))
        with backend.transaction():
            backend._core.connection().execute("INSERT INTO meta VALUES ('half', 'written')")
            os._exit(9)
        """
    )
    assert child.wait(timeout=60) == 9
    assert tuple(cache.glob("*.writer"))  # its lease is left behind, its pid is gone

    calls: list[str] = []
    assert not _ask(_client(cache, _counting(calls))).cache_hit
    assert calls == ["sent"] and not tuple(cache.glob("*.writer*"))
    with closing(sqlite3.connect(cache / "model-cache.sqlite")) as connection:
        assert connection.execute("SELECT count(*) FROM meta WHERE key = 'half'").fetchone() == (0,)


def test_a_torn_wal_tail_loses_only_the_last_call_which_is_simply_made_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "sqlite")
    cache, copy = tmp_path / "model-cache", tmp_path / "after-crash"
    calls: list[str] = []
    client = _client(cache, _counting(calls))
    _ask(client, "first")
    _ask(client, "second")
    wal = cache / "model-cache.sqlite-wal"
    before_third = wal.stat().st_size
    _ask(client, "third")
    assert wal.stat().st_size > before_third
    # The machine stops while the third call's frames are being flushed: copy the db and a WAL
    # cut inside its first frame (no -shm: sqlite rebuilds the index from the WAL).
    copy.mkdir()
    shutil.copyfile(cache / "model-cache.sqlite", copy / "model-cache.sqlite")
    (copy / "model-cache.sqlite-wal").write_bytes(wal.read_bytes()[: before_third + 100])

    again = _client(copy, _counting(calls))
    results = [_ask(again, prompt) for prompt in ("first", "second", "third")]

    assert [result.cache_hit for result in results] == [True, True, False]
    assert calls == ["sent"] * 4 and again.live_call_count == 1
    with closing(sqlite3.connect(copy / "model-cache.sqlite")) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_a_damaged_db_is_set_aside_rebuilt_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "sqlite")
    cache = tmp_path / "model-cache"
    calls: list[str] = []
    first = _client(cache, _counting(calls))
    _ask(first)
    first.backend.close()
    (cache / "model-cache.sqlite").write_bytes(b"not a database" * 100)

    with recording_repairs() as repairs:
        again = _client(cache, _counting(calls))
        result = _ask(again)

    assert not result.cache_hit and calls == ["sent", "sent"]
    assert repairs == Counter({"store_db": 1})
    assert len(tuple(cache.glob("model-cache.sqlite.corrupt-*"))) == 1
    assert _ask(_client(cache, _counting(calls), budget=0)).cache_hit


# ---- end to end: a store written by the files layout reruns under sqlite --------------------


@pytest.fixture
def _isolated_inline_index() -> Iterator[None]:
    document_store._INLINE_INDEXES.clear()
    yield
    document_store._INLINE_INDEXES.clear()


@pytest.mark.usefixtures("_isolated_inline_index")
def test_a_folder_ingested_with_files_recomputes_under_sqlite_from_its_old_model_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every stage result is dropped, so each model call is looked up again: all of them are
    answered from the per-PDF ``requests`` / ``responses`` files the first run wrote, with no
    live call, no repair, the same published id, and those files left as they were."""
    tasks = lite_env(monkeypatch)
    _mode(monkeypatch, "files")
    mixed_folder(tmp_path)
    (first,) = run_mode(tmp_path, "full").documents
    assert first.publication is not None
    assert first.publication.published_processing_id == FULL_PUBLISHED_ID
    assert sum(tasks.values()) > 0
    root = tmp_path / "ingestion"
    (cache,) = root.glob("*/processing/model-cache")
    written = _files(cache)
    for stage_cache in root.glob("*/*/stage-cache-sharded"):
        shutil.rmtree(stage_cache)
    document_store._INLINE_INDEXES.clear()
    tasks.clear()
    _mode(monkeypatch, "sqlite")
    replayed: list[str] = []
    real_record = FileModelCacheBackend.record

    def legacy_record(self: FileModelCacheBackend, key: str) -> bytes | None:
        found = real_record(self, key)
        if found is not None:
            replayed.append(key)
        return found

    monkeypatch.setattr(FileModelCacheBackend, "record", legacy_record)

    (again,) = run_mode(tmp_path, "full").documents

    assert again.status == "published" and again.publication is not None
    assert again.publication.published_processing_id == FULL_PUBLISHED_ID
    assert (sum(tasks.values()), again.live_calls, again.storage_repairs) == (0, 0, {})
    assert {name: data for name, data in _files(cache).items() if "sqlite" not in name} == written
    assert replayed  # the stages really looked their calls up again ...
    assert _count(cache, "requests") == 0  # ... and every one was a replay of an old file
