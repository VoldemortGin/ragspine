"""DX 缝：Fact.metric keyword-only 建造器 + SqliteFactStore.has_source_doc 存在性助手。

- Fact.metric：免受 10 个位置字段顺序之扰，只按名字传；与位置构造器等价，v2 可选字段透传。
- has_source_doc：某 source_doc_id 是否已有任一事实（不论 review_status），存在性探测。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.storage.fact_store import (
    REVIEW_PENDING,
    Fact,
    SqliteFactStore,
)


def _fresh_store(tmp_path) -> SqliteFactStore:
    fs = SqliteFactStore(tmp_path / "facts.db")
    fs.init_schema()
    return fs


# --------------------------------------------------------------------------
# Fact.metric —— keyword-only 建造器
# --------------------------------------------------------------------------
def test_metric_builder_equals_positional_constructor():
    """按名字传的 Fact.metric 与位置构造器产出等价的 Fact（含 dim_key 一致）。"""
    positional = Fact(
        "REVENUE", "ACME_CN", "CN", "TOTAL", "FY", "2024",
        2680.0, "US$m", "q4.xlsx", "sheet=PL!B2",
    )
    built = Fact.metric(
        metric_code="REVENUE", entity="ACME_CN", geography="CN",
        period_type="FY", period="2024", value=2680.0, unit="US$m",
        source_doc_id="q4.xlsx", source_locator="sheet=PL!B2",
    )
    assert built == positional
    assert SqliteFactStore.dim_key_for(built) == SqliteFactStore.dim_key_for(positional)


def test_metric_builder_is_keyword_only():
    """前 10 个字段顺序敏感是要规避的痛点：Fact.metric 全 keyword-only，位置传参即报错。"""
    with pytest.raises(TypeError):
        Fact.metric(  # type: ignore[misc]
            "REVENUE", "ACME_CN", "FY", "2024", 2680.0, "US$m", "q4.xlsx", "sheet=PL!B2",
        )


def test_metric_builder_defaults_channel_total_and_empty_geography():
    """channel 缺省 'TOTAL'（同 query 口径）、geography 缺省 ''（无地理维）。"""
    built = Fact.metric(
        metric_code="REVENUE", entity="ACME_CN",
        period_type="FY", period="2024", value=1.0, unit="US$m",
        source_doc_id="d.xlsx", source_locator="l",
    )
    assert built.channel == "TOTAL"
    assert built.geography == ""


def test_metric_builder_passes_through_v2_fields():
    """v2 可选字段（confidence / review_status / tags ...）经 **extra 透传给位置构造器。"""
    built = Fact.metric(
        metric_code="REVENUE", entity="ACME_CN",
        period_type="FY", period="2024", value=1.0, unit="US$m",
        source_doc_id="d.xlsx", source_locator="l",
        confidence=0.98, review_status=REVIEW_PENDING, source_file_hash="abc123",
    )
    assert built.confidence == 0.98
    assert built.review_status == REVIEW_PENDING
    assert built.source_file_hash == "abc123"


def test_metric_builder_roundtrips_through_store(tmp_path):
    """建造器造的 Fact 能正常入库并按维度查回。"""
    fs = _fresh_store(tmp_path)
    fs.upsert_facts([
        Fact.metric(
            metric_code="REVENUE", entity="ACME_CN", geography="CN",
            period_type="FY", period="2024", value=2680.0, unit="US$m",
            source_doc_id="q4.xlsx", source_locator="sheet=PL!B2",
        )
    ])
    got = fs.query("REVENUE", "ACME_CN", "FY", "2024")
    assert len(got) == 1
    assert got[0].value == 2680.0
    assert got[0].source_doc_id == "q4.xlsx"


# --------------------------------------------------------------------------
# SqliteFactStore.has_source_doc —— 存在性助手
# --------------------------------------------------------------------------
def test_has_source_doc_true_after_ingest(tmp_path):
    fs = _fresh_store(tmp_path)
    fs.upsert_facts([
        Fact.metric(
            metric_code="REVENUE", entity="ACME_CN", geography="CN",
            period_type="FY", period="2024", value=2680.0, unit="US$m",
            source_doc_id="q4.xlsx", source_locator="l",
        )
    ])
    assert fs.has_source_doc("q4.xlsx") is True


def test_has_source_doc_false_for_unknown_doc(tmp_path):
    fs = _fresh_store(tmp_path)
    assert fs.has_source_doc("never_ingested.xlsx") is False


def test_has_source_doc_ignores_review_status(tmp_path):
    """存在性不看可见性：即使事实处于 PENDING（query 默认不可见），源文档仍算已 ingest。"""
    fs = _fresh_store(tmp_path)
    fs.upsert_facts([
        Fact.metric(
            metric_code="REVENUE", entity="ACME_CN", geography="CN",
            period_type="FY", period="2024", value=1.0, unit="US$m",
            source_doc_id="pending.xlsx", source_locator="l",
            review_status=REVIEW_PENDING,
        )
    ])
    # query 默认过滤掉 PENDING（不可见），但 has_source_doc 仍应为 True
    assert fs.query("REVENUE", "ACME_CN", "FY", "2024") == []
    assert fs.has_source_doc("pending.xlsx") is True
