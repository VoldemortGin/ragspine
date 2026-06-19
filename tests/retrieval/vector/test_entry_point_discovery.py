"""make_vector_store 的 entry-point 自动发现测试（落地 docs/prd-breadth-via-adapters.md 的
「Registry + entry-point discovery」与 user stories 1 & 4）。

config-string 选择此前已经能用（见 test_vector_store_factory.py）；本文件锁住最后一条腿：
一个第三方包在 entry-point group `ragspine.vector_stores` 下注册一行，make_vector_store
就能按名字选中它——核心【零改动】、不 import 任何第三方 SDK。验证四件事：
    1. 内置名字（含别名）仍照常解析（注册表回归）。
    2. 未知名字 -> ValueError，错误信息列出内置 + 已发现的 entry-point 名字。
    3. monkeypatch 出来的一个假 entry point 让 make_vector_store('dummy') 返回该实现。
    4. 按名字【解析】内置 adapter 不会 import 其 SDK——SDK 只在【实例化】时才延迟 import。
大小写 / 留白不敏感对内置与 entry-point 名字一致生效。
"""

import importlib.metadata
import os
import sys

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.vector import store as store_mod
from ragspine.retrieval.vector.store import (
    VECTOR_STORE_ENTRY_POINT_GROUP,
    InProcessVectorStore,
    VectorStore,
    _resolve_factory,
    make_vector_store,
)


# --------------------------------------------------------------------------- #
# 测试替身：一个最小的第三方 VectorStore + 假 entry point。                      #
# --------------------------------------------------------------------------- #
class _DummyVectorStore:
    """测试用最小 VectorStore 实现（满足 Protocol 的四个方法；记下 kwargs 以验证透传）。"""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def upsert(self, records):  # noqa: ANN001
        return 0

    def query(self, vector, *, k=50, where=None):  # noqa: ANN001
        return []

    def delete(self, *, where):  # noqa: ANN001
        return 0

    def count(self):
        return 0


class _FakeEntryPoint:
    """importlib.metadata.EntryPoint 的最小替身（只用到 .name + .load()）。"""

    def __init__(self, name: str, target) -> None:  # noqa: ANN001
        self.name = name
        self._target = target

    def load(self):
        return self._target


def _patch_entry_points(monkeypatch, eps: list[_FakeEntryPoint]) -> None:
    """把 importlib.metadata.entry_points 替换成只对本 group 返回 eps 的假实现。"""

    def _fake(*, group=None):  # noqa: ANN001
        return list(eps) if group == VECTOR_STORE_ENTRY_POINT_GROUP else []

    monkeypatch.setattr(importlib.metadata, "entry_points", _fake)


# --------------------------------------------------------------------------- #
# 1. 内置名字 + 别名仍照常解析（注册表回归）。                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "spec, expected_name",
    [
        ("in_process", "InProcessVectorStore"),
        ("in-process", "InProcessVectorStore"),
        ("InProcess", "InProcessVectorStore"),
        ("  MEMORY  ", "InProcessVectorStore"),
        ("sqlite_vec", "SqliteVecVectorStore"),
        ("sqlite-vec", "SqliteVecVectorStore"),
        ("SqliteVec", "SqliteVecVectorStore"),
        ("pgvector", "PgVectorVectorStore"),
        ("pg_vector", "PgVectorVectorStore"),
        ("qdrant", "QdrantVectorStore"),
        ("  Qdrant ", "QdrantVectorStore"),
    ],
)
def test_builtin_names_and_aliases_resolve(spec, expected_name):
    """内置名字（含别名、大小写/留白不敏感）-> 对应类（仅解析，不实例化，故无需装 SDK）。"""
    factory = _resolve_factory(spec.strip().lower())
    assert factory.__name__ == expected_name


def test_in_process_still_constructs_via_make_vector_store():
    """make_vector_store('in_process') 仍返回全新空的 InProcessVectorStore（满足 Protocol）。"""
    store = make_vector_store("in_process")
    assert isinstance(store, InProcessVectorStore)
    assert isinstance(store, VectorStore)
    assert store.count() == 0


# --------------------------------------------------------------------------- #
# 2. 未知名字 -> ValueError，列出内置 + 已发现 entry-point 名字。                #
# --------------------------------------------------------------------------- #
def test_unknown_name_raises_value_error_listing_available(monkeypatch):
    """既非内置也非已注册 entry point 的名字 -> ValueError，信息同时列出两类可选名字。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("dummy", _DummyVectorStore)])
    with pytest.raises(ValueError) as excinfo:
        make_vector_store("nope_no_such_backend")
    msg = str(excinfo.value)
    assert "vector store" in msg
    assert "in_process" in msg  # 内置名字被列出
    assert "dummy" in msg  # 已发现的 entry-point 名字也被列出


def test_unknown_name_lists_no_discovered_when_none(monkeypatch):
    """没有任何 entry point 时，错误信息仍干净给出（不崩、不臆造）。"""
    _patch_entry_points(monkeypatch, [])
    with pytest.raises(ValueError, match="vector store"):
        make_vector_store("milvus")


# --------------------------------------------------------------------------- #
# 3. 模拟一个 entry point -> make_vector_store('dummy') 返回该第三方实现。       #
# --------------------------------------------------------------------------- #
def test_entry_point_backend_is_selectable_by_name(monkeypatch):
    """注册一个名为 dummy 的 entry point 后，make_vector_store('dummy') 返回其实例。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("dummy", _DummyVectorStore)])
    store = make_vector_store("dummy")
    assert isinstance(store, _DummyVectorStore)
    assert isinstance(store, VectorStore)  # 满足 runtime_checkable 结构协议


def test_entry_point_name_is_case_and_whitespace_insensitive(monkeypatch):
    """entry-point 名字解析与内置一致：大小写 / 留白不敏感。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("dummy", _DummyVectorStore)])
    assert isinstance(make_vector_store("  DUMMY  "), _DummyVectorStore)


def test_entry_point_kwargs_passthrough(monkeypatch):
    """选用 entry-point 后端时 **kwargs 原样透传给其构造函数。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("dummy", _DummyVectorStore)])
    store = make_vector_store("dummy", path="/tmp/x", collection="c")
    assert store.kwargs == {"path": "/tmp/x", "collection": "c"}


def test_builtin_name_wins_over_entry_point(monkeypatch):
    """内置名字优先于同名 entry point（注册表先查，第三方不能劫持内置语义）。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("in_process", _DummyVectorStore)])
    store = make_vector_store("in_process")
    assert isinstance(store, InProcessVectorStore)
    assert not isinstance(store, _DummyVectorStore)


def test_entry_points_queried_with_group(monkeypatch):
    """_discover_entry_points 只查本 group（用 group 关键字调用 importlib.metadata）。"""
    seen_groups: list[str | None] = []

    def _spy(*, group=None):  # noqa: ANN001
        seen_groups.append(group)
        return []

    monkeypatch.setattr(importlib.metadata, "entry_points", _spy)
    list(store_mod._discover_entry_points())
    assert seen_groups == [VECTOR_STORE_ENTRY_POINT_GROUP]


# --------------------------------------------------------------------------- #
# 4. 按名字解析内置 adapter 不会 import 其 SDK——SDK 只在实例化时延迟 import。   #
# --------------------------------------------------------------------------- #
def test_resolving_builtin_adapter_does_not_import_sdk(monkeypatch):
    """_resolve_factory('qdrant') 只 import adapter 模块、不 import qdrant_client SDK。

    刻意把 adapter 模块与其 SDK 都从 sys.modules 移除，再解析：重新 import adapter 模块
    会执行其模块体——若模块体里【没有】顶层 import SDK，则解析后 SDK 仍不在 sys.modules，
    证明 core 解析路径零 SDK import（SDK 留待 adapter.__init__ 在实例化时延迟 import）。
    """
    monkeypatch.delitem(sys.modules, "qdrant_client", raising=False)
    monkeypatch.delitem(sys.modules, "ragspine.retrieval.vector.adapters.qdrant", raising=False)
    factory = _resolve_factory("qdrant")
    assert factory.__name__ == "QdrantVectorStore"
    assert "qdrant_client" not in sys.modules  # 解析只拉 adapter 模块，未拉 SDK


def test_builtin_adapters_resolve_without_extras_installed():
    """三个 [vector] adapter 的【类解析】都不依赖其 SDK 是否安装（仅实例化才需）。"""
    for spec in ("sqlite_vec", "pgvector", "qdrant"):
        factory = _resolve_factory(spec)
        assert isinstance(factory, type)  # 拿到的是类，尚未实例化
