"""dim_key（确定性自然身份键）+ Fact.dimensions 维度袋的回归测试。

ADR 0004 步骤 7+8：fact_metric 新增可空 dim_key 列，UNIQUE(dim_key) 复现旧
(metric,entity,period_type,period,channel) 唯一性；Fact 新增内存态 dimensions
袋（不入 DB、保留名守卫、空时从身份列派生）。dim_key 是 storage-only：从类型化
身份列重算、从不回灌进 Fact。

覆盖：
- 同身份重复 upsert → 1 行（dim_key 复现旧 composite 唯一性）；
  仅 channel 不同 / 仅 period 不同 → 各自独立、各自可查。
- dimensions 保留名守卫（命中保留名 raise ValueError，普通名合法）。
- period_type 前缀单射（('FY','2024') 与 ('HY','2024') 不同 key）。
- round-trip：query 返回的 Fact 无 dim_key 属性、dimensions 被重新派生、无 TypeError。
- 契约冻结：前 10 字段顺序、dimensions 为最后字段、10 元组绑前 10 字段。
"""

import dataclasses
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.storage.fact_store import Fact, FactStore, _compute_dim_key


def _fresh_store(tmp_db_path) -> FactStore:
    fs = FactStore(tmp_db_path)
    fs.init_schema()
    return fs


def _base_fact(**overrides) -> Fact:
    data = dict(
        metric_code="REVENUE",
        entity="ACME_HK",
        geography="HK",
        channel="TOTAL",
        period_type="FY",
        period="2025",
        value=1702.0,
        unit="USD_M",
        source_doc_id="ACME_FY2025_Results.pptx",
        source_locator="slide=5,table=1,row=2,col=3",
    )
    data.update(overrides)
    return Fact(**data)


# ===========================================================================
# 唯一性：dim_key 复现旧 (metric,entity,period_type,period,channel) composite
# ===========================================================================
def test_same_identity_upsert_yields_single_row(tmp_db_path):
    """同身份两条 fact upsert → 表里 1 行（dim_key 唯一性复现旧 composite）。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(value=1702.0)])
    fs.upsert_facts([_base_fact(value=1750.0)])  # 同身份、新值 → 替换
    assert fs.count() == 1
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2025")
    assert got.value == 1750.0
    fs.close()


def test_channel_difference_is_distinct_dim_key(tmp_db_path):
    """仅 channel 不同 → 各自独立 dim_key、两行共存、各自可查。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(channel="TOTAL", value=1702.0)])
    fs.upsert_facts([_base_fact(channel="AGENCY", value=1200.0)])
    assert fs.count() == 2
    [total] = fs.query("REVENUE", "ACME_HK", "FY", "2025", channel="TOTAL")
    [agency] = fs.query("REVENUE", "ACME_HK", "FY", "2025", channel="AGENCY")
    assert total.value == 1702.0
    assert agency.value == 1200.0
    assert _compute_dim_key(_base_fact(channel="TOTAL")) != _compute_dim_key(
        _base_fact(channel="AGENCY")
    )
    fs.close()


def test_period_difference_is_distinct_dim_key(tmp_db_path):
    """仅 period 不同 → 各自独立 dim_key、两行共存、各自可查。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(period="2025", value=1702.0)])
    fs.upsert_facts([_base_fact(period="2024", value=1538.0)])
    assert fs.count() == 2
    [y25] = fs.query("REVENUE", "ACME_HK", "FY", "2025")
    [y24] = fs.query("REVENUE", "ACME_HK", "FY", "2024")
    assert y25.value == 1702.0
    assert y24.value == 1538.0
    fs.close()


# ===========================================================================
# 保留名守卫
# ===========================================================================
def test_reserved_dimension_name_rejected():
    """dimensions 命中保留名（如 value）→ raise ValueError（防止遮蔽血缘/结构列）。"""
    with pytest.raises(ValueError):
        _base_fact(dimensions={"value": "x"})


def test_non_reserved_dimension_name_accepted():
    """dimensions 用普通名（site）合法，原样保留。"""
    f = _base_fact(dimensions={"site": "SH"})
    assert f.dimensions == {"site": "SH"}


# ===========================================================================
# period_type 前缀单射
# ===========================================================================
def test_period_type_prefix_injectivity():
    """('FY','2024') 与 ('HY','2024') 经 period_type 前缀 → dim_key 不同。"""
    fy = _base_fact(period_type="FY", period="2024")
    hy = _base_fact(period_type="HY", period="2024")
    assert _compute_dim_key(fy) != _compute_dim_key(hy)


# ===========================================================================
# round-trip：dim_key 不回灌、dimensions 重新派生、无 TypeError
# ===========================================================================
def test_round_trip_fact_has_no_dim_key_attr_and_rederives_dimensions(tmp_db_path):
    """upsert → query 返回的 Fact 无 dim_key 属性、dimensions 被重新派生、全程无 TypeError。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact()])
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2025")
    assert hasattr(got, "dim_key") is False
    assert got.dimensions == {
        "metric": "REVENUE",
        "entity": "ACME_HK",
        "channel": "TOTAL",
        "period": "FY2025",
    }
    fs.close()


# ===========================================================================
# 契约冻结：字段顺序 / dimensions 为最后 / 10 元组绑前 10 字段
# ===========================================================================
def test_first_ten_fields_frozen():
    """前 10 个 dataclass 字段名顺序冻结（结构身份 + 数值 + 血缘）。"""
    names = [f.name for f in dataclasses.fields(Fact)]
    assert names[:10] == [
        "metric_code",
        "entity",
        "geography",
        "channel",
        "period_type",
        "period",
        "value",
        "unit",
        "source_doc_id",
        "source_locator",
    ]


def test_dimensions_is_last_field():
    """dimensions 是 Fact 的最后一个字段（additive-only，never reposition）。"""
    names = [f.name for f in dataclasses.fields(Fact)]
    assert names[-1] == "dimensions"


def test_ten_tuple_binds_first_ten_fields():
    """一个 10 元组 Fact(*row) 绑定前 10 字段，其余取默认（qa_eval._EVAL_FACT_ROWS 形态）。"""
    row10 = (
        "REVENUE",
        "ACME_HK",
        "HK",
        "TOTAL",
        "FY",
        "2025",
        1702.0,
        "USD_M",
        "doc.pptx",
        "slide=5",
    )
    f = Fact(*row10)
    assert f.metric_code == "REVENUE"
    assert f.source_locator == "slide=5"
    assert f.value == 1702.0
    # 其余字段取默认。
    assert f.review_status == "auto_approved"
    assert f.valid_as_of is None
    # dimensions 默认空 → __post_init__ 从身份列派生。
    assert f.dimensions == {
        "metric": "REVENUE",
        "entity": "ACME_HK",
        "channel": "TOTAL",
        "period": "FY2025",
    }
