"""make_vector_store 工厂测试（落地 docs/prd-vector-store-seam.md 的 config-string 选择）。

范式同 ragspine.retrieval.vector.embedding_backends.make_embedding_backend：把「选哪个
向量存储」从改代码降为一个 spec/env。默认 None＝不注入具体 store（检索器用内置内存默认）。
真实后端（sqlite-vec / qdrant / pgvector）待 [vector] extra 落地，本期只认 none / in_process。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.vector.store import (
    VECTOR_STORE_ENV,
    InProcessVectorStore,
    VectorStore,
    make_vector_store,
)


def test_none_spec_returns_none():
    """None / 'none'（大小写/留白不敏感）-> None（不注入具体 store）。"""
    assert make_vector_store(None) is None
    assert make_vector_store("none") is None
    assert make_vector_store("  NONE  ") is None


@pytest.mark.parametrize("spec", ["in_process", "in-process", "memory", "InProcess"])
def test_in_process_specs_return_inprocess_store(spec):
    """in_process 同义词 -> 一个全新空的 InProcessVectorStore（满足 VectorStore 协议）。"""
    store = make_vector_store(spec)
    assert isinstance(store, InProcessVectorStore)
    assert isinstance(store, VectorStore)  # runtime_checkable 结构校验
    assert store.count() == 0


def test_env_read_when_spec_omitted(monkeypatch):
    """缺省 spec 时读环境变量 RAGSPINE_VECTOR_STORE。"""
    monkeypatch.setenv(VECTOR_STORE_ENV, "in_process")
    assert isinstance(make_vector_store(), InProcessVectorStore)


def test_explicit_spec_overrides_env(monkeypatch):
    """显式 spec 优先于环境变量。"""
    monkeypatch.setenv(VECTOR_STORE_ENV, "in_process")
    assert make_vector_store("none") is None


def test_unknown_spec_raises_value_error():
    """未知 spec -> ValueError（错误信息点向 [vector] extra 的真实后端）。"""
    with pytest.raises(ValueError, match="vector store"):
        make_vector_store("qdrant")


def test_each_call_returns_fresh_instance():
    """每次调用返回独立实例（互不共享状态）。"""
    a = make_vector_store("in_process")
    b = make_vector_store("in_process")
    assert a is not b
    a.upsert([])  # 空 upsert 不影响 b
    assert b.count() == 0
