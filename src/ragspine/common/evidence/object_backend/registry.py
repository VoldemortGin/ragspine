"""后端注册表:读设置 → 探测 → 返回实现(``make_*`` / Registry 工厂的缝元模式)。

``open_backend(root, kind, *, settings=None)``:

- ``kind="object"`` → 一个 store 根的 ``ObjectBackend``;
- ``kind="model-cache"`` → 一个模型缓存目录的 ``ModelCacheBackend``。

模式由 ``settings.object_store_backend`` 决定(``APP_OBJECT_STORE_BACKEND``):

- ``files``:直接返回文件布局实现,不探测;
- ``sqlite``:显式要求 sqlite;探测失败即抛 ``BackendUnavailable``(绝不静默回退);
- ``auto``(默认):探测可用 → sqlite,否则回退文件布局——回退时每个目录一条 warning +
  一条 trace(``event=object_backend_fallback``、``failure_code``,只带探测失败码,不带路径);
- ``staged``(opt-in,enterprise-pdf-rag ADR 0044):对象 store 的 db 在本地工作目录
  (``APP_OBJECT_STORE_STAGING_DIR``)读写、阶段结束整文件发布回 ``root``,进程内每个 root
  共享一个 ``StagedBackend``;工作目录探测失败即 ``BackendUnavailable``。没显式设置
  ``APP_OBJECT_STORE_INLINE_MAX_BYTES`` 时内联上限是 8 MiB(ADR 0046)。模型缓存:父目录是本进程
  正在 staged 的 store(文档自己的 ``processing/model-cache``)→ ``StagedModelCacheBackend``,
  随那个 store 一起发布(ADR 0046);其余(根级答案缓存)走文件布局。
  ADR 0047:``<doc>/source``、``<doc>/processing`` 与 ``<doc>/processing/model-cache`` 解析到
  同一个 ``StagedDocument`` 的三个 scope(每份文档一个 ``document.sqlite``,PDF 也进库);
  别的 store 根仍是 ADR 0044 的每根一个 db。非 staged 模式遇到 ``document.sqlite`` →
  ``LayoutMismatch``(不当成空 store)。

本 PR(PR-1)只提供注册表;没有任何调用方读它——store / 模型缓存在 PR-2/3 接线。
"""

import logging
import tempfile
from pathlib import Path
from threading import Lock
from typing import Literal, overload

from ragspine.common.evidence.configs import Settings, get_settings
from ragspine.common.evidence.object_backend.files import FileBackend, FileModelCacheBackend
from ragspine.common.evidence.object_backend.probe import probe_directory
from ragspine.common.evidence.object_backend.protocol import (
    BackendUnavailable,
    LayoutMismatch,
    ModelCacheBackend,
    ObjectBackend,
)
from ragspine.common.evidence.object_backend.sqlite import (
    SqliteBackend,
    SqliteModelCacheBackend,
)
from ragspine.common.evidence.object_backend.staged import (
    DOCUMENT_DB_NAME,
    STAGED_INLINE_MAX_BYTES,
    STAGED_PDF_INLINE_MAX_BYTES,
    acquire_staged,
    acquire_staged_document,
    acquire_staged_model_cache,
    document_root_of,
)
from ragspine.common.observability.trace import emit_trace

BackendRole = Literal["object", "model-cache"]
_LOGGER = logging.getLogger(__name__)
# auto 回退已告警过的目录(进程内,每个目录一次)。
_WARNED_LOCK = Lock()
_WARNED: set[Path] = set()


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
    if mode != "staged":
        _refuse_document_db(root, kind)
    if mode == "files":
        return _files(root, kind)
    if mode == "staged":
        if kind == "model-cache":
            staged = acquire_staged_model_cache(root, synchronous=settings.object_store_synchronous)
            return _files(root, kind) if staged is None else staged
        return _staged(root, settings)
    result = probe_directory(root)
    if result.ok:
        return _sqlite(root, kind, settings)
    if mode == "sqlite":
        raise BackendUnavailable(
            "显式配置了 APP_OBJECT_STORE_BACKEND=sqlite,但该目录上的 sqlite 可用性探测"
            f"失败(失败码 {result.code})。不会静默回退:要继续使用文件布局,请把"
            " APP_OBJECT_STORE_BACKEND 设回 files(或删掉该设置用默认的 auto 自动回退)。"
        )
    _note_fallback(root, result.code)
    return _files(root, kind)


def _refuse_document_db(root: Path, kind: BackendRole) -> None:
    """非 staged 模式打开 staged 写的单文件文档(ADR 0047)→ ``LayoutMismatch``,不当成空 store。"""
    located = document_root_of(root, kind)
    if located is not None and (located[0] / DOCUMENT_DB_NAME).is_file():
        raise LayoutMismatch(
            "layout_mismatch: 这个文档目录是 APP_OBJECT_STORE_BACKEND=staged 写的单文件"
            " document.sqlite(ADR 0047),files / sqlite / auto 读不到它。请设"
            " APP_OBJECT_STORE_BACKEND=staged(并把 APP_OBJECT_STORE_STAGING_DIR 指向本地盘)。"
        )


def _note_fallback(root: Path, code: str | None) -> None:
    """``auto`` 退到文件布局:每个目录一条 warning + 一条 trace,只带失败码(隐私:不带路径)。"""
    key = root.expanduser().resolve()
    with _WARNED_LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    _LOGGER.warning(
        "APP_OBJECT_STORE_BACKEND=auto: sqlite 可用性探测失败(%s),该目录回退到文件布局"
        "(文件数会多很多;Databricks 上可改用 APP_OBJECT_STORE_BACKEND=staged)",
        code,
    )
    emit_trace(
        event="object_backend_fallback",
        requested="auto",
        backend="files",
        failure_code=code or "unknown",
    )


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
    inline_max_bytes = (
        settings.object_store_inline_max_bytes
        if "object_store_inline_max_bytes" in settings.model_fields_set
        else STAGED_INLINE_MAX_BYTES
    )
    located = document_root_of(root, "object")
    if located is not None:
        # ADR 0047:文档的 store 是文档 db 的一个 scope;PDF 原件默认也进库(显式设置的
        # APP_OBJECT_STORE_EXTERNAL_MEDIA_TYPES 仍优先),内联到 64 MiB。
        return acquire_staged_document(
            located[0],
            located[1],
            staging_root=staging_root,
            synchronous=settings.object_store_synchronous,
            inline_max_bytes=inline_max_bytes,
            external_media_types=(
                external_media_types(settings)
                if "object_store_external_media_types" in settings.model_fields_set
                else frozenset()
            ),
            max_db_bytes=settings.object_store_max_db_bytes,
            media_inline_max_bytes={
                "application/pdf": max(STAGED_PDF_INLINE_MAX_BYTES, inline_max_bytes)
            },
        )
    return acquire_staged(
        root,
        staging_root=staging_root,
        synchronous=settings.object_store_synchronous,
        inline_max_bytes=inline_max_bytes,
        external_media_types=external_media_types(settings),
        max_db_bytes=settings.object_store_max_db_bytes,
    )
