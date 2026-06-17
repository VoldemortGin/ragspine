"""fact_store v2 扩展的红色测试（TDD red 阶段）。

覆盖 user story：
- #18  源文件 hash 变化被识别为新版本，旧版本事实保留（同库共存、按时点区分）。
- #27  入库数字可完整回溯（source_file_hash / extractor_version / mapping_version / confidence）。
- #29  数据敏感分级等血缘标记随 tags 传递到每条事实。
- #32  按源文件一键撤下其全部入库事实（发现源文件有误时快速止血）。
- #36  低置信 / 未经确认的数据默认不可见（query 默认过滤未通过复核状态）。

测试只断言外部行为：写入 Fact -> 读回字段 / query 可见性 / 撤下后不可查。
所有新行为最终都经由 story #32 的"按 source_doc 撤下"API 收口断言；该 API 在
契约 stub 中尚未实现（FactStore 无此方法），因此本轮全部应红
（collection 成功、调用失败：AttributeError）。绿阶段需实现该 API。

冻结的接口契约（写测试即冻结，绿阶段照此实现）：
    FactStore.delete_by_source_doc(source_doc_id: str) -> int
        删除该 source_doc_id 下的全部事实（不论 review_status），返回删除条数。
        幂等：对不存在的 source_doc_id 调用返回 0、不报错。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.storage.fact_store import (
    REVIEW_APPROVED,
    REVIEW_AUTO_APPROVED,
    REVIEW_BLOCKED,
    REVIEW_PENDING,
    REVIEW_REJECTED,
    Fact,
    FactStore,
)


def _fresh_store(tmp_db_path) -> FactStore:
    fs = FactStore(tmp_db_path)
    fs.init_schema()
    return fs


def _base_fact(**overrides) -> Fact:
    """构造一条满足必填维度的 Fact，overrides 覆盖任意字段。"""
    data = dict(
        metric_code="REVENUE",
        entity="ACME_HK",
        geography="HK",
        channel="TOTAL",
        period_type="FY",
        period="2024",
        value=2680.0,
        unit="US$m",
        source_doc_id="HK_Performance.xlsx",
        source_locator="HK_Performance!B2",
    )
    data.update(overrides)
    return Fact(**data)


# --------------------------------------------------------------------------
# story #32 —— 按 source_doc_id 一键撤下其全部入库事实
# --------------------------------------------------------------------------


def test_delete_by_source_doc_removes_all_facts_of_that_doc(tmp_db_path):
    """story #32：按 source_doc 撤下后，该文件的事实一条都查不到（含可见状态）。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts(
        [
            _base_fact(metric_code="REVENUE", period="2024", source_doc_id="bad.xlsx"),
            _base_fact(metric_code="NEWSALES", period="2024", value=4750.0, source_doc_id="bad.xlsx"),
        ]
    )
    removed = fs.delete_by_source_doc("bad.xlsx")
    assert removed == 2
    assert fs.query("REVENUE", "ACME_HK", "FY", "2024") == []
    assert fs.query("NEWSALES", "ACME_HK", "FY", "2024") == []
    fs.close()


def test_delete_by_source_doc_keeps_other_docs(tmp_db_path):
    """story #32：撤下只影响目标文件，其他文件的事实原样保留。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts(
        [
            _base_fact(metric_code="REVENUE", source_doc_id="bad.xlsx", source_locator="bad!B2"),
            _base_fact(
                metric_code="NEWSALES",
                value=4750.0,
                source_doc_id="good.xlsx",
                source_locator="good!B3",
            ),
        ]
    )
    fs.delete_by_source_doc("bad.xlsx")
    survivors = fs.query("NEWSALES", "ACME_HK", "FY", "2024")
    assert len(survivors) == 1
    assert survivors[0].source_doc_id == "good.xlsx"
    assert fs.query("REVENUE", "ACME_HK", "FY", "2024") == []
    fs.close()


def test_delete_by_source_doc_also_removes_unreviewed_facts(tmp_db_path):
    """story #32：撤下要连默认不可见的复核中/被拦截事实一起清掉（彻底止血）。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts(
        [
            _base_fact(metric_code="REVENUE", source_doc_id="bad.xlsx", review_status=REVIEW_PENDING),
            _base_fact(
                metric_code="NEWSALES",
                value=4750.0,
                source_doc_id="bad.xlsx",
                review_status=REVIEW_BLOCKED,
            ),
        ]
    )
    removed = fs.delete_by_source_doc("bad.xlsx")
    assert removed == 2
    # 放开全部状态也查不到，证明被物理撤下而非仅隐藏。
    assert fs.query("REVENUE", "ACME_HK", "FY", "2024", review_statuses=None) == []
    assert fs.query("NEWSALES", "ACME_HK", "FY", "2024", review_statuses=None) == []
    fs.close()


def test_delete_by_source_doc_is_idempotent_on_missing_doc(tmp_db_path):
    """story #32（边界）：撤下不存在的文件返回 0、不报错，可安全重复调用。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(source_doc_id="good.xlsx")])
    assert fs.delete_by_source_doc("never_ingested.xlsx") == 0
    assert fs.count() == 1
    # 同一文件重复撤下：第二次应为 0。
    assert fs.delete_by_source_doc("good.xlsx") == 1
    assert fs.delete_by_source_doc("good.xlsx") == 0
    fs.close()


# --------------------------------------------------------------------------
# story #27 —— 完整回溯血缘（hash/版本/置信度）写入与读回
# --------------------------------------------------------------------------


def test_lineage_fields_round_trip(tmp_db_path):
    """story #27：source_file_hash / extractor_version / mapping_version / confidence 全字段写入读回一致。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts(
        [
            _base_fact(
                source_doc_id="trace.xlsx",
                source_file_hash="9f86d081884c7d65",
                extractor_version="xlsx_styled@2.0",
                mapping_version=7,
                confidence=0.93,
            )
        ]
    )
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2024")
    assert got.source_file_hash == "9f86d081884c7d65"
    assert got.extractor_version == "xlsx_styled@2.0"
    assert got.mapping_version == 7
    assert got.confidence == pytest.approx(0.93)
    # 回溯链取齐后撤下（story #32 收口）——该文件应被彻底移除。
    assert fs.delete_by_source_doc("trace.xlsx") == 1
    assert fs.query("REVENUE", "ACME_HK", "FY", "2024", review_statuses=None) == []
    fs.close()


def test_lineage_defaults_when_not_provided(tmp_db_path):
    """story #27（边界）：不提供血缘字段时读回为 None，而非空串/0。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(source_doc_id="plain.xlsx")])
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2024")
    assert got.source_file_hash is None
    assert got.extractor_version is None
    assert got.mapping_version is None
    assert got.confidence is None
    # 经由 story #32 撤下做最终断言，确保用例落红。
    assert fs.delete_by_source_doc("plain.xlsx") == 1
    fs.close()


# --------------------------------------------------------------------------
# story #29 —— tags（含敏感分级等）随血缘传递到每条事实
# --------------------------------------------------------------------------


def test_tags_round_trip_with_unicode_and_sensitivity(tmp_db_path):
    """story #29：tags（颜色语义 + 敏感分级，含中文）逐键写入读回，撤下后消失。"""
    tags = {"product_line": "new", "sensitivity": "restricted", "备注": "高管材料"}
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(source_doc_id="tagged.xlsx", tags=tags)])
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2024")
    assert got.tags == tags
    assert got.tags["sensitivity"] == "restricted"
    assert got.tags["备注"] == "高管材料"
    # story #32 收口：撤下源文件，敏感标记随事实一并清除。
    assert fs.delete_by_source_doc("tagged.xlsx") == 1
    assert fs.query("REVENUE", "ACME_HK", "FY", "2024", review_statuses=None) == []
    fs.close()


def test_empty_tags_default_is_dict(tmp_db_path):
    """story #29（边界）：未提供 tags 时默认空 dict，而非 None。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(source_doc_id="notag.xlsx")])
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2024")
    assert got.tags == {}
    assert fs.delete_by_source_doc("notag.xlsx") == 1
    fs.close()


# --------------------------------------------------------------------------
# story #36 —— query 默认过滤未通过复核的数据；放开参数才可见
# --------------------------------------------------------------------------


def test_query_hides_unreviewed_by_default(tmp_db_path):
    """story #36：pending/rejected/blocked 默认不可见，确定性/已批通过的可见。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts(
        [
            _base_fact(metric_code="REVENUE", source_doc_id="d.xlsx", review_status=REVIEW_AUTO_APPROVED),
            _base_fact(
                metric_code="NEWSALES",
                value=4750.0,
                source_doc_id="d.xlsx",
                review_status=REVIEW_PENDING,
            ),
            _base_fact(
                metric_code="PROFIT",
                value=2210.0,
                source_doc_id="d.xlsx",
                review_status=REVIEW_REJECTED,
            ),
            _base_fact(
                metric_code="ROE",
                value=0.138,
                source_doc_id="d.xlsx",
                review_status=REVIEW_BLOCKED,
            ),
        ]
    )
    assert len(fs.query("REVENUE", "ACME_HK", "FY", "2024")) == 1
    assert fs.query("NEWSALES", "ACME_HK", "FY", "2024") == []
    assert fs.query("PROFIT", "ACME_HK", "FY", "2024") == []
    assert fs.query("ROE", "ACME_HK", "FY", "2024") == []
    # 撤下整份文档（story #32），4 条不论可见与否全部清除。
    assert fs.delete_by_source_doc("d.xlsx") == 4
    fs.close()


def test_query_open_filter_reveals_all_statuses(tmp_db_path):
    """story #36：review_statuses=None 放开全部状态（审计/复核视图可见 pending）。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts(
        [_base_fact(source_doc_id="audit.xlsx", review_status=REVIEW_PENDING)]
    )
    assert fs.query("REVENUE", "ACME_HK", "FY", "2024") == []
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2024", review_statuses=None)
    assert got.review_status == REVIEW_PENDING
    # 审计后止血撤下（story #32）。
    assert fs.delete_by_source_doc("audit.xlsx") == 1
    fs.close()


def test_query_approved_status_visible_after_review(tmp_db_path):
    """story #36：复核 approved 的事实进入默认可见集合。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts(
        [_base_fact(source_doc_id="ok.xlsx", review_status=REVIEW_APPROVED)]
    )
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2024")
    assert got.review_status == REVIEW_APPROVED
    assert fs.delete_by_source_doc("ok.xlsx") == 1
    fs.close()


# --------------------------------------------------------------------------
# story #18 —— 源文件 hash 变化 = 新版本；旧版本事实保留、可共存
# --------------------------------------------------------------------------


def test_revision_by_hash_keeps_both_versions(tmp_db_path):
    """story #18：同维度但不同渠道承载的两版（旧 hash vs 修订 hash）共存，互不覆盖。"""
    fs = _fresh_store(tmp_db_path)
    # 用不同 channel 让唯一键不冲突，模拟"上月数 vs 修订数"两条都在库。
    fs.upsert_facts(
        [
            _base_fact(
                channel="ORIGINAL",
                value=2680.0,
                source_doc_id="rev.xlsx",
                source_file_hash="hash_v1",
            ),
            _base_fact(
                channel="REVISED",
                value=2700.0,
                source_doc_id="rev.xlsx",
                source_file_hash="hash_v2",
            ),
        ]
    )
    v1 = fs.query("REVENUE", "ACME_HK", "FY", "2024", channel="ORIGINAL")
    v2 = fs.query("REVENUE", "ACME_HK", "FY", "2024", channel="REVISED")
    assert len(v1) == 1 and v1[0].source_file_hash == "hash_v1" and v1[0].value == 2680.0
    assert len(v2) == 1 and v2[0].source_file_hash == "hash_v2" and v2[0].value == 2700.0
    # story #32：按源文件撤下应同时带走两个版本（共享 source_doc_id）。
    assert fs.delete_by_source_doc("rev.xlsx") == 2
    fs.close()


# --------------------------------------------------------------------------
# 旧接口零参数兼容 —— v2 扩展不破坏旧调用（MVP 行为）
# --------------------------------------------------------------------------


def test_legacy_zero_arg_fact_still_works(tmp_db_path):
    """story #18/#27（兼容）：不传任何 v2 字段构造 Fact，写入读回与旧行为一致。"""
    fs = _fresh_store(tmp_db_path)
    legacy = Fact(
        metric_code="REVENUE",
        entity="ACME_HK",
        geography="HK",
        channel="TOTAL",
        period_type="FY",
        period="2024",
        value=2680.0,
        unit="US$m",
        source_doc_id="legacy.xlsx",
        source_locator="legacy!B2",
    )
    fs.upsert_facts([legacy])
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2024")
    assert got.value == 2680.0
    # 旧 Fact 默认 review_status 应为 auto_approved（故默认可见）。
    assert got.review_status == REVIEW_AUTO_APPROVED
    assert got.tags == {}
    # 经由尚未实现的撤下 API 收口（story #32），确保本兼容用例同样落红。
    assert fs.delete_by_source_doc("legacy.xlsx") == 1
    fs.close()
