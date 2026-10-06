"""不存在才创建的文件落位: 优先硬链接, 文件系统不支持硬链接时回退为 rename.

受限 FUSE 挂载 (如 Databricks 的 Workspace files / Unity Catalog volumes) 上 ``os.link``
会以 EPERM 等失败, 目录 fsync 也常以 EINVAL 失败; 普通的创建 / 顺序写 / rename 仍可用.
"""

import errno
import os
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

# ADR 0029: a flat directory of hash-named files (``objects/sha256``, ``stage-cache``) is
# superseded by a sibling ``<name>-sharded/<first two hex>/<name>``. New files are written only
# there, so a legacy flat directory that is already full (Databricks Workspace files: 10 000
# children per folder) is never written again; reads look in the sharded place, then the flat one.
SHARDED_SUFFIX = "-sharded"
_SHARD = re.compile(r"[0-9a-f]{2}")


def sharded_directory(flat: Path) -> Path:
    return flat.with_name(flat.name + SHARDED_SUFFIX)


def sharded_path(flat: Path, name: str) -> Path:
    """Where a file named ``name`` of the flat directory ``flat`` is written now."""
    if _SHARD.fullmatch(name[:2]) is None:
        raise ValueError("A sharded file name starts with two lowercase hex digits")
    return sharded_directory(flat) / name[:2] / name


def stored_path(flat: Path, name: str) -> Path | None:
    """The existing file of that name, sharded place first, then the legacy flat place."""
    for path in (sharded_path(flat, name), flat / name):
        if path.is_file():
            return path
    return None


def read_stored(flat: Path, name: str) -> tuple[Path, bytes]:
    """Read the file of that name (sharded first, then flat); ``FileNotFoundError`` if neither."""
    try:
        path = sharded_path(flat, name)
        return path, path.read_bytes()
    except FileNotFoundError:
        path = flat / name
        return path, path.read_bytes()


def stored_names(flat: Path) -> list[str]:
    """Every file name in either layout, sorted and without duplicates."""
    names = {path.name for path in flat.iterdir() if path.is_file()} if flat.is_dir() else set()
    sharded = sharded_directory(flat)
    if sharded.is_dir():
        names.update(
            path.name
            for shard in sharded.iterdir()
            if shard.is_dir()
            for path in shard.iterdir()
            if path.is_file()
        )
    return sorted(names)


_REPAIRS: ContextVar[tuple[Counter[str], ...]] = ContextVar("storage_repairs", default=())


@contextmanager
def recording_repairs() -> Iterator[Counter[str]]:
    """Count, by kind, the damaged stored entries repaired or recomputed inside this scope."""
    counter: Counter[str] = Counter()
    token = _REPAIRS.set((*_REPAIRS.get(), counter))
    try:
        yield counter
    finally:
        _REPAIRS.reset(token)


def note_repair(kind: str) -> None:
    """One damaged entry (missing, empty, truncated or not its digest) was repaired / recomputed."""
    for counter in _REPAIRS.get():
        counter[kind] += 1


def replace_file(temporary: Path, target: Path) -> None:
    """Put the fsynced ``temporary`` in place of a damaged ``target``, then read it back."""
    content = temporary.read_bytes()
    os.replace(temporary, target)
    if target.read_bytes() != content:
        raise OSError(errno.EIO, "Replaced file does not read back as written", str(target))


def _codes(*names: str) -> frozenset[int]:
    return frozenset(getattr(errno, name) for name in names if hasattr(errno, name))


# "这个文件系统做不了硬链接"一类; EACCES / ENOSPC / EROFS / EIO 等真实故障不在此列, 照常上抛.
_LINK_UNSUPPORTED = _codes("EPERM", "ENOTSUP", "EOPNOTSUPP", "ENOSYS", "EXDEV")
# "这个文件系统不支持目录 fsync"一类.
_DIRECTORY_FSYNC_UNSUPPORTED = _codes("EINVAL", "EPERM", "ENOTSUP", "EOPNOTSUPP", "ENOSYS")


def link_new_file(temporary: Path, target: Path) -> None:
    """把已写完并 fsync 的同目录 ``temporary`` 以 ``target`` 名发布, 已存在则抛 ``FileExistsError``.

    支持硬链接时就是 ``os.link``: 原子, 先到先得, ``temporary`` 原样留给调用方删除.
    不支持时先判存在再 ``os.replace``: 读者仍只会看到完整文件, 但"判存在"与 rename 之间
    不是原子的 -- 同名不同内容的并发写入者里, 后 rename 者会覆盖先到者. 落位后读回比对,
    发现已被别人覆盖则同样抛 ``FileExistsError``, 交给调用方按其冲突语义处理.
    回退路径下 ``temporary`` 已被 rename 走, 调用方须以 ``missing_ok=True`` 清理.
    """
    try:
        os.link(temporary, target)
        return
    except OSError as error:
        if error.errno not in _LINK_UNSUPPORTED:
            raise
    if target.exists():
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
    content = temporary.read_bytes()
    os.replace(temporary, target)
    if target.read_bytes() != content:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))


def fsync_directory(directory: Path) -> None:
    """fsync 目录让新建的目录项落盘; 文件系统不支持目录 fsync 时跳过, 其余错误照抛.

    只用在目录 fsync 属于持久性加固, 而非正确性所必需的地方.
    """
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in _DIRECTORY_FSYNC_UNSUPPORTED:
            raise
    finally:
        os.close(descriptor)
