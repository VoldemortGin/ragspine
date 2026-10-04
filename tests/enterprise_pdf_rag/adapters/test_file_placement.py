"""Create-if-absent placement still works where the filesystem cannot hard-link."""

import errno
import os
from pathlib import Path

import pytest

from ragspine.common.evidence.file_placement import fsync_directory, link_new_file
from tests.enterprise_pdf_rag.adapters.no_hard_link_helpers import (
    fail_directory_fsync,
    forbid_hard_links,
)

_UNSUPPORTED = (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV)
_REAL_FAILURES = (errno.EACCES, errno.ENOSPC, errno.EROFS, errno.EIO)


def _temporary(tmp_path: Path, content: bytes) -> Path:
    temporary = tmp_path / "tmp-object"
    temporary.write_bytes(content)
    return temporary


def test_a_linking_filesystem_keeps_the_hard_link_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replaced: list[tuple[object, ...]] = []
    real_replace = os.replace

    def spy(source: Path, destination: Path) -> None:
        replaced.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", spy)
    temporary = _temporary(tmp_path, b"content")
    target = tmp_path / "object"

    link_new_file(temporary, target)

    assert target.read_bytes() == b"content"
    assert temporary.read_bytes() == b"content", "the caller still owns the temporary"
    assert replaced == []


def test_a_linking_filesystem_reports_an_existing_target_like_os_link(tmp_path: Path) -> None:
    target = tmp_path / "object"
    target.write_bytes(b"first")

    with pytest.raises(FileExistsError):
        link_new_file(_temporary(tmp_path, b"second"), target)

    assert target.read_bytes() == b"first"


@pytest.mark.parametrize("code", _UNSUPPORTED)
def test_without_hard_links_the_temporary_is_renamed_into_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    forbid_hard_links(monkeypatch, code)
    temporary = _temporary(tmp_path, b"content")
    target = tmp_path / "object"

    link_new_file(temporary, target)

    assert target.read_bytes() == b"content"
    assert not temporary.exists()
    assert [path.name for path in tmp_path.iterdir()] == ["object"]


def test_without_hard_links_an_existing_target_is_reported_and_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_hard_links(monkeypatch)
    target = tmp_path / "object"
    target.write_bytes(b"first")
    temporary = _temporary(tmp_path, b"second")

    with pytest.raises(FileExistsError):
        link_new_file(temporary, target)

    assert target.read_bytes() == b"first"
    assert temporary.read_bytes() == b"second", "the caller still owns the temporary"


def test_without_hard_links_a_racing_writer_that_lands_last_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_hard_links(monkeypatch)
    real_replace = os.replace
    target = tmp_path / "object"

    def racing_replace(source: Path, destination: Path) -> None:
        real_replace(source, destination)
        target.write_bytes(b"racer")

    monkeypatch.setattr(os, "replace", racing_replace)

    with pytest.raises(FileExistsError):
        link_new_file(_temporary(tmp_path, b"mine"), target)

    assert target.read_bytes() == b"racer"


@pytest.mark.parametrize("code", _REAL_FAILURES)
def test_real_link_failures_still_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    forbid_hard_links(monkeypatch, code)
    temporary = _temporary(tmp_path, b"content")
    target = tmp_path / "object"

    with pytest.raises(OSError) as raised:
        link_new_file(temporary, target)

    assert raised.value.errno == code
    assert not target.exists()
    assert temporary.exists(), "the caller still owns the temporary"


@pytest.mark.parametrize(
    "code", (errno.EINVAL, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS)
)
def test_an_unsupported_directory_fsync_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    fail_directory_fsync(monkeypatch, code)

    fsync_directory(tmp_path)


@pytest.mark.parametrize("code", (errno.EIO, errno.ENOSPC, errno.EACCES))
def test_a_real_directory_fsync_failure_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    fail_directory_fsync(monkeypatch, code)

    with pytest.raises(OSError) as raised:
        fsync_directory(tmp_path)

    assert raised.value.errno == code
