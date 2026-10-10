"""后端 parametrize 的共享 fixture:同一套一致性测试在 files / sqlite / staged 上各跑一遍
(对象 store 与模型缓存都是;staged 模型缓存是文档自己的那一份,ADR 0046)。"""

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.http.processing_schemas import StageEnvelope
from ragspine.common.evidence.object_backend import lease
from ragspine.common.evidence.object_backend.files import (
    CLAIM_FORMAT,
    FileBackend,
    FileModelCacheBackend,
)
from ragspine.common.evidence.object_backend.protocol import (
    ClaimOwner,
    ModelCacheBackend,
    ObjectBackend,
    StageEntry,
)
from ragspine.common.evidence.object_backend.sqlite import (
    SqliteBackend,
    SqliteModelCacheBackend,
)
from ragspine.common.evidence.object_backend.staged import (
    StagedBackend,
    StagedModelCacheBackend,
)
from ragspine.extraction.evidence.document.models import AssetRef
from ragspine.extraction.evidence.page.models import StageOutcome, StageState

BackendKind = str


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def staged_work_dir(root: Path) -> Path:
    """测试里 staged 后端的本地工作目录(与 store 根同级)。"""
    return root.parent / f"{root.name}.work"


def db_file(kind: str, root: Path) -> Path:
    """该后端的 store db 所在(staged 的 db 在本地工作目录)。"""
    if kind == "staged":
        return staged_work_dir(root) / "store.sqlite"
    return root / "store.sqlite"


def make_backend(kind: str, root: Path, **sqlite_kwargs: object) -> ObjectBackend:
    if kind == "files":
        return FileBackend(root)
    if kind == "staged":
        return StagedBackend(root, work_dir=staged_work_dir(root), **sqlite_kwargs)  # type: ignore[arg-type]
    return SqliteBackend(root, **sqlite_kwargs)  # type: ignore[arg-type]


def make_model_cache(kind: str, cache_dir: Path) -> ModelCacheBackend:
    if kind == "files":
        return FileModelCacheBackend(cache_dir)
    if kind == "staged":
        return StagedModelCacheBackend(cache_dir, work_dir=staged_work_dir(cache_dir))
    return SqliteModelCacheBackend(cache_dir)


@pytest.fixture(params=["files", "sqlite", "staged"])
def backend_kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(params=["files", "sqlite", "staged"])
def model_cache_kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def backend(backend_kind: str, store_root: Path) -> Iterator[ObjectBackend]:
    built = make_backend(backend_kind, store_root)
    yield built
    built.close()


@pytest.fixture
def model_cache(model_cache_kind: str, tmp_path: Path) -> Iterator[ModelCacheBackend]:
    built = make_model_cache(model_cache_kind, tmp_path / "model-cache")
    yield built
    built.close()


def stage_envelope(artifact: bytes, fingerprint: str, *, stage: str = "description") -> StageEntry:
    """一条真实的 ``StageEnvelope``(与 ``ProcessingStore.cache`` 写的字节同构)。"""
    outcome = StageOutcome(
        stage,
        fingerprint,
        StageState.SUCCEEDED,
        "producer-v1",
        AssetRef(sha(artifact), "application/json", len(artifact)),
    )
    envelope = StageEnvelope(outcome=outcome).model_dump_json().encode()
    return StageEntry(sha(envelope), envelope)


def live_owner(lease_seconds: int = 300) -> ClaimOwner:
    return lease.current_owner(CLAIM_FORMAT, lease_seconds)


def expired_owner() -> ClaimOwner:
    owner = lease.current_owner(CLAIM_FORMAT, 5)
    return replace(owner, created_at=owner.created_at - 10_000)


def damage_object(kind: str, root: Path, digest: str, garbage: bytes = b"\x00damaged\x00") -> None:
    """把一个已存对象弄坏:files 改文件字节,sqlite 改行内字节(不动摘要)。"""
    if kind == "files":
        path = root / "objects" / "sha256-sharded" / digest[:2] / digest
        if not path.is_file():
            path = root / "objects" / "sha256" / digest
        path.write_bytes(garbage)
        return
    with closing(sqlite3.connect(db_file(kind, root))) as connection, connection:
        connection.execute(
            "UPDATE objects SET bytes = ?, encoding = 'raw' WHERE digest = ?", (garbage, digest)
        )


def damage_stage_entry(kind: str, root: Path, fingerprint: str) -> None:
    """把一条 stage-cache 条目弄坏(信封不再 hash 到它的摘要行)。"""
    if kind == "files":
        path = root / "stage-cache-sharded" / fingerprint[:2] / fingerprint
        payload = path.read_bytes()
        head, _, _ = payload.partition(b"\n")
        path.write_bytes(head + b"\n" + b"{not the envelope}" + b"\n")
        return
    with closing(sqlite3.connect(db_file(kind, root))) as connection, connection:
        connection.execute(
            "UPDATE stage_cache SET envelope = ? WHERE fingerprint = ?",
            (b"{not the envelope}", fingerprint),
        )


def tree(root: Path) -> dict[str, bytes]:
    """一个目录树的 {相对路径: 字节},用于两边逐字节比对。"""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
