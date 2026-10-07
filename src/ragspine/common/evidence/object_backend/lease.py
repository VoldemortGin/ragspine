"""O_EXCL 写者租约:持有者 JSON + 租约过期 + 接管代次(从 ADR 0023 的 claim 逻辑抽取)。

与 ``providers/json_completion.py`` 的 ``_claim_*`` 家族判定等价(由
``tests/enterprise_pdf_rag/object_backend/test_lease.py`` 钉死);原实现**原样保留**,
PR-3 才把模型缓存切到这里。本模块同时服务 ``SqliteBackend`` 的 ``store.sqlite.writer``
跨进程写者互斥(设计稿 §4:不信任 FUSE 上的 sqlite 文件锁)。

租约文件从不含 prompt / key / 正文,只含持有者身份与租期。
"""

import json
import os
import socket
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from time import time

from ragspine.common.evidence.file_placement import fsync_directory
from ragspine.common.evidence.object_backend.protocol import ClaimOwner

# 旧格式(只有指纹或空文件)的租约按 mtime 判定,阈值与 json_completion 一致:
# 比最长的现行租约(_claim_lease(180) = 840 s)多出一分钟有余。
LEGACY_LEASE_SECONDS = 900
# 本进程唯一:pid 可能被后来的进程复用。与 json_completion._PROCESS_TOKEN 同机制
# (各自独立生成:两个 token 标识的都是"本进程",跨模块比较只看"是不是自己")。
PROCESS_TOKEN = uuid.uuid4().hex
# 租约时钟(epoch 秒);测试注入的缝。
wall_clock: Callable[[], float] = time


def current_owner(claim_format: str, lease_seconds: int) -> ClaimOwner:
    """以本进程为持有者的一份 ``ClaimOwner``;``claim_format`` 仅作签名提示,不进字段。"""
    del claim_format
    return ClaimOwner(
        host=socket.gethostname(),
        pid=os.getpid(),
        process=PROCESS_TOKEN,
        created_at=round(wall_clock(), 3),
        lease_seconds=lease_seconds,
    )


def owner_payload(
    claim_format: str, owner: ClaimOwner, extra: Mapping[str, str] | None = None
) -> bytes:
    """租约文件 / claims 行的内容:持有者身份 + 租期(+ 调用方的少量标识字段)。"""
    document: dict[str, object] = {
        "claim": claim_format,
        "host": owner.host,
        "pid": owner.pid,
        "process": owner.process,
        "created_at": owner.created_at,
        "lease_seconds": owner.lease_seconds,
        **(dict(extra) if extra else {}),
    }
    return json.dumps(document, sort_keys=True).encode()


def _pid_alive(pid: int) -> bool:
    """``pid`` 是否还是本机上在跑的进程;不确定按活着算(与 json_completion 相同)。"""
    if os.name != "posix" or pid <= 0:
        # Windows 上 os.kill(pid, 0) 会真的发信号,绝不探测。
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def lease_expired(
    content: bytes,
    modified: float,
    *,
    claim_format: str,
    process_token: str = PROCESS_TOKEN,
    legacy_lease_seconds: int = LEGACY_LEASE_SECONDS,
) -> bool:
    """该租约背后的尝试是否确定已结束(持有者死了或租约到期)。

    判定与 ``json_completion._expired`` 逐条等价(ADR 0023):
    现行格式、本机、非本进程、pid 已不在 → 立即过期;``created_at + lease_seconds``
    已过 → 过期;旧格式 / 空 / 不可解析 / 字段不可用 → 只看 mtime 与
    ``legacy_lease_seconds``。
    """
    now = wall_clock()
    try:
        owner = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        owner = None
    if not isinstance(owner, dict) or owner.get("claim") != claim_format:
        return now - modified > legacy_lease_seconds
    pid, created, lease = owner.get("pid"), owner.get("created_at"), owner.get("lease_seconds")
    if (
        owner.get("process") != process_token
        and owner.get("host") == socket.gethostname()
        and type(pid) is int
        and not _pid_alive(pid)
    ):
        return True
    if not isinstance(created, int | float) or type(lease) is not int or lease <= 0:
        return now - modified > legacy_lease_seconds
    return now - created > lease


def write_lease(path: Path, content: bytes) -> bool:
    """以 ``content`` 排他地、持久地创建 ``path``;已存在 → ``False``。"""
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


def generation_path(base: Path, generation: int = 0) -> Path:
    """第 0 代就是 ``base`` 自己;第 n 代是 ``<base>.takeover-<n>``(ADR 0023 §3)。"""
    return base if generation == 0 else base.with_name(f"{base.name}.takeover-{generation}")


def latest_generation(base: Path) -> int:
    """当前存在的最高接管代次(0 = 从未被接管);按序探测,不列目录。"""
    generation = 0
    while generation_path(base, generation + 1).exists():
        generation += 1
    return generation


def acquire_lease(
    base: Path,
    content: bytes,
    *,
    claim_format: str,
    process_token: str = PROCESS_TOKEN,
    legacy_lease_seconds: int = LEGACY_LEASE_SECONDS,
) -> int | None:
    """取得(或接管)``base`` 名下的写者租约;返回接管代次(0 = 全新),拿不到 → ``None``。

    与 ``json_completion._claim_request`` 的流程等价,只是"拿不到"以 ``None`` 表达
    (那边抛 ``request_in_progress_or_uncertain``),由调用方决定错误语义。
    持有者中途消失(释放了)同样返回 ``None``:调用方应重查其记录 / 现状再来。
    """
    base.parent.mkdir(parents=True, exist_ok=True)
    if write_lease(generation_path(base), content):
        return 0
    generation = latest_generation(base)
    holder = generation_path(base, generation)
    try:
        held = holder.read_bytes()
        modified = holder.stat().st_mtime
    except FileNotFoundError:
        return None
    if not lease_expired(
        held,
        modified,
        claim_format=claim_format,
        process_token=process_token,
        legacy_lease_seconds=legacy_lease_seconds,
    ) or not write_lease(generation_path(base, generation + 1), content):
        return None
    return generation + 1


def release_lease(base: Path) -> None:
    """自最新代次起删除租约文件;删除失败无害(ADR 0023 §5)。"""
    for generation in range(latest_generation(base), -1, -1):
        with suppress(OSError):
            generation_path(base, generation).unlink(missing_ok=True)


def holder_process(base: Path) -> str | None:
    """当前最高代次持有者的进程 token;没有租约或不可解析 → ``None``。"""
    holder = generation_path(base, latest_generation(base))
    try:
        owner = json.loads(holder.read_bytes())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    process = owner.get("process") if isinstance(owner, dict) else None
    return process if isinstance(process, str) else None
