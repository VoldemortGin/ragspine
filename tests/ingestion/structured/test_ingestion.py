"""单文件 Excel ingestion 编排的红色测试（TDD）。

只验证外部行为：给定合成 fixture（excel_styled_fixture.xlsx，其 HK_Performance
sheet 是 glossary 可识别的指标×期间区，A1='ACME Hong Kong'）与逐格 ground truth，
断言 ingest_excel 的对外输出（IngestReport 内容 + fact_store / queue 的状态）。

覆盖 PRD user stories：
    #16 单文件 dry-run 模式（只产抽取报告不入库）—— 报告完整、store/queue 零写入。
    #17 重复 ingest 同一文件完全幂等 —— 库内事实数不变，报告可见幂等。
    （并连带验证告警传递、status/error、血缘新字段、active 颜色映射打 tag。）

红色预期：所有用例因 ingest_excel stub raise NotImplementedError 而 FAIL
（收集成功、无 collection error、无意外 PASS）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.color.color_semantics import ColorMapping, LegendEntry, MappingRegistry
from ragspine.storage.fact_store import FactStore, REVIEW_AUTO_APPROVED, VISIBLE_REVIEW_STATUSES
from ragspine.ingestion.structured.ingestion import IngestReport, ingest_excel
from ragspine.ingestion.review.review_queue import ReviewQueue

# HK_Performance 是 fixture 里唯一 glossary 完整可识别的数据 sheet：
#   A1='ACME Hong Kong'（实体）/ 首行 FY2022..FY2024（期间）/ 首列 REVENUE/NEWSALES/PROFIT/ROE（指标）
# 共 4 指标 × 3 期间 = 12 个候选事实。
HK_SCOPE = "excel_styled_fixture.xlsx"
EXPECTED_HK_FACTS = 12


# --------------------------------------------------------------------------- #
# fixtures：只构造对象（开 sqlite 连接，不触发任何 NotImplementedError）。
# init_schema / 装配 active 映射等会 raise 的步骤一律放进测试体内首行调用，
# 以便契约未实现时 NotImplementedError 作为用例 FAILURE 而非 fixture ERROR 暴露
# （沿用 test_review_queue.py 的红色阶段约定）。
# --------------------------------------------------------------------------- #
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


def _setup_three(store, registry, queue, ground_truth):
    """建表并装配与 fixture 图例一致的 active 颜色映射（product_line: new/mature）。

    在测试体内调用——契约未实现时此处 NotImplementedError 使用例 FAIL（非 ERROR）。
    """
    store.init_schema()
    queue.init_schema()
    registry.init_schema()
    legend = ground_truth["sheets"]["HK_Performance"]["legend_expect"]
    entries = [
        LegendEntry(
            rgb=e["rgb"],
            meaning=e["meaning"],
            tag_key=e["tag_key"],
            tag_value=e["tag_value"],
        )
        for e in legend
    ]
    version = registry.register_draft(ColorMapping(scope=HK_SCOPE, entries=entries))
    registry.confirm(HK_SCOPE, version, actor="sme_fin", note="fixture 图例")


# ===========================================================================
# story #16 — dry-run：报告完整但 store / queue 零写入
# ===========================================================================
def test_dry_run_returns_report(store, registry, queue, excel_fixture_path, ground_truth):
    """story #16 —— dry_run 返回一份 IngestReport，dry_run 标记为 True。"""
    _setup_three(store, registry, queue, ground_truth)
    report = ingest_excel(
        excel_fixture_path, store, registry, queue, dry_run=True
    )
    assert isinstance(report, IngestReport)
    assert report.dry_run is True
    assert report.status == "ok"
    assert report.error is None


def test_dry_run_report_is_complete(store, registry, queue, excel_fixture_path, ground_truth):
    """story #16 —— dry_run 仍完整跑抽取：grid 数、候选事实数、血缘都齐备。"""
    _setup_three(store, registry, queue, ground_truth)
    report = ingest_excel(
        excel_fixture_path, store, registry, queue, dry_run=True
    )
    assert report.source_doc_id == HK_SCOPE
    assert report.file_hash  # 文件 hash 已算出
    assert report.n_grids >= 4  # fixture 有 4 个 worksheet
    # HK_Performance 的 12 个指标×期间候选事实必须被识别出来
    assert report.n_facts_extracted >= EXPECTED_HK_FACTS


def test_dry_run_writes_nothing_to_store(store, registry, queue, excel_fixture_path, ground_truth):
    """story #16 —— dry_run 绝不写 fact_store（库内事实数保持 0）。"""
    _setup_three(store, registry, queue, ground_truth)
    assert store.count() == 0
    report = ingest_excel(
        excel_fixture_path, store, registry, queue, dry_run=True
    )
    assert report.n_facts_ingested == 0
    assert store.count() == 0


def test_dry_run_writes_nothing_to_queue(store, registry, queue, excel_fixture_path, ground_truth):
    """story #16 —— dry_run 绝不写复核队列（pending 仍为空）。"""
    _setup_three(store, registry, queue, ground_truth)
    ingest_excel(excel_fixture_path, store, registry, queue, dry_run=True)
    assert queue.list_pending() == []


def test_dry_run_then_real_ingest_independent(store, registry, queue, excel_fixture_path, ground_truth):
    """story #16 —— 先 dry_run 预览、再正式 ingest，正式入库不受 dry_run 影响。"""
    _setup_three(store, registry, queue, ground_truth)
    ingest_excel(excel_fixture_path, store, registry, queue, dry_run=True)
    assert store.count() == 0
    report = ingest_excel(excel_fixture_path, store, registry, queue, dry_run=False)
    assert report.dry_run is False
    assert report.n_facts_ingested == EXPECTED_HK_FACTS
    assert store.count() == EXPECTED_HK_FACTS


# ===========================================================================
# story #17 — 重复 ingest 幂等：库内事实数不变
# ===========================================================================
def test_real_ingest_writes_expected_facts(store, registry, queue, excel_fixture_path, ground_truth):
    """story #17 —— 正式 ingest 把 HK_Performance 的 12 条事实写入 store。"""
    _setup_three(store, registry, queue, ground_truth)
    report = ingest_excel(excel_fixture_path, store, registry, queue)
    assert report.dry_run is False
    assert report.n_facts_ingested == EXPECTED_HK_FACTS
    assert store.count() == EXPECTED_HK_FACTS


def test_repeat_ingest_is_idempotent(store, registry, queue, excel_fixture_path, ground_truth):
    """story #17 —— 同一文件 ingest 两次，库内事实总数不增长（唯一键 upsert）。"""
    _setup_three(store, registry, queue, ground_truth)
    ingest_excel(excel_fixture_path, store, registry, queue)
    count_after_first = store.count()
    ingest_excel(excel_fixture_path, store, registry, queue)
    assert store.count() == count_after_first


def test_repeat_ingest_report_visible_idempotent(store, registry, queue, excel_fixture_path, ground_truth):
    """story #17 —— 重复 ingest 的报告仍报告应入库事实数，但不制造重复事实。"""
    _setup_three(store, registry, queue, ground_truth)
    first = ingest_excel(excel_fixture_path, store, registry, queue)
    second = ingest_excel(excel_fixture_path, store, registry, queue)
    # 两次报告的应入库事实数一致（幂等可见）
    assert second.n_facts_ingested == first.n_facts_ingested
    # 但库里没有翻倍
    assert store.count() == EXPECTED_HK_FACTS


def test_repeat_ingest_no_duplicate_for_same_cell(store, registry, queue, excel_fixture_path, ground_truth):
    """story #17 —— 同一指标×实体×期间×渠道在重复 ingest 后仍只有 1 条。"""
    _setup_three(store, registry, queue, ground_truth)
    ingest_excel(excel_fixture_path, store, registry, queue)
    ingest_excel(excel_fixture_path, store, registry, queue)
    rows = store.query("REVENUE", "ACME_HK", "FY", "2024", channel="TOTAL")
    assert len(rows) == 1
    assert rows[0].value == 2680.0


# ===========================================================================
# 血缘新字段 + active 颜色映射打 tag（入库侧契约）
# ===========================================================================
def test_ingested_facts_carry_lineage(store, registry, queue, excel_fixture_path, ground_truth):
    """story #16/#17 —— 入库事实带版本血缘新字段（hash / extractor_version）。"""
    _setup_three(store, registry, queue, ground_truth)
    report = ingest_excel(
        excel_fixture_path, store, registry, queue,
        extractor_version="xlsx_styled@1",
    )
    rows = store.query("REVENUE", "ACME_HK", "FY", "2024")
    assert len(rows) == 1
    fact = rows[0]
    assert fact.source_doc_id == HK_SCOPE
    assert fact.source_file_hash == report.file_hash
    assert fact.extractor_version == "xlsx_styled@1"
    assert fact.review_status in VISIBLE_REVIEW_STATUSES


def test_color_tags_applied_from_active_mapping(store, registry, queue, excel_fixture_path, ground_truth):
    """story #17 —— 黄色 REVENUE 行经 active 映射打上 product_line=new 的 tag。"""
    _setup_three(store, registry, queue, ground_truth)
    report = ingest_excel(excel_fixture_path, store, registry, queue)
    rows = store.query("REVENUE", "ACME_HK", "FY", "2024")
    assert len(rows) == 1
    assert rows[0].tags.get("product_line") == "new"
    assert report.n_tags_applied >= 1


def test_mature_line_tags_applied(store, registry, queue, excel_fixture_path, ground_truth):
    """story #17 —— 绿色 NEWSALES/PROFIT 行经 active 映射打 product_line=mature。"""
    _setup_three(store, registry, queue, ground_truth)
    ingest_excel(excel_fixture_path, store, registry, queue)
    newsales = store.query("NEWSALES", "ACME_HK", "FY", "2024")
    assert len(newsales) == 1
    assert newsales[0].tags.get("product_line") == "mature"


# ===========================================================================
# 未确认映射不静默入库：相关 tags 置空并告警（架构红线）
# ===========================================================================
def test_unconfirmed_mapping_yields_empty_tags_and_warning(
    store, queue, tmp_sqlite_factory, excel_fixture_path
):
    """story #16/#17 —— registry 无 active 映射时事实仍入库但颜色 tags 置空并告警。"""
    store.init_schema()
    queue.init_schema()
    empty_reg = MappingRegistry(tmp_sqlite_factory("registry_empty"))
    empty_reg.init_schema()
    try:
        report = ingest_excel(excel_fixture_path, store, empty_reg, queue)
        # 事实仍然入库（值不依赖颜色），但颜色 tag 为空
        rows = store.query("REVENUE", "ACME_HK", "FY", "2024")
        assert len(rows) == 1
        assert "product_line" not in rows[0].tags
        # 未确认映射必须产生告警，绝不静默
        assert any("map" in w.lower() or "映射" in w for w in report.warnings)
    finally:
        empty_reg.close()


# ===========================================================================
# 告警传递 + status / error
# ===========================================================================
def test_warnings_propagated_into_report(store, registry, queue, excel_fixture_path, ground_truth):
    """story #16 —— grid 级告警（条件格式 / 不可识别表头等）汇聚进 report.warnings。"""
    _setup_three(store, registry, queue, ground_truth)
    report = ingest_excel(excel_fixture_path, store, registry, queue, dry_run=True)
    assert isinstance(report.warnings, list)
    # fixture 含 CondFormat 与转置/合并等刁钻 sheet，至少应产出一条告警
    assert len(report.warnings) >= 1


def test_status_ok_on_clean_run(store, registry, queue, excel_fixture_path, ground_truth):
    """story #16 —— 正常文件 ingest 后 status='ok' 且 error 为 None。"""
    _setup_three(store, registry, queue, ground_truth)
    report = ingest_excel(excel_fixture_path, store, registry, queue)
    assert report.status == "ok"
    assert report.error is None


def test_missing_file_reports_failed(store, registry, queue, tmp_path, ground_truth):
    """story #16 —— 源文件不存在时 status='failed' 且 error 非空（不抛裸异常上抛）。"""
    _setup_three(store, registry, queue, ground_truth)
    missing = tmp_path / "does_not_exist.xlsx"
    report = ingest_excel(missing, store, registry, queue)
    assert report.status == "failed"
    assert report.error
    # 失败时不应有事实落库
    assert store.count() == 0
