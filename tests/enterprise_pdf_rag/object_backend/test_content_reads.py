"""完整内容读顺序(PR-2):对象 → 内联 stage 产物 → 旧代文件,两后端同语义。

``get_content`` / ``note_product`` / ``object_location`` / ``content_path`` 是 store 层
(``LocalDocumentStore._read_digest`` / ``content_path``)下沉到后端缝的读路径;
sqlite 额外钉死:只读路径绝不创建 db(扫描旧目录零写入),``verify_many`` 分批。
"""

from dataclasses import replace
from pathlib import Path

import pytest

from ragspine.common.evidence.file_placement import sharded_path
from ragspine.common.evidence.object_backend.files import FileBackend
from ragspine.common.evidence.object_backend.protocol import (
    DamagedEntry,
    ObjectBackend,
    StageEntry,
)
from ragspine.common.evidence.object_backend.sqlite import SqliteBackend
from tests.enterprise_pdf_rag.object_backend.conftest import (
    damage_object,
    make_backend,
    sha,
    stage_envelope,
)

FP = "b" * 64


def inline_entry(artifact: bytes, fingerprint: str = FP) -> StageEntry:
    """一条内联携带产物的 stage 条目(ADR 0029 Amendment 2)。"""
    entry = stage_envelope(artifact, fingerprint)
    return replace(entry, product=artifact)


def test_get_content_reads_objects_and_inline_products(
    backend: ObjectBackend, backend_kind: str, store_root: Path
) -> None:
    data = b'{"object": true}' * 20
    digest = sha(data)
    backend.put_object(digest, data, "application/json")
    assert backend.get_content(digest) == data

    product = b'{"product": 1}'
    backend.put_stage_entry(FP, inline_entry(product))
    assert backend.get_object(sha(product)) is None  # 产物从不是对象
    assert backend.get_content(sha(product)) == product
    # 位置:对象给文件或 db;内联产物在 files 下是指针文件,在 sqlite 下住在行里。
    if backend_kind == "files":
        pointer = store_root / "stage-cache-sharded" / FP[:2] / FP
        assert backend.object_location(sha(product)) == pointer
        assert backend.content_path(sha(product)) == pointer
        assert backend.object_location(digest) == sharded_path(
            store_root / "objects" / "sha256", digest
        )
    else:
        assert backend.object_location(sha(product)) is None
        assert backend.object_location(digest) is None
        with pytest.raises(LookupError):
            backend.content_path(sha(product))
        with pytest.raises(LookupError):
            backend.content_path(digest)


def test_get_content_absent_is_none_and_damaged_object_refuses(
    backend: ObjectBackend, backend_kind: str, store_root: Path
) -> None:
    assert backend.get_content("c" * 64) is None
    data = b'{"will": "break"}' * 30
    digest = sha(data)
    backend.put_object(digest, data, "application/json")
    damage_object(backend_kind, store_root, digest)
    with pytest.raises(DamagedEntry):
        backend.get_content(digest)


def test_note_product_seeds_the_location_without_trusting_it(
    backend: ObjectBackend, backend_kind: str, store_root: Path
) -> None:
    product = b'{"noted": true}'
    backend.put_stage_entry(FP, inline_entry(product))
    # 一个新的后端实例(新进程的替身)靠 note_product 提示免扫描,但读回仍然 hash。
    fresh = make_backend(backend_kind, store_root)
    try:
        fresh.note_product(sha(product), FP)
        assert fresh.get_content(sha(product)) == product
    finally:
        fresh.close()


def test_external_object_location_is_its_file_on_both_backends(
    backend: ObjectBackend, store_root: Path
) -> None:
    pdf = b"%PDF-1.7 fake"
    digest = sha(pdf)
    backend.put_object(digest, pdf, "application/pdf")
    located = backend.object_location(digest)
    assert located == sharded_path(store_root / "objects" / "sha256", digest)
    assert backend.content_path(digest) == located


def test_sqlite_reads_a_files_generation_store_without_creating_a_db(tmp_path: Path) -> None:
    root = tmp_path / "store"
    legacy = FileBackend(root)
    data = b'{"legacy": "object"}' * 10
    product = b'{"legacy": "product"}'
    legacy.put_object(sha(data), data, "application/json")
    legacy.put_stage_entry(FP, inline_entry(product))
    reader = SqliteBackend(root)
    try:
        assert reader.get_content(sha(data)) == data
        assert reader.get_content(sha(product)) == product  # 旧代内联指针的扫描读
        assert reader.pointer("current-processing") is None
        assert reader.stage_entry(FP) is not None
        assert sha(data) in reader.object_names()
        assert not (root / "store.sqlite").exists()  # 只读路径不建库
    finally:
        reader.close()
    assert not (root / "store.sqlite").exists()


def test_sqlite_verify_many_batches_across_db_inline_and_external(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path / "store")
    payloads = [f'{{"n": {i}}}'.encode() for i in range(503)]
    digests = [sha(data) for data in payloads]
    for data, digest in zip(payloads, digests, strict=True):
        backend.put_object(digest, data, "application/json")
    product = b'{"inline": "p"}'
    backend.put_stage_entry(FP, inline_entry(product))
    pdf = b"%PDF-1.7 external"
    backend.put_object(sha(pdf), pdf, "application/pdf")
    wanted = [*digests, sha(product), sha(pdf)]
    try:
        assert list(backend.verify_many(wanted)) == list(
            zip(wanted, [*payloads, product, pdf], strict=True)
        )
        with pytest.raises(LookupError):
            list(backend.verify_many([sha(b"missing")]))
        damage_object("sqlite", tmp_path / "store", digests[0])
        with pytest.raises(DamagedEntry):
            list(backend.verify_many(digests))
    finally:
        backend.close()
