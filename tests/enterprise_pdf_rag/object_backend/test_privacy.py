"""隐私:后端的异常文案、探测结果与租约文件从不含对象正文 / prompt / 答案。

与仓库的 privacy-aware trace 不变量同一取向:code / 计数 / 时长可以出现,内容不行。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from ragspine.common.evidence.object_backend import lease
from ragspine.common.evidence.object_backend.files import CLAIM_FORMAT
from ragspine.common.evidence.object_backend.probe import clear_probe_cache, probe_directory
from ragspine.common.evidence.object_backend.protocol import (
    DamagedEntry,
    ObjectBackend,
    StageEntry,
    StoreBusy,
    StoreConflict,
)
from ragspine.common.evidence.object_backend.sqlite import SqliteBackend
from tests.enterprise_pdf_rag.object_backend.conftest import (
    damage_object,
    live_owner,
    sha,
    stage_envelope,
)

SECRET = "TOP-SECRET-ANSWER-4242"


def test_damaged_object_error_never_quotes_the_bytes(
    backend: ObjectBackend, backend_kind: str, store_root: Path
) -> None:
    data = f"evidence: {SECRET}".encode()
    digest = sha(data)
    backend.put_object(digest, data, "text/plain")
    damage_object(backend_kind, store_root, digest, garbage=f"damaged {SECRET}".encode())
    with pytest.raises(DamagedEntry) as caught:
        backend.get_object(digest)
    assert SECRET not in str(caught.value)


def test_stage_conflict_error_never_quotes_the_envelope(backend: ObjectBackend) -> None:
    fingerprint = "b" * 64
    backend.put_stage_entry(fingerprint, stage_envelope(SECRET.encode(), fingerprint))
    with pytest.raises(StoreConflict) as caught:
        backend.put_stage_entry(
            fingerprint, stage_envelope((SECRET + "-other").encode(), fingerprint)
        )
    assert SECRET not in str(caught.value)


def test_probe_failure_codes_carry_no_path_and_match_the_code_shape(tmp_path: Path) -> None:
    clear_probe_cache()

    def refuse(_path: str) -> sqlite3.Connection:
        raise sqlite3.OperationalError(f"unable to open {tmp_path}/x")

    result = probe_directory(tmp_path, refresh=True, connect=refuse)
    assert result.ok is False
    assert result.code is not None and result.code.startswith("sqlite_")
    assert result.code.replace("_", "").isalpha()  # 只有步骤码
    assert str(tmp_path) not in repr(result)


def test_store_busy_is_a_bare_code(tmp_path: Path) -> None:
    owner = live_owner(3600)
    foreign = lease.owner_payload(
        "object-store-writer-v1",
        type(owner)(owner.host, owner.pid, "foreign-token", owner.created_at, 3600),
    )
    (tmp_path / "store").mkdir(parents=True)
    lease.write_lease(tmp_path / "store" / "store.sqlite.writer", foreign)
    backend = SqliteBackend(tmp_path / "store")
    with pytest.raises(StoreBusy) as caught:
        backend.put_object(sha(b"x"), b"x", "text/plain")
    assert str(caught.value) == "store_busy"
    backend.close()


def test_claim_and_lease_files_hold_identity_only(tmp_path: Path) -> None:
    owner = live_owner(300)
    content = lease.owner_payload(CLAIM_FORMAT, owner, extra={"request_fingerprint": "a" * 64})
    base = tmp_path / "claim"
    lease.write_lease(base, content)
    document = json.loads(base.read_bytes())
    assert set(document) == {
        "claim",
        "request_fingerprint",
        "host",
        "pid",
        "process",
        "created_at",
        "lease_seconds",
    }


def test_stage_entry_damage_message_is_fixed(backend: ObjectBackend, store_root: Path) -> None:
    fingerprint = "c" * 64
    entry = stage_envelope(SECRET.encode(), fingerprint)
    backend.put_stage_entry(fingerprint, StageEntry(entry.envelope_digest, entry.envelope))
    # 指针/行完好时无异常;这里只钉损坏文案的形状(具体注入见一致性测试)。
    read = backend.stage_entry(fingerprint)
    assert read is not None
    with pytest.raises(ValueError, match=r"^Stage envelope does not hash to its digest line$"):
        backend.put_stage_entry(fingerprint, StageEntry("d" * 64, entry.envelope))
