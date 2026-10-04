"""Simulate a filesystem (e.g. a restricted FUSE mount) that cannot hard-link or fsync a directory."""

import errno
import os
import stat

import pytest


def forbid_hard_links(monkeypatch: pytest.MonkeyPatch, code: int = errno.EPERM) -> None:
    """Every ``os.link`` fails the way such a filesystem reports it."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(os, "link", refuse)


def fail_directory_fsync(monkeypatch: pytest.MonkeyPatch, code: int = errno.EINVAL) -> None:
    """``os.fsync`` of a directory descriptor fails; regular files still sync."""
    real_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(code, os.strerror(code))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
