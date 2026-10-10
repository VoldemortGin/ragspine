"""两后端一致性:同一组操作在 files 与 sqlite 上给出同样的可观察行为。

digest / 指纹 / 信封字节在两种后端下不变;冲突文案、损坏的拒绝与修复、指针原子性、
records、pin 漂移、事务重入、外置对象、legacy 读穿、zlib 往返都在这里双跑钉死。
"""

import hashlib
import json
import sqlite3
import zlib
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from ragspine.common.evidence.file_placement import recording_repairs
from ragspine.common.evidence.object_backend.files import FileModelCacheBackend
from ragspine.common.evidence.object_backend.protocol import (
    DamagedEntry,
    ModelCacheBackend,
    ObjectBackend,
    StageEntry,
    StoreConflict,
)
from ragspine.common.evidence.object_backend.sqlite import SqliteModelCacheBackend
from tests.enterprise_pdf_rag.object_backend.conftest import (
    DOCUMENT_SCOPE,
    damage_object,
    damage_stage_entry,
    db_file,
    expired_owner,
    live_owner,
    make_backend,
    make_model_cache,
    sha,
    stage_envelope,
)

FP = "b" * 64


# ---- 内容寻址对象 ---------------------------------------------------------------------


def test_put_get_roundtrip_and_first_writer(backend: ObjectBackend) -> None:
    data = b'{"k": "v"}' * 40
    digest = sha(data)
    assert backend.get_object(digest) is None
    assert backend.put_object(digest, data, "application/json") == "placed"
    assert backend.put_object(digest, data, "application/json") == "existing"
    assert backend.get_object(digest) == data
    assert backend.read_existing(digest) == data
    assert backend.object_names() == [digest]


def test_put_refuses_bytes_that_are_not_their_digest(backend: ObjectBackend) -> None:
    with pytest.raises(ValueError, match="digest"):
        backend.put_object("a" * 64, b"other bytes", "text/plain")


def test_damaged_object_refused_on_read_repaired_on_write(
    backend: ObjectBackend, backend_kind: str, store_root: Path
) -> None:
    data = b"evidence bytes " * 10
    digest = sha(data)
    backend.put_object(digest, data, "text/plain")
    damage_object(backend_kind, store_root, digest)
    with pytest.raises(DamagedEntry):
        backend.get_object(digest)
    with recording_repairs() as repairs:
        assert backend.put_object(digest, data, "text/plain") == "placed"
    assert repairs["object"] == 1
    assert backend.get_object(digest) == data


def test_verify_many_streams_and_refuses(
    backend: ObjectBackend, backend_kind: str, store_root: Path
) -> None:
    payloads = [f"payload-{index}".encode() * 20 for index in range(5)]
    digests = [sha(payload) for payload in payloads]
    for payload, digest in zip(payloads, digests, strict=True):
        backend.put_object(digest, payload, "text/plain")
    assert list(backend.verify_many(digests)) == list(zip(digests, payloads, strict=True))
    with pytest.raises(LookupError):
        list(backend.verify_many([sha(b"absent")]))
    damage_object(backend_kind, store_root, digests[2])
    with pytest.raises(DamagedEntry):
        list(backend.verify_many(digests))


# ---- stage-cache 条目 ------------------------------------------------------------------


def test_stage_entry_first_writer_wins_and_conflict_text(backend: ObjectBackend) -> None:
    artifact = b'{"result": 1}'
    backend.put_object(sha(artifact), artifact, "application/json")
    entry = stage_envelope(artifact, FP)
    assert backend.stage_entry(FP) is None
    assert backend.put_stage_entry(FP, entry) == "placed"
    assert backend.put_stage_entry(FP, entry) == "existing"
    read = backend.stage_entry(FP)
    assert read is not None and (read.envelope_digest, read.envelope) == (
        entry.envelope_digest,
        entry.envelope,
    )
    other_artifact = b'{"result": 2}'
    backend.put_object(sha(other_artifact), other_artifact, "application/json")
    other = stage_envelope(other_artifact, FP)
    with pytest.raises(StoreConflict, match="Conflicting immutable stage cache entry"):
        backend.put_stage_entry(FP, other)
    kept = backend.stage_entry(FP)
    assert kept is not None and kept.envelope == entry.envelope  # 首写的字节留下


def test_damaged_stage_entry_refused_then_replaced(
    backend: ObjectBackend, backend_kind: str, store_root: Path
) -> None:
    artifact = b'{"result": 3}'
    backend.put_object(sha(artifact), artifact, "application/json")
    entry = stage_envelope(artifact, FP)
    backend.put_stage_entry(FP, entry)
    damage_stage_entry(backend_kind, store_root, FP)
    with pytest.raises(DamagedEntry):
        backend.stage_entry(FP)
    assert backend.put_stage_entry(FP, entry, replace=True) == "placed"
    read = backend.stage_entry(FP)
    assert read is not None and read.envelope == entry.envelope


def test_stage_entry_product_roundtrip_reserved_for_amendment_2(backend: ObjectBackend) -> None:
    """Amendment 2 预留:产物内联在条目里,读回必须 hash 到信封里 artifact 的摘要。"""
    artifact = b'{"product": true}' * 30
    entry = stage_envelope(artifact, FP)
    inline = StageEntry(entry.envelope_digest, entry.envelope, artifact)
    assert backend.put_stage_entry(FP, inline) == "placed"
    read = backend.stage_entry(FP)
    assert read is not None and read.product == artifact
    with pytest.raises(ValueError, match="artifact digest"):
        backend.put_stage_entry("c" * 64, StageEntry(entry.envelope_digest, entry.envelope, b"x"))


# ---- legacy 三代布局的读穿 --------------------------------------------------------------


def _legacy_flat_object(root: Path, data: bytes) -> str:
    flat = root / "objects" / "sha256"
    flat.mkdir(parents=True, exist_ok=True)
    digest = sha(data)
    (flat / digest).write_bytes(data)
    return digest


def test_reads_through_all_three_stage_pointer_generations(
    backend: ObjectBackend, store_root: Path
) -> None:
    # 第 0 代:平铺目录里 digest-only 指针,信封是 store 对象(平铺布局)。
    artifact = b'{"generation": 0}'
    entry = stage_envelope(artifact, FP)
    _legacy_flat_object(store_root, entry.envelope)
    legacy_dir = store_root / "stage-cache"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / FP).write_bytes(entry.envelope_digest.encode() + b"\n")
    read = backend.stage_entry(FP)
    assert read is not None and read.envelope == entry.envelope and read.product is None

    # 第 1 代:分层目录里两行内联信封。
    fp1 = "c" * 64
    entry1 = stage_envelope(b'{"generation": 1}', fp1)
    sharded = store_root / "stage-cache-sharded" / fp1[:2]
    sharded.mkdir(parents=True, exist_ok=True)
    (sharded / fp1).write_bytes(entry1.envelope_digest.encode() + b"\n" + entry1.envelope + b"\n")
    read1 = backend.stage_entry(fp1)
    assert read1 is not None and read1.envelope == entry1.envelope and read1.product is None

    # 第 2 代(预留格式):第三段是内联产物。
    fp2 = "d" * 64
    product = b'{"generation": 2}'
    entry2 = stage_envelope(product, fp2)
    sharded2 = store_root / "stage-cache-sharded" / fp2[:2]
    sharded2.mkdir(parents=True, exist_ok=True)
    (sharded2 / fp2).write_bytes(
        entry2.envelope_digest.encode() + b"\n" + entry2.envelope + b"\n" + product
    )
    read2 = backend.stage_entry(fp2)
    assert read2 is not None and read2.envelope == entry2.envelope and read2.product == product


def test_reads_legacy_flat_object(backend: ObjectBackend, store_root: Path) -> None:
    data = b"flat layout bytes"
    digest = _legacy_flat_object(store_root, data)
    assert backend.get_object(digest) == data
    assert digest in backend.object_names()


# ---- 指针与 records ---------------------------------------------------------------------


def test_pointer_atomic_replacement_and_absence(backend: ObjectBackend) -> None:
    assert backend.pointer("current-processing") is None
    first, second = "1" * 64, "2" * 64
    backend.set_pointer("current-processing", first)
    assert backend.pointer("current-processing") == first
    backend.set_pointer("current-processing", second)
    assert backend.pointer("current-processing") == second
    with pytest.raises(ValueError, match="identifier"):
        backend.set_pointer("current-processing", "not-a-digest")
    assert backend.pointer("current-processing") == second


def test_records_are_mutable_last_writer_wins(backend: ObjectBackend) -> None:
    name = "document-tree/" + "e" * 64 + ".json"
    assert backend.record(name) is None
    backend.put_record(name, b'{"state": "deferred"}')
    backend.put_record(name, b'{"state": "succeeded"}')
    assert backend.record(name) == b'{"state": "succeeded"}'


# ---- pin 与漂移 -------------------------------------------------------------------------


def test_pin_hits_while_unchanged_and_sees_drift(
    backend: ObjectBackend, backend_kind: str, store_root: Path
) -> None:
    data = b"pinned bytes " * 10
    digest = sha(data)
    backend.put_object(digest, data, "text/plain")
    token = backend.pin(digest)
    assert backend.pin_unchanged(token)
    damage_object(backend_kind, store_root, digest, garbage=b"drifted to other bytes!!")
    assert not backend.pin_unchanged(token)
    with pytest.raises(LookupError):
        backend.pin(sha(b"absent"))


# ---- 事务(提交、重入;回滚语义是 sqlite 专项)-------------------------------------------


def test_transaction_commits_and_is_reentrant(backend: ObjectBackend) -> None:
    data = b"transactional"
    digest = sha(data)
    with backend.transaction(), backend.transaction():
        backend.put_object(digest, data, "text/plain")
        backend.set_pointer("current-manifest", digest)
    assert backend.get_object(digest) == data
    assert backend.pointer("current-manifest") == digest


# ---- 外置对象与 zlib 往返 ---------------------------------------------------------------


def test_external_media_type_stays_a_file(backend_kind: str, tmp_path: Path) -> None:
    backend = make_backend(backend_kind, tmp_path / "s")
    data = b"%PDF-1.7 fake pdf bytes " * 10
    digest = sha(data)
    backend.put_object(digest, data, "application/pdf")
    assert backend.get_object(digest) == data
    placed = tmp_path / "s" / "objects" / "sha256-sharded" / digest[:2] / digest
    assert placed.read_bytes() == data  # PDF 原件永远留文件系统
    backend.close()


def test_oversize_object_is_external(backend_kind: str, tmp_path: Path) -> None:
    kwargs = {} if backend_kind == "files" else {"inline_max_bytes": 64}
    backend = make_backend(backend_kind, tmp_path / "s", **kwargs)
    data = b"large object bytes " * 10
    digest = sha(data)
    backend.put_object(digest, data, "application/json")
    assert backend.get_object(digest) == data
    placed = tmp_path / "s" / "objects" / "sha256-sharded" / digest[:2] / digest
    assert placed.read_bytes() == data
    backend.close()


def test_zlib_roundtrip_keeps_bytes_and_digest(backend: ObjectBackend, backend_kind: str) -> None:
    data = json.dumps({"rows": list(range(500))}).encode()
    assert len(data) >= 1024
    digest = sha(data)
    backend.put_object(digest, data, "application/json")
    assert backend.get_object(digest) == data  # digest 是解压后字节的 sha256,往返不变
    if backend_kind == "sqlite":
        with closing(sqlite3.connect(backend.root / "store.sqlite")) as connection, connection:
            encoding, blob = connection.execute(
                "SELECT encoding, bytes FROM objects WHERE digest = ?", (digest,)
            ).fetchone()
        assert encoding == "zlib" and zlib.decompress(blob) == data
        assert hashlib.sha256(zlib.decompress(blob)).hexdigest() == digest


def test_an_uncompressed_row_of_a_compressible_type_still_reads(
    backend_kind: str, tmp_path: Path
) -> None:
    """压缩之前写下的行(encoding = raw 的 SVG / JSON)照样读得出,digest 不变。"""
    if backend_kind == "files":
        pytest.skip("文件布局没有 db 行")
    root = tmp_path / "s"
    backend = make_backend(backend_kind, root)
    seed = b"seed"
    backend.put_object(sha(seed), seed, "text/plain")  # 建库
    data = b"<svg>" + b"<g/>" * 2000 + b"</svg>"
    digest = sha(data)
    with closing(sqlite3.connect(db_file(backend_kind, root))) as connection, connection:
        connection.execute(
            "INSERT INTO objects (digest, byte_length, media_type, encoding, external, bytes,"
            " created_at) VALUES (?, ?, 'image/svg+xml', 'raw', 0, ?, 0)",
            (digest, len(data), data),
        )
        if backend_kind == "document":  # 文档 db:对象行共用,scope 成员表另记
            connection.execute(
                f"INSERT INTO {DOCUMENT_SCOPE}_object_refs (digest) VALUES (?)", (digest,)
            )
    assert backend.get_object(digest) == data
    assert backend.get_content(digest) == data
    assert backend.put_object(digest, data, "image/svg+xml") == "existing"  # 不改写旧行
    backend.close()


# ---- 模型缓存(双跑)--------------------------------------------------------------------


def test_model_cache_record_first_writer_wins(model_cache: ModelCacheBackend) -> None:
    key = "a" * 64
    record = b'{"request_fingerprint": "' + b"a" * 64 + b'", "response_digest": null}'
    assert model_cache.record(key) is None
    model_cache.put_record(key, record)
    model_cache.put_record(key, record)  # 同字节 no-op
    with pytest.raises(StoreConflict, match="cache_conflict"):
        model_cache.put_record(key, b'{"other": true}')
    model_cache.put_record(key, b'{"repaired": true}', replace_damaged=True)
    assert model_cache.record(key) == b'{"repaired": true}'


def test_model_cache_retry_record_key(model_cache: ModelCacheBackend) -> None:
    key = "a" * 64 + ".retry-1"
    model_cache.put_record(key, b'{"retry": 1}')
    assert model_cache.record(key) == b'{"retry": 1}'
    assert model_cache.record("a" * 64) is None


def test_model_cache_response_and_context_roundtrip(model_cache: ModelCacheBackend) -> None:
    body = json.dumps({"choices": ["x" * 2000]}).encode()
    digest = sha(body)
    assert model_cache.response(digest) is None
    model_cache.put_response(digest, body)
    model_cache.put_response(digest, body)
    assert model_cache.response(digest) == body
    with pytest.raises(ValueError, match="digest"):
        model_cache.put_response(digest, b"other")
    fingerprint = "f" * 64
    model_cache.put_context(fingerprint, b'{"payload": 1}')
    model_cache.put_context(fingerprint, b'{"payload": 2}')  # 首写胜出,静默
    assert model_cache.context(fingerprint) == b'{"payload": 1}'


def test_model_cache_has_records_only_once_a_record_is_written(
    model_cache: ModelCacheBackend,
) -> None:
    """``has_records`` 只看请求记录:claim / 响应 / 上下文都不算(没有记录就无可重放)。"""
    assert model_cache.has_records() is False
    owner = live_owner()
    assert model_cache.claim("7" * 64, owner) == 0
    model_cache.put_response(sha(b"{}"), b"{}")
    model_cache.put_context("6" * 64, b'{"payload": 1}')
    assert model_cache.has_records() is False
    model_cache.release("7" * 64, owner)
    model_cache.put_record("5" * 64 + ".retry-1", b'{"retry": 1}')
    assert model_cache.has_records() is True


def test_sqlite_model_cache_has_records_sees_legacy_request_files(tmp_path: Path) -> None:
    cache_dir = tmp_path / "model-cache"
    FileModelCacheBackend(cache_dir).put_record("4" * 64, b'{"legacy": true}')
    sqlite_cache = SqliteModelCacheBackend(cache_dir)
    try:
        assert sqlite_cache.has_records() is True
    finally:
        sqlite_cache.close()


def test_model_cache_claim_lifecycle(model_cache: ModelCacheBackend) -> None:
    key = "9" * 64
    owner = live_owner()
    assert model_cache.claim(key, owner) == 0
    assert model_cache.claim(key, owner) is None  # 活租约挡住(本进程也一样,ADR 0023)
    model_cache.release(key, owner)
    assert model_cache.claim(key, owner) == 0
    model_cache.release(key, owner)


def test_model_cache_expired_claim_is_taken_over(model_cache: ModelCacheBackend) -> None:
    key = "8" * 64
    assert model_cache.claim(key, expired_owner()) == 0
    follower = live_owner()
    assert model_cache.claim(key, follower) == 1  # 过期持有者被接管,代次 +1
    assert model_cache.claim(key, live_owner()) is None


def test_model_cache_renew_moves_the_holder_to_the_next_generation(
    model_cache: ModelCacheBackend,
) -> None:
    """ADR 0035:重试前续租 = 以下一代次重新持有;代次已被别人推进则续不上。"""
    key = "7" * 64
    owner = live_owner()
    assert model_cache.claim(key, owner) == 0
    assert model_cache.renew(key, live_owner(), 0) == 1
    assert model_cache.renew(key, live_owner(), 1) == 2
    assert model_cache.renew(key, live_owner(), 1) is None  # 第 1 代已不是当前持有者
    assert model_cache.claim(key, live_owner()) is None  # 续过的租约仍是活的
    model_cache.release(key, owner)
    assert model_cache.claim(key, owner) == 0


def test_model_cache_claimed_says_whether_a_claim_is_held(model_cache: ModelCacheBackend) -> None:
    key = "6" * 64 + ".retry-1"
    owner = live_owner()
    assert not model_cache.claimed(key)
    assert model_cache.claim(key, owner) == 0
    assert model_cache.claimed(key) and not model_cache.claimed("6" * 64)
    model_cache.release(key, owner)
    assert not model_cache.claimed(key)


def test_model_cache_claim_asks_the_given_judge_once_per_attempt(
    model_cache: ModelCacheBackend,
) -> None:
    """json_completion 传自己的 ``_expired``:每个调用方对当前持有者只判一次,判"已结束"
    才接管,判"还在跑"就拿不到——两后端一样。"""
    key = "5" * 64
    assert model_cache.claim(key, live_owner()) == 0
    asked: list[bytes] = []

    def judge(verdict: bool) -> Callable[[bytes, float], bool]:
        def expired(content: bytes, modified: float) -> bool:
            asked.append(content)
            return verdict

        return expired

    assert model_cache.claim(key, live_owner(), expired=judge(False)) is None
    assert model_cache.claim(key, live_owner(), expired=judge(True)) == 1
    assert len(asked) == 2
    assert all(json.loads(content)["request_fingerprint"] == key for content in asked)


def _plant_legacy_claim(cache_dir: Path, key: str, content: bytes, *, generation: int = 0) -> Path:
    base = cache_dir / "requests" / f"{key}.json.claim"
    base.parent.mkdir(parents=True, exist_ok=True)
    path = base if generation == 0 else base.with_name(f"{base.name}.takeover-{generation}")
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("kind", ["files", "sqlite", "staged", "document"])
def test_a_legacy_claim_file_is_judged_then_taken_over_at_its_next_generation(
    tmp_path: Path, kind: str
) -> None:
    """旧 ``.claim`` 文件(旧代码写的):还可能在跑就挡住;已结束则以其代次 + 1 接管,
    释放时连同旧文件一起删掉——sqlite 读穿旧文件,与文件布局逐例一致。"""
    key = "4" * 64
    cache_dir = tmp_path / "model-cache"
    _plant_legacy_claim(cache_dir, key, b"")
    dead = _plant_legacy_claim(cache_dir, key, b"", generation=1)
    cache = make_model_cache(kind, cache_dir)
    try:
        assert cache.claim(key, live_owner(), expired=lambda content, modified: False) is None
        assert cache.claimed(key)
        owner = live_owner()
        assert cache.claim(key, owner, expired=lambda content, modified: True) == 2
        assert cache.claim(key, live_owner()) is None  # 新持有者是本进程,租约内
        cache.release(key, owner)
        assert not dead.exists() and not tuple((cache_dir / "requests").iterdir())
        assert not cache.claimed(key)
    finally:
        cache.close()
