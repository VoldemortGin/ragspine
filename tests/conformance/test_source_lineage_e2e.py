"""端到端血缘不变量：lineage 经 SourceConnector → bridge → fact_store 不丢（conformance）。

新不变量「lineage 端到端进 fact_store 不丢」：一份 RawDoc 的 source_doc_id（血缘根）与
metadata['file_hash']（版本血缘）必须原样落到它抽出的每一条 Fact 的 source_doc_id /
source_file_hash 上——即便中途经 bridge 落进临时文件再委托 ingest_file。

正证：InMemoryConnector 包一份真实 xlsx fixture，跑 ingest_from_connector，断言库里每条 Fact
的血缘 == 该 RawDoc 的血缘（非空）。
反证：喂一个 source_doc_id 与真实文件名【不同】的 RawDoc，断言抽出的 Fact 的 source_doc_id
等于该【连接器给的 id】而非临时目录路径——证明是 connector 的血缘、而非落盘 temp path 在赢。
"""

import hashlib
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.color.color_semantics import ColorMapping, LegendEntry, MappingRegistry
from ragspine.ingestion.review.review_queue import ReviewQueue
from ragspine.ingestion.source.bridge import ingest_from_connector
from ragspine.ingestion.source.connector import RawDoc
from ragspine.ingestion.source.memory import InMemoryConnector
from ragspine.storage.fact_store import SqliteFactStore

# HK_Performance 是 fixture 里唯一 glossary 完整可识别的数据 sheet：4 指标 × 3 期间 = 12 事实。
EXPECTED_HK_FACTS = 12


@pytest.fixture
def store(tmp_sqlite_factory):
    fs = SqliteFactStore(tmp_sqlite_factory("facts"))
    fs.init_schema()
    yield fs
    fs.close()


@pytest.fixture
def queue(tmp_sqlite_factory):
    q = ReviewQueue(tmp_sqlite_factory("queue"))
    q.init_schema()
    yield q
    q.close()


def _registry_with_active_mapping(tmp_sqlite_factory, ground_truth, scope: str) -> MappingRegistry:
    """建 MappingRegistry 并对给定 scope 装配与 fixture 图例一致的 active 颜色映射。

    scope = 连接器给 RawDoc 的 source_doc_id（= 落盘临时文件名，ingest_file 据此取 active 映射），
    复用 tests/ingestion/structured/test_ingestion.py 的确定性配置口径，使抽取产出事实而非全入复核。
    """
    reg = MappingRegistry(tmp_sqlite_factory("registry"))
    reg.init_schema()
    legend = ground_truth["sheets"]["HK_Performance"]["legend_expect"]
    entries = [
        LegendEntry(rgb=e["rgb"], meaning=e["meaning"], tag_key=e["tag_key"], tag_value=e["tag_value"])
        for e in legend
    ]
    version = reg.register_draft(ColorMapping(scope=scope, entries=entries))
    reg.confirm(scope, version, actor="sme_fin", note="fixture 图例")
    return reg


def _rawdoc_from_fixture(excel_fixture_path, *, source_doc_id: str) -> RawDoc:
    """把真实 xlsx fixture 的字节包成一份 RawDoc（血缘 = 给定 source_doc_id + 字节 sha256）。"""
    content = excel_fixture_path.read_bytes()
    return RawDoc(
        source_doc_id=source_doc_id,
        locator=f"mem://{source_doc_id}",
        content=content,
        content_type=".xlsx",
        metadata={"file_hash": hashlib.sha256(content).hexdigest()},
    )


# ===========================================================================
# 正证：lineage 端到端进 fact_store 不丢
# ===========================================================================
def test_lineage_survives_connector_to_fact_store(
    store, queue, tmp_sqlite_factory, excel_fixture_path, ground_truth
):
    """每条落库 Fact 的 source_doc_id / source_file_hash == RawDoc 的血缘（皆非空）。"""
    scope = excel_fixture_path.name
    raw = _rawdoc_from_fixture(excel_fixture_path, source_doc_id=scope)
    registry = _registry_with_active_mapping(tmp_sqlite_factory, ground_truth, scope)
    try:
        reports = ingest_from_connector(InMemoryConnector([raw]), store, registry, queue)
    finally:
        registry.close()

    assert len(reports) == 1
    assert reports[0].status == "ok"
    assert reports[0].n_facts_ingested == EXPECTED_HK_FACTS

    facts = store.query("REVENUE", "ACME_HK", "FY", "2024")
    assert facts, "抽取应产出事实（HK_Performance 可归因），而非全部入复核"
    all_facts = _all_facts(store)
    assert all_facts
    for f in all_facts:
        assert f.source_doc_id == raw.source_doc_id
        assert f.source_doc_id  # 非空
        assert f.source_file_hash == raw.metadata["file_hash"]
        assert f.source_file_hash  # 非空


# ===========================================================================
# 反证：连接器给的 id 在赢，而非落盘临时目录路径
# ===========================================================================
def test_connector_id_wins_not_temp_path(
    store, queue, tmp_sqlite_factory, excel_fixture_path, ground_truth
):
    """source_doc_id 与真实文件名【不同】的 RawDoc：Fact 血缘 == 连接器给的 id，临时路径不泄漏。"""
    renamed = "renamed_evidence.xlsx"  # 与 fixture 真名不同，但仍是 .xlsx 以走 xlsx 抽取器
    raw = _rawdoc_from_fixture(excel_fixture_path, source_doc_id=renamed)
    registry = _registry_with_active_mapping(tmp_sqlite_factory, ground_truth, renamed)
    try:
        ingest_from_connector(InMemoryConnector([raw]), store, registry, queue)
    finally:
        registry.close()

    all_facts = _all_facts(store)
    assert all_facts, "renamed RawDoc 仍应抽出事实"
    for f in all_facts:
        # 连接器的 id 在赢：血缘 = RawDoc.source_doc_id，而非落盘 temp path 的目录段。
        assert f.source_doc_id == renamed
        assert "/" not in f.source_doc_id and "\\" not in f.source_doc_id
        assert f.source_doc_id != excel_fixture_path.name  # 也不是 fixture 真名
        assert f.source_file_hash == raw.metadata["file_hash"]


def _all_facts(store):
    """取库里全部可见事实（跨 HK_Performance 的 4 指标）。"""
    out = []
    for metric in ("REVENUE", "NEWSALES", "PROFIT", "ROE"):
        for period in ("2022", "2023", "2024"):
            out.extend(store.query(metric, "ACME_HK", "FY", period))
    return out
