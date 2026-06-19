"""PersistencePolicy 缝的单元测试（落地敏感度门控持久化增量）。

一条缝、一项决策：某个块的向量是否可写盘（at-rest）。默认 IsolationFirstPolicy 绝不
落盘 RESTRICTED 块的向量；PersistEverythingPolicy 为 opt-in（整库已按 RESTRICTED-tier 保护时）。
系统级隔离绑定（默认 policy 下 NarrativeIndex 不落盘 RESTRICTED 向量）在 NarrativeIndex 测试里。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.vector.persistence_policy import (
    PERSISTENCE_POLICY_ENV,
    IsolationFirstPolicy,
    PersistencePolicy,
    PersistEverythingPolicy,
    make_persistence_policy,
)


def _chunk(sensitivity: str) -> Chunk:
    return Chunk(
        chunk_id="d#c0", doc_id="d", seq=0, text="x", source_locator="d#para1",
        para_start=1, para_end=1, sensitivity=sensitivity,
    )


def test_isolation_first_refuses_restricted_persists_others():
    """默认：RESTRICTED 不可落盘；INTERNAL/PUBLIC 可落盘；大小写/留白不敏感。"""
    policy = IsolationFirstPolicy()
    assert policy.persistable(_chunk("INTERNAL")) is True
    assert policy.persistable(_chunk("PUBLIC")) is True
    assert policy.persistable(_chunk("RESTRICTED")) is False
    assert policy.persistable(_chunk("  restricted ")) is False  # 归一后仍挡


def test_persist_everything_persists_all_including_restricted():
    """opt-in：全部可落盘（含 RESTRICTED）——仅当整库已按 RESTRICTED-tier at-rest 保护时用。"""
    policy = PersistEverythingPolicy()
    assert policy.persistable(_chunk("RESTRICTED")) is True
    assert policy.persistable(_chunk("INTERNAL")) is True


def test_policies_satisfy_protocol():
    """两个默认实现结构上满足 PersistencePolicy 协议（runtime_checkable）。"""
    assert isinstance(IsolationFirstPolicy(), PersistencePolicy)
    assert isinstance(PersistEverythingPolicy(), PersistencePolicy)


def test_factory_default_is_isolation_first():
    """工厂缺省（None / 'default'）-> IsolationFirstPolicy（隔离优先安全默认）。"""
    assert isinstance(make_persistence_policy(), IsolationFirstPolicy)
    assert isinstance(make_persistence_policy("default"), IsolationFirstPolicy)
    assert isinstance(make_persistence_policy("isolation_first"), IsolationFirstPolicy)


def test_factory_persist_everything():
    """'persist_everything' / 'all' -> PersistEverythingPolicy。"""
    assert isinstance(make_persistence_policy("persist_everything"), PersistEverythingPolicy)
    assert isinstance(make_persistence_policy("all"), PersistEverythingPolicy)


def test_factory_reads_env(monkeypatch):
    """缺省 spec 时读环境变量 RAGSPINE_PERSISTENCE_POLICY。"""
    monkeypatch.setenv(PERSISTENCE_POLICY_ENV, "persist_everything")
    assert isinstance(make_persistence_policy(), PersistEverythingPolicy)


def test_factory_unknown_raises():
    """未知 spec -> ValueError。"""
    with pytest.raises(ValueError, match="persistence policy"):
        make_persistence_policy("yolo")
