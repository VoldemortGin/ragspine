"""块库测试（叙事通路检索侧，TDD 红色阶段）。

只验证外部行为：schema 幂等、写入/读回保真、valid_as_of/ingested_at/版本血缘、
同 doc 重新入库幂等替换（旧版本失效）、元数据过滤遍历、多文档隔离、execute_read。
sqlite 走 conftest 的 tmp_db_path 临时库，零网络。

红色预期：ChunkStore 行为方法因 stub raise NotImplementedError 而全部 FAIL。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore, StoredChunk
from ragspine.retrieval.chunking.chunking import Chunk


def _chunk(doc_id: str = "d1", seq: int = 0, text: str = "香港 REVENUE 增长", **overrides) -> Chunk:
    """构造一个带全量元数据的 Chunk（可逐字段覆盖）。"""
    kwargs = dict(
        chunk_id=f"{doc_id}#c{seq}",
        doc_id=doc_id,
        seq=seq,
        text=text,
        source_locator=f"{doc_id}#para{seq + 1}",
        para_start=seq + 1,
        para_end=seq + 1,
        title="标题",
        topic="FIN",
        entity="ACME_HK",
        geography="HK",
        period="2025H1",
        language="zh",
        sensitivity="INTERNAL",
    )
    kwargs.update(overrides)
    return Chunk(**kwargs)


@pytest.fixture
def store(tmp_db_path):
    """每测试独立的临时块库。"""
    s = ChunkStore(tmp_db_path)
    s.init_schema()
    yield s
    s.close()


# ===========================================================================
# schema / 写入读回
# ===========================================================================

def test_init_schema_idempotent(store):
    """init_schema 可重复调用且初始为空。"""
    store.init_schema()
    assert store.count() == 0


def test_replace_and_roundtrip(store):
    """写入 2 块读回：字段保真、version=1、active=True。"""
    chunks = [_chunk(seq=0, text="第一块"), _chunk(seq=1, text="第二块", topic="REG")]
    written = store.replace_doc_chunks("d1", chunks)
    assert written == 2

    rows = store.iter_chunks()
    assert len(rows) == 2
    assert all(isinstance(r, StoredChunk) for r in rows)
    first = rows[0]
    assert first.chunk_id == "d1#c0"
    assert first.text == "第一块"
    assert first.source_locator == "d1#para1"
    assert first.para_start == 1 and first.para_end == 1
    assert (first.title, first.topic, first.entity) == ("标题", "FIN", "ACME_HK")
    assert (first.geography, first.period, first.language) == ("HK", "2025H1", "zh")
    assert first.sensitivity == "INTERNAL"
    assert first.version == 1
    assert first.active is True
    assert rows[1].topic == "REG"


def test_valid_as_of_and_ingested_at(store):
    """valid_as_of 原样落库；ingested_at 由库内生成（ISO 串）。"""
    store.replace_doc_chunks("d1", [_chunk()], valid_as_of="2026-06-10")
    row = store.iter_chunks()[0]
    assert row.valid_as_of == "2026-06-10"
    assert row.ingested_at and "T" in row.ingested_at


# ===========================================================================
# 幂等重入 / 版本
# ===========================================================================

def test_reingest_replaces_active_set(store):
    """同 doc 重新入库：活跃集 = 最新一批，旧块默认不可见（幂等）。"""
    store.replace_doc_chunks(
        "d1", [_chunk(seq=i, text=f"旧块{i}") for i in range(3)]
    )
    store.replace_doc_chunks(
        "d1", [_chunk(seq=i, text=f"新块{i}") for i in range(2)]
    )
    rows = store.iter_chunks(doc_id="d1")
    assert [r.text for r in rows] == ["新块0", "新块1"]
    assert store.count() == 2


def test_reingest_bumps_version_and_keeps_history(store):
    """重新入库版本递增，旧版本置 inactive 但保留可溯源。"""
    store.replace_doc_chunks("d1", [_chunk(seq=i) for i in range(3)])
    store.replace_doc_chunks("d1", [_chunk(seq=i) for i in range(2)])

    active = store.iter_chunks(doc_id="d1")
    assert {r.version for r in active} == {2}
    assert all(r.active for r in active)

    everything = store.iter_chunks(doc_id="d1", include_inactive=True)
    assert len(everything) == 5
    old = [r for r in everything if r.version == 1]
    assert len(old) == 3
    assert all(not r.active for r in old)
    assert store.count(include_inactive=True) == 5


def test_replace_with_empty_deactivates_doc(store):
    """空列表重入 = 把该文档从活跃集撤下。"""
    store.replace_doc_chunks("d1", [_chunk(seq=0)])
    store.replace_doc_chunks("d1", [])
    assert store.iter_chunks(doc_id="d1") == []
    assert store.count() == 0


def test_multiple_docs_isolated(store):
    """重入 d1 不影响 d2 的活跃块。"""
    store.replace_doc_chunks("d1", [_chunk(doc_id="d1", seq=0, text="d1 旧")])
    store.replace_doc_chunks("d2", [_chunk(doc_id="d2", seq=0, text="d2 块")])
    store.replace_doc_chunks("d1", [_chunk(doc_id="d1", seq=0, text="d1 新")])

    assert [r.text for r in store.iter_chunks(doc_id="d2")] == ["d2 块"]
    assert [r.text for r in store.iter_chunks(doc_id="d1")] == ["d1 新"]


# ===========================================================================
# 元数据过滤遍历（给检索层的预过滤入口）
# ===========================================================================

@pytest.fixture
def filled(store):
    """跨 topic/entity/period/language 的小语料库。"""
    store.replace_doc_chunks("d1", [
        _chunk(doc_id="d1", seq=0, topic="FIN", entity="ACME_HK", period="2025H1", language="zh"),
        _chunk(doc_id="d1", seq=1, topic="REG", entity="ACME_HK", period="2025H1", language="en"),
    ])
    store.replace_doc_chunks("d2", [
        _chunk(doc_id="d2", seq=0, topic="REG", entity="ACME_CN", geography="CN", period="2024", language="zh"),
    ])
    return store


def test_filter_single_field(filled):
    """单字段过滤：topic / entity / language / period 各自生效。"""
    assert {r.chunk_id for r in filled.iter_chunks(topic="REG")} == {"d1#c1", "d2#c0"}
    assert {r.chunk_id for r in filled.iter_chunks(entity="ACME_CN")} == {"d2#c0"}
    assert {r.chunk_id for r in filled.iter_chunks(language="en")} == {"d1#c1"}
    assert {r.chunk_id for r in filled.iter_chunks(period="2024")} == {"d2#c0"}
    assert {r.chunk_id for r in filled.iter_chunks(geography="CN")} == {"d2#c0"}


def test_filter_combination_is_and(filled):
    """组合过滤为 AND 语义。"""
    rows = filled.iter_chunks(topic="REG", entity="ACME_HK")
    assert [r.chunk_id for r in rows] == ["d1#c1"]


def test_filter_no_match_returns_empty(filled):
    """无匹配 -> []。"""
    assert filled.iter_chunks(topic="NOPE") == []


def test_iter_order_by_doc_and_seq(filled):
    """遍历按 (doc_id, seq) 排序，确定性。"""
    rows = filled.iter_chunks()
    assert [r.chunk_id for r in rows] == ["d1#c0", "d1#c1", "d2#c0"]


def test_execute_read(filled):
    """只读 SQL 入口（与 fact_store 约定一致）。"""
    rows = filled.execute_read(
        "SELECT COUNT(*) AS n FROM narrative_chunk WHERE active = 1 AND topic = ?",
        ("REG",),
    )
    assert rows[0]["n"] == 2
