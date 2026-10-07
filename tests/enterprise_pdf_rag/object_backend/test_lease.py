"""lease.py 与 json_completion 原 claim 逻辑的判定等价(含 legacy 空 claim 的 mtime 规则)。

PR-3 起 json_completion 只经后端走 lease.py;原 claim 家族冻结在 ``legacy_model_cache.py``,
``_expired`` 仍在 json_completion。这里把两边喂同样的输入,断言同样的判定。
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from time import time

import pytest

from ragspine.common.evidence.object_backend import lease
from ragspine.common.evidence.object_backend.files import CLAIM_FORMAT
from ragspine.common.evidence.providers import json_completion
from tests.enterprise_pdf_rag.object_backend import legacy_model_cache as legacy
from tests.enterprise_pdf_rag.object_backend.conftest import live_owner

NOW = 1_791_200_000.0


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(json_completion, "_wall_clock", lambda: NOW)
    monkeypatch.setattr(lease, "wall_clock", lambda: NOW)
    return NOW


def test_claim_format_constant_matches_json_completion() -> None:
    assert CLAIM_FORMAT == json_completion.CLAIM_FORMAT
    assert lease.LEGACY_LEASE_SECONDS == json_completion.LEGACY_CLAIM_LEASE_SECONDS


def _current(**overrides: object) -> bytes:
    document: dict[str, object] = {
        "claim": CLAIM_FORMAT,
        "request_fingerprint": "f" * 64,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "process": "someone-else",
        "created_at": NOW - 10,
        "lease_seconds": 300,
    }
    document.update(overrides)
    return json.dumps(document, sort_keys=True).encode()


def _dead_pid() -> int:
    probe = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(probe.stdout.strip())


@pytest.mark.parametrize(
    ("content", "modified"),
    [
        (_current(), NOW - 10),  # 别进程、本机、pid 活着、租约内
        (_current(created_at=NOW - 10_000), NOW - 10_000),  # 租约过期
        (_current(host="elsewhere", created_at=NOW - 10_000), NOW - 10_000),  # 异机,只看租约
        (_current(host="elsewhere"), NOW - 10),  # 异机,租约内
        (_current(process=json_completion._PROCESS_TOKEN), NOW - 10),  # 本进程自己的
        (_current(created_at=None), NOW - 10),  # 字段不可用 → legacy mtime 规则
        (_current(created_at=None), NOW - 1_000),
        (_current(lease_seconds="300"), NOW - 1_000),  # 类型不对 → legacy mtime 规则
        (b"f" * 64, NOW - 10),  # legacy:只有指纹,新 mtime
        (b"f" * 64, NOW - 901),  # legacy:过了 900 s
        (b"", NOW - 899),  # legacy 空 claim,899 s:还活着
        (b"", NOW - 901),  # legacy 空 claim,901 s:过期
        (b"\xff not json", NOW - 10),
        (b"\xff not json", NOW - 100_000),
        (json.dumps({"claim": "other-format"}).encode(), NOW - 901),
    ],
)
def test_expiry_judgment_matches_json_completion(
    frozen_clock: float, content: bytes, modified: float
) -> None:
    ours = lease.lease_expired(
        content,
        modified,
        claim_format=CLAIM_FORMAT,
        process_token=json_completion._PROCESS_TOKEN,
    )
    theirs = json_completion._expired(content, modified)
    assert ours == theirs


def test_dead_pid_on_this_host_matches(frozen_clock: float) -> None:
    content = _current(pid=_dead_pid())
    assert json_completion._expired(content, NOW - 10) is True
    assert (
        lease.lease_expired(
            content,
            NOW - 10,
            claim_format=CLAIM_FORMAT,
            process_token=json_completion._PROCESS_TOKEN,
        )
        is True
    )


def test_acquire_release_files_match_json_completion(tmp_path: Path) -> None:
    fingerprint = "a" * 64
    record_a = tmp_path / "a" / "requests" / f"{fingerprint}.json"
    record_a.parent.mkdir(parents=True)
    base_b = tmp_path / "b" / "requests" / f"{fingerprint}.json.claim"

    assert legacy._claim_request(record_a, fingerprint, 300) == 0
    owner = live_owner(300)
    content = lease.owner_payload(CLAIM_FORMAT, owner, extra={"request_fingerprint": fingerprint})
    assert lease.acquire_lease(base_b, content, claim_format=CLAIM_FORMAT) == 0

    claim_a = record_a.with_suffix(".json.claim")
    assert claim_a.is_file() and base_b.is_file()
    assert set(json.loads(claim_a.read_bytes())) == set(json.loads(base_b.read_bytes()))

    # 持有者还活着:一边抛 request_in_progress_or_uncertain,一边 None —— 同一个判定。
    with pytest.raises(json_completion.JsonCompletionError, match="request_in_progress"):
        legacy._claim_request(record_a, fingerprint, 300)
    assert lease.acquire_lease(base_b, content, claim_format=CLAIM_FORMAT) is None

    legacy._release_claims(record_a)
    lease.release_lease(base_b)
    assert not claim_a.exists() and not base_b.exists()


def test_legacy_empty_claim_blocks_then_is_taken_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """legacy 空 claim:900 s 内两边都挡住;过了之后两边都以 .takeover-1 接管。"""
    fingerprint = "a" * 64
    record_a = tmp_path / "a" / "requests" / f"{fingerprint}.json"
    record_a.parent.mkdir(parents=True)
    base_b = tmp_path / "b" / "requests" / f"{fingerprint}.json.claim"
    base_b.parent.mkdir(parents=True)
    claim_a = record_a.with_suffix(".json.claim")
    claim_a.write_bytes(b"")
    base_b.write_bytes(b"")

    with pytest.raises(json_completion.JsonCompletionError, match="request_in_progress"):
        legacy._claim_request(record_a, fingerprint, 300)
    content = lease.owner_payload(CLAIM_FORMAT, live_owner(300))
    assert lease.acquire_lease(base_b, content, claim_format=CLAIM_FORMAT) is None

    late = time() + lease.LEGACY_LEASE_SECONDS + 60
    monkeypatch.setattr(json_completion, "_wall_clock", lambda: late)
    monkeypatch.setattr(lease, "wall_clock", lambda: late)
    assert legacy._claim_request(record_a, fingerprint, 300) == 1
    assert lease.acquire_lease(base_b, content, claim_format=CLAIM_FORMAT) == 1
    assert claim_a.with_name(claim_a.name + ".takeover-1").is_file()
    assert base_b.with_name(base_b.name + ".takeover-1").is_file()
