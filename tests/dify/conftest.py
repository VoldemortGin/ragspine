"""ragspine.dify 测试共享 fixture：fixture YAML 路径与文本。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """tests/dify/fixtures 目录绝对路径。"""
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def fixture_text() -> Callable[[str], str]:
    """按名读 fixture YAML 文本，如 fixture_text('seq')。"""

    def _read(name: str) -> str:
        return (FIXTURES_DIR / f"{name}.yml").read_text(encoding="utf-8")

    return _read
