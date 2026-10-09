"""后端注册表:读设置 → 探测 → 返回实现(``make_*`` / Registry 工厂的缝元模式)。

``open_backend(root, kind, *, settings=None)``:

- ``kind="object"`` → 一个 store 根的 ``ObjectBackend``;
- ``kind="model-cache"`` → 一个模型缓存目录的 ``ModelCacheBackend``。

模式由 ``settings.object_store_backend`` 决定(``APP_OBJECT_STORE_BACKEND``):

- ``files``:直接返回文件布局实现,不探测;
- ``sqlite``:显式要求 sqlite;探测失败即抛 ``BackendUnavailable``(绝不静默回退);
- ``auto``(默认):探测可用 → sqlite,否则回退文件布局;
- ``staged``(opt-in,enterprise-pdf-rag ADR 0040):对象 store 的 db 在本地工作目录
  (``APP_OBJECT_STORE_STAGING_DIR``)读写、阶段结束整文件发布回 ``root``,进程内每个 root
  共享一个 ``StagedBackend``;工作目录探测失败即 ``BackendUnavailable``。模型缓存走文件布局。

本 PR(PR-1)只提供注册表;没有任何调用方读它——store / 模型缓存在 PR-2/3 接线。
"""

import tempfile
from pathlib import Path
from typing import Literal, overload

from ragspine.common.evidence.configs import Settings, get_settings
from ragspine.common.evidence.object_backend.files import FileBackend, FileModelCacheBackend
from ragspine.common.evidence.object_backend.probe import probe_directory
from ragspine.common.evidence.object_backend.protocol import (
    BackendUnavailable,
    ModelCacheBackend,
    ObjectBackend,
)
from ragspine.common.evidence.object_backend.sqlite import (
    SqliteBackend,
    SqliteModelCacheBackend,
)
from ragspine.common.evidence.object_backend.staged import acquire_staged

BackendRole = Literal["object", "model-cache"]


def external_media_types(settings: Settings) -> frozenset[str]:
    """``APP_OBJECT_STORE_EXTERNAL_MEDIA_TYPES`` 的解析:逗号分隔,空段忽略。"""
    return frozenset(
        part.strip()
        for part in settings.object_store_external_media_types.split(",")
        if part.strip()
    )


@overload
def open_backend(
    root: Path, kind: Literal["object"] = "object", *, settings: Settings | None = None
) -> ObjectBackend: ...


@overload
def open_backend(
    root: Path, kind: Literal["model-cache"], *, settings: Settings | None = None
) -> ModelCacheBackend: ...


def open_backend(
    root: Path, kind: BackendRole = "object", *, settings: Settings | None = None
) -> ObjectBackend | ModelCacheBackend:
    """按设置为 ``root`` 打开一个后端;见模块 docstring 的三种模式。"""
    settings = get_settings() if settings is None else settings
    mode = settings.object_store_backend
    if mode == "files":
        return _files(root, kind)
    if mode == "staged":
        return _files(root, kind) if kind == "model-cache" else _staged(root, settings)
    result = probe_directory(root)
    if result.ok:
        return _sqlite(root, kind, settings)
    if mode == "sqlite":
        raise BackendUnavailable(
            "显式配置了 APP_OBJECT_STORE_BACKEND=sqlite,但该目录上的 sqlite 可用性探测"
            f"失败(失败码 {result.code})。不会静默回退:要继续使用文件布局,请把"
            " APP_OBJECT_STORE_BACKEND 设回 files(或删掉该设置用默认的 auto 自动回退)。"
        )
    return _files(root, kind)


def _files(root: Path, kind: BackendRole) -> ObjectBackend | ModelCacheBackend:
    if kind == "model-cache":
        return FileModelCacheBackend(root)
    return FileBackend(root)


def _sqlite(root: Path, kind: BackendRole, settings: Settings) -> ObjectBackend | ModelCacheBackend:
    if kind == "model-cache":
        return SqliteModelCacheBackend(root, synchronous=settings.object_store_synchronous)
    return SqliteBackend(
        root,
        synchronous=settings.object_store_synchronous,
        inline_max_bytes=settings.object_store_inline_max_bytes,
        external_media_types=external_media_types(settings),
        max_db_bytes=settings.object_store_max_db_bytes,
    )


def _staged(root: Path, settings: Settings) -> ObjectBackend:
    staging_root = settings.object_store_staging_dir
    if staging_root is None:
        staging_root = Path(tempfile.gettempdir()) / "ragspine-staged"
    staging_root = staging_root.expanduser()
    staging_root.mkdir(parents=True, exist_ok=True)
    result = probe_directory(staging_root)
    if not result.ok:
        raise BackendUnavailable(
            "APP_OBJECT_STORE_BACKEND=staged 的本地工作目录上 sqlite 可用性探测失败"
            f"(失败码 {result.code})。请把 APP_OBJECT_STORE_STAGING_DIR 指向本地盘"
            "(如 /local_disk0/ragspine-staged),或把 APP_OBJECT_STORE_BACKEND 设回 files / auto。"
        )
    return acquire_staged(
        root,
        staging_root=staging_root,
        synchronous=settings.object_store_synchronous,
        inline_max_bytes=settings.object_store_inline_max_bytes,
        external_media_types=external_media_types(settings),
        max_db_bytes=settings.object_store_max_db_bytes,
    )
