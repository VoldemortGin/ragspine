"""InMemoryConnector（零依赖、确定性「只读外部库 / fixture」连接器）的行为契约。

provenance 不变量本身在 tests/conformance/test_source_connector_provenance.py 对【每个注册
connector】绑定；这里测 InMemoryConnector 的具体语义（结构合规 / 原样按序产出 / 跨调用确定性 /
血缘非空）与工厂解析（'memory' / 'in_memory' / 'fixture' 别名）。
"""

import hashlib
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.ingestion.source.connector import (
    RawDoc,
    SourceConnector,
    make_source_connector,
)
from ragspine.ingestion.source.memory import InMemoryConnector


def _docs():
    """三份确定性 RawDoc（顺序即契约），血缘齐备。"""
    return [
        RawDoc(
            source_doc_id="a.txt",
            locator="mem://a.txt",
            content=b"alpha",
            content_type="text/plain",
            metadata={"file_hash": hashlib.sha256(b"alpha").hexdigest()},
        ),
        RawDoc(
            source_doc_id="b.txt",
            locator="mem://b.txt",
            content=b"bravo",
            content_type="text/plain",
            metadata={"file_hash": hashlib.sha256(b"bravo").hexdigest()},
        ),
        RawDoc(
            source_doc_id="c.txt",
            locator="mem://c.txt",
            content=b"charlie",
            content_type="text/plain",
            metadata={"file_hash": hashlib.sha256(b"charlie").hexdigest()},
        ),
    ]


def test_structural_conformance_to_protocol():
    """InMemoryConnector 结构化满足 runtime_checkable SourceConnector。"""
    assert isinstance(InMemoryConnector(_docs()), SourceConnector)


def test_yields_given_docs_in_given_order():
    """按调用方给定顺序原样产出（顺序即契约，不重排）。"""
    docs = _docs()
    out = list(InMemoryConnector(docs).iter_documents())
    assert [d.source_doc_id for d in out] == ["a.txt", "b.txt", "c.txt"]
    assert out == docs


def test_deterministic_across_two_calls():
    """同一 connector 两次迭代产出相同顺序（确定性）。"""
    conn = InMemoryConnector(_docs())
    first = [d.source_doc_id for d in conn.iter_documents()]
    second = [d.source_doc_id for d in conn.iter_documents()]
    assert first == second == ["a.txt", "b.txt", "c.txt"]


def test_provenance_non_empty():
    """每个 RawDoc 都带非空 source_doc_id + locator。"""
    for d in InMemoryConnector(_docs()).iter_documents():
        assert d.source_doc_id
        assert d.locator


def test_empty_docs_yields_nothing():
    """空输入产出空流（不臆造）。"""
    assert list(InMemoryConnector([]).iter_documents()) == []


def test_factory_memory_aliases():
    """'memory' / 'in_memory' / 'fixture' 别名皆解析到 InMemoryConnector。"""
    for spec in ["memory", "in_memory", "  Fixture "]:
        conn = make_source_connector(spec, docs=_docs())
        assert isinstance(conn, InMemoryConnector)
        assert [d.source_doc_id for d in conn.iter_documents()] == ["a.txt", "b.txt", "c.txt"]
