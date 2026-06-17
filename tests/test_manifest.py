"""Ingestion manifest 台账 + 可观测指标 + 版本清单的红色测试（TDD）。

只验证外部行为：手工构造 store / queue / registry / manifest 的状态，
断言 ManifestStore / compute_metrics / list_versions 的对外输出。

覆盖 PRD user stories：
    #30 manifest 台账完整记录一批的输入 / 产出 / 告警 / 失败 / 耗时。
    #31 关键指标可观测（抽取量 / 告警率 / 复核积压 / 置信度分布桶）。
    #33 抽取器与映射表的版本清单可查询（当前生产配置）。

红色预期：所有用例因 stub raise NotImplementedError 而 FAIL
（收集成功、无 collection error、无意外 PASS）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.color.color_semantics import ColorMapping, LegendEntry, MappingRegistry
from ragspine.storage.fact_store import Fact, FactStore
from ragspine.ingestion.structured.ingestion_manifest import (
    BATCH_DONE,
    BATCH_FAILED,
    ManifestRecord,
    ManifestStore,
    compute_metrics,
    list_versions,
)
from ragspine.ingestion.review.review_queue import ReviewQueue


# --------------------------------------------------------------------------- #
# fixtures：只构造对象（开 sqlite 连接），init_schema 等会 raise 的步骤一律放进
# 测试体内调用，使契约未实现时 NotImplementedError 作为用例 FAILURE 而非 fixture
# ERROR 暴露（沿用 test_review_queue.py 的红色阶段约定）。
# --------------------------------------------------------------------------- #
@pytest.fixture
def manifest(tmp_sqlite_factory):
    ms = ManifestStore(tmp_sqlite_factory("manifest"))
    yield ms
    ms.close()


@pytest.fixture
def store(tmp_sqlite_factory):
    fs = FactStore(tmp_sqlite_factory("facts"))
    yield fs
    fs.close()


@pytest.fixture
def queue(tmp_sqlite_factory):
    q = ReviewQueue(tmp_sqlite_factory("queue"))
    yield q
    q.close()


@pytest.fixture
def registry(tmp_sqlite_factory):
    reg = MappingRegistry(tmp_sqlite_factory("registry"))
    yield reg
    reg.close()


def _init_all(*stores) -> None:
    """在测试体内统一建表——契约未实现时此处 NotImplementedError 使用例 FAIL。"""
    for s in stores:
        s.init_schema()


def _fact(metric, period, value, *, confidence=None, extractor_version="xlsx_styled@1"):
    """构造一条最小可入库 Fact（ACME_HK / TOTAL / FY），便于手工铺 store 状态。"""
    return Fact(
        metric_code=metric,
        entity="ACME_HK",
        geography="HK",
        channel="TOTAL",
        period_type="FY",
        period=period,
        value=value,
        unit="USD_M",
        source_doc_id="excel_styled_fixture.xlsx",
        source_locator=f"sheet=HK_Performance!{metric}{period}",
        source_file_hash="deadbeef",
        extractor_version=extractor_version,
        confidence=confidence,
    )


# ===========================================================================
# story #30 — manifest 台账：一批的输入 / 产出 / 告警 / 失败 / 耗时
# ===========================================================================
def test_open_batch_returns_id(manifest):
    """story #30 —— open_batch 返回 batch_id，记录 running 状态与 started_at。"""
    _init_all(manifest)
    batch_id = manifest.open_batch()
    assert isinstance(batch_id, str)
    rec = manifest.get_batch(batch_id)
    assert isinstance(rec, ManifestRecord)
    assert rec.batch_id == batch_id
    assert rec.status == "running"
    assert rec.started_at


def test_open_batch_with_explicit_id(manifest):
    """story #30 —— 可传入显式 batch_id（与 ingest 的 batch_id 关联）。"""
    _init_all(manifest)
    batch_id = manifest.open_batch("batch-2026-06-12")
    assert batch_id == "batch-2026-06-12"
    assert manifest.get_batch("batch-2026-06-12") is not None


def test_record_input_accumulates_inputs(manifest):
    """story #30 —— record_input 把输入文件（path/hash/format）累加进该批清单。"""
    _init_all(manifest)
    batch_id = manifest.open_batch()
    manifest.record_input(batch_id, "a.xlsx", "hash-a", "xlsx", n_facts=12, n_warnings=1)
    manifest.record_input(batch_id, "b.xlsx", "hash-b", "xlsx", n_facts=8, n_warnings=0)

    rec = manifest.get_batch(batch_id)
    assert len(rec.inputs) == 2
    paths = {inp["path"] for inp in rec.inputs}
    assert paths == {"a.xlsx", "b.xlsx"}
    fmts = {inp["format"] for inp in rec.inputs}
    assert fmts == {"xlsx"}
    hashes = {inp["hash"] for inp in rec.inputs}
    assert hashes == {"hash-a", "hash-b"}


def test_record_input_aggregates_facts_and_warnings(manifest):
    """story #30 —— 批级 n_facts / n_warnings 是各输入的累加。"""
    _init_all(manifest)
    batch_id = manifest.open_batch()
    manifest.record_input(batch_id, "a.xlsx", "h1", "xlsx", n_facts=12, n_warnings=2)
    manifest.record_input(batch_id, "b.xlsx", "h2", "xlsx", n_facts=8, n_warnings=3)

    rec = manifest.get_batch(batch_id)
    assert rec.n_facts == 20
    assert rec.n_warnings == 5


def test_record_input_failure_counts_and_records(manifest):
    """story #30/#19 —— 单文件失败计入 n_failed 与 failures，不影响其余输入。"""
    _init_all(manifest)
    batch_id = manifest.open_batch()
    manifest.record_input(batch_id, "good.xlsx", "h1", "xlsx", n_facts=12)
    manifest.record_input(
        batch_id, "bad.xlsx", None, "xlsx", failed=True, error="corrupt zip"
    )

    rec = manifest.get_batch(batch_id)
    assert rec.n_failed == 1
    assert len(rec.failures) == 1
    failure = rec.failures[0]
    # 失败明细可定位到具体文件与原因
    assert "bad.xlsx" in str(failure)
    assert "corrupt" in str(failure)
    # 成功文件的产出仍计入
    assert rec.n_facts == 12


def test_close_batch_sets_finish_status_duration(manifest):
    """story #30 —— close_batch 写 finished_at、最终 status、算出 duration_s。"""
    _init_all(manifest)
    batch_id = manifest.open_batch()
    manifest.record_input(batch_id, "a.xlsx", "h1", "xlsx", n_facts=12)
    manifest.close_batch(batch_id, status=BATCH_DONE)

    rec = manifest.get_batch(batch_id)
    assert rec.status == BATCH_DONE
    assert rec.finished_at
    assert rec.duration_s is not None
    assert rec.duration_s >= 0


def test_close_batch_failed_status(manifest):
    """story #30 —— 整批失败时 close 为 failed 状态可台账查询。"""
    _init_all(manifest)
    batch_id = manifest.open_batch()
    manifest.close_batch(batch_id, status=BATCH_FAILED)
    rec = manifest.get_batch(batch_id)
    assert rec.status == BATCH_FAILED


def test_get_missing_batch_returns_none(manifest):
    """story #30 —— 取不存在的批次返回 None（不抛异常）。"""
    _init_all(manifest)
    assert manifest.get_batch("no-such-batch") is None


def test_list_batches_returns_all(manifest):
    """story #30 —— list_batches 列出全部批次（运维总览）。"""
    _init_all(manifest)
    b1 = manifest.open_batch("b1")
    manifest.close_batch(b1)
    b2 = manifest.open_batch("b2")
    manifest.close_batch(b2)

    batches = manifest.list_batches()
    ids = {b.batch_id for b in batches}
    assert {"b1", "b2"} <= ids


def test_manifest_persists_across_reopen(tmp_sqlite_factory):
    """story #30 —— 台账写盘后重开连接仍可完整读回（持久化）。"""
    path = tmp_sqlite_factory("manifest")
    ms1 = ManifestStore(path)
    ms1.init_schema()
    batch_id = ms1.open_batch("persist-batch")
    ms1.record_input(batch_id, "a.xlsx", "h1", "xlsx", n_facts=12, n_warnings=1)
    ms1.close_batch(batch_id)
    ms1.close()

    ms2 = ManifestStore(path)
    try:
        rec = ms2.get_batch("persist-batch")
        assert rec is not None
        assert rec.n_facts == 12
        assert len(rec.inputs) == 1
        assert rec.status == BATCH_DONE
    finally:
        ms2.close()


# ===========================================================================
# story #31 — compute_metrics：抽取量 / 告警率 / 复核积压 / 置信度分布桶
# ===========================================================================
def test_compute_metrics_total_facts(manifest, queue, store):
    """story #31 —— 抽取量：事实总数从 store 汇总进指标。"""
    _init_all(manifest, queue, store)
    store.upsert_facts([
        _fact("REVENUE", "2024", 2680.0, confidence=0.95),
        _fact("NEWSALES", "2024", 4750.0, confidence=0.9),
        _fact("PROFIT", "2024", 2210.0, confidence=0.6),
    ])
    metrics = compute_metrics(manifest, queue, store)
    assert metrics["n_facts_total"] == 3


def test_compute_metrics_review_backlog(manifest, queue, store):
    """story #31 —— 复核积压数 = 当前 pending 复核项数。"""
    _init_all(manifest, queue, store)
    queue.enqueue("low_confidence", {"v": 1}, "sheet=HK!C4", priority=10)
    queue.enqueue("unconfirmed_mapping", {"v": 2}, "sheet=HK!C5", priority=20)
    approved = queue.enqueue("low_confidence", {"v": 3}, "sheet=HK!C6", priority=30)
    queue.approve(approved, actor="sme_fin")

    metrics = compute_metrics(manifest, queue, store)
    # 三项中一项已 approve，积压应为 2
    assert metrics["review_backlog"] == 2


def test_compute_metrics_confidence_buckets(manifest, queue, store):
    """story #31 —— 置信度分布桶：按区间统计事实置信度计数。"""
    _init_all(manifest, queue, store)
    store.upsert_facts([
        _fact("REVENUE", "2024", 1.0, confidence=0.95),   # 高
        _fact("NEWSALES", "2024", 2.0, confidence=0.85),    # 高
        _fact("PROFIT", "2024", 3.0, confidence=0.7),    # 中
        _fact("ROE", "2024", 4.0, confidence=0.3),     # 低
    ])
    metrics = compute_metrics(manifest, queue, store)
    buckets = metrics["confidence_buckets"]
    # 桶内计数总和 = 有置信度的事实数（此处 4 条都带 confidence）
    assert sum(buckets.values()) == 4
    # 至少有一条落进低置信桶、两条落进高置信桶
    assert any(v >= 2 for v in buckets.values())


def test_compute_metrics_warning_rate(manifest, queue, store):
    """story #31 —— 告警率从 manifest 台账的告警数 / 输入数推导。"""
    _init_all(manifest, queue, store)
    batch_id = manifest.open_batch()
    manifest.record_input(batch_id, "a.xlsx", "h1", "xlsx", n_facts=12, n_warnings=1)
    manifest.record_input(batch_id, "b.xlsx", "h2", "xlsx", n_facts=8, n_warnings=3)
    manifest.close_batch(batch_id)

    metrics = compute_metrics(manifest, queue, store)
    assert "warning_rate" in metrics
    assert metrics["warning_rate"] >= 0.0


def test_compute_metrics_returns_dict(manifest, queue, store):
    """story #31 —— compute_metrics 返回 dict，且空状态下不报错。"""
    _init_all(manifest, queue, store)
    metrics = compute_metrics(manifest, queue, store)
    assert isinstance(metrics, dict)
    assert metrics["n_facts_total"] == 0
    assert metrics["review_backlog"] == 0


# ===========================================================================
# story #33 — list_versions：生产配置清单
# ===========================================================================
def test_list_versions_extractor_versions(store, registry):
    """story #33 —— 事实表中出现过的 extractor_version 去重清单。"""
    _init_all(store, registry)
    store.upsert_facts([
        _fact("REVENUE", "2023", 2350.0, extractor_version="xlsx_styled@1"),
        _fact("REVENUE", "2024", 2680.0, extractor_version="xlsx_styled@1"),
        _fact("NEWSALES", "2024", 4750.0, extractor_version="xlsx_styled@2"),
    ])
    versions = list_versions(store, registry)
    assert set(versions["extractor_versions"]) == {"xlsx_styled@1", "xlsx_styled@2"}


def test_list_versions_active_mappings(store, registry):
    """story #33 —— registry 各 scope 当前 active 的映射版本被列出。"""
    _init_all(store, registry)
    scope = "excel_styled_fixture.xlsx"
    entries = [LegendEntry(rgb="FFFF00", meaning="黄色=新产品线",
                           tag_key="product_line", tag_value="new")]
    v1 = registry.register_draft(ColorMapping(scope=scope, entries=entries))
    registry.confirm(scope, v1, actor="sme_fin")

    versions = list_versions(store, registry)
    assert versions["active_mappings"].get(scope) == v1


def test_list_versions_reflects_new_active_version(store, registry):
    """story #33/#26 —— 映射修订生成新 active 版本后，清单反映最新生效版本。"""
    _init_all(store, registry)
    scope = "excel_styled_fixture.xlsx"
    e1 = [LegendEntry(rgb="FFFF00", meaning="黄色=新产品线",
                      tag_key="product_line", tag_value="new")]
    v1 = registry.register_draft(ColorMapping(scope=scope, entries=e1))
    registry.confirm(scope, v1, actor="sme_fin")

    e2 = e1 + [LegendEntry(rgb="92D050", meaning="绿色=成熟产品线",
                           tag_key="product_line", tag_value="mature")]
    v2 = registry.register_draft(ColorMapping(scope=scope, entries=e2))
    registry.confirm(scope, v2, actor="sme_fin", note="补绿色")

    versions = list_versions(store, registry)
    # 当前生效版本是 v2（修订生成新版本而非覆盖）
    assert versions["active_mappings"].get(scope) == v2


def test_list_versions_returns_dict_when_empty(store, registry):
    """story #33 —— 空 store / 空 registry 时仍返回结构完整的 dict。"""
    _init_all(store, registry)
    versions = list_versions(store, registry)
    assert isinstance(versions, dict)
    assert versions["extractor_versions"] == []
    assert versions["active_mappings"] == {}
