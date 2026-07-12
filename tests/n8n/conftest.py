"""ragspine.n8n 测试共享 fixture：n8n JSON fixture 与 dify YAML fixture 的加载器。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DIFY_FIXTURES_DIR = Path(__file__).parent.parent / "dify" / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """tests/n8n/fixtures 目录绝对路径。"""
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def fixture_text() -> Callable[[str], str]:
    """按名读 n8n fixture JSON 文本，如 fixture_text('linear')。"""

    def _read(name: str) -> str:
        return (FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8")

    return _read


@pytest.fixture(scope="session")
def fixture_json() -> Callable[[str], dict[str, Any]]:
    """按名读 n8n fixture 为 dict，如 fixture_json('linear')。"""

    def _read(name: str) -> dict[str, Any]:
        data = json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data

    return _read


@pytest.fixture(scope="session")
def dify_fixture_text() -> Callable[[str], str]:
    """按名读 tests/dify/fixtures 的 YAML 文本，如 dify_fixture_text('seq')。"""

    def _read(name: str) -> str:
        return (DIFY_FIXTURES_DIR / f"{name}.yml").read_text(encoding="utf-8")

    return _read
