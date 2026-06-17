"""冻结金标：query_metric 工具 schema（ADR 0004 pin-before-touch）。

ADR 0004 会把工具 schema 改为从 DomainProfile.dimensions 动态生成。本测试钉住当前
（金融 ACME 实例）的生成结果【字节级不变】：属性插入顺序 metric→entity→period→channel、
required==[metric,entity,period]、以及含实体/指标同义词的逐字 description。
金标存 data/golden/query_metric_tool_schema.json（由当前值生成、force-tracked），
是后续"动态生成字节级等价"的回归门。
"""

import json
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.query_tools import (
    QUERY_METRIC_TOOL_ANTHROPIC,
    QUERY_METRIC_TOOL_OPENAI,
    execute_fact_query,
    execute_query_metric,
)
from ragspine.common.company_profile import load_company_profile
from ragspine.storage.fact_store import Fact, FactStore

_GOLDEN = json.loads(
    (ROOT_DIR / "data" / "golden" / "query_metric_tool_schema.json").read_text(encoding="utf-8")
)


def test_anthropic_tool_schema_byte_identical():
    assert QUERY_METRIC_TOOL_ANTHROPIC == _GOLDEN["anthropic"]


def test_openai_tool_schema_byte_identical():
    assert QUERY_METRIC_TOOL_OPENAI == _GOLDEN["openai"]


def test_tool_schema_structural_invariants():
    """属性顺序 + required 是动态生成必须复现的硬结构。"""
    sch = QUERY_METRIC_TOOL_ANTHROPIC["input_schema"]
    assert QUERY_METRIC_TOOL_ANTHROPIC["name"] == "query_metric"
    assert list(sch["properties"].keys()) == ["metric", "entity", "period", "channel"]
    assert sch["required"] == ["metric", "entity", "period"]

    fn = QUERY_METRIC_TOOL_OPENAI["function"]
    assert fn["name"] == "query_metric"
    assert list(fn["parameters"]["properties"].keys()) == ["metric", "entity", "period", "channel"]
    assert fn["parameters"]["required"] == ["metric", "entity", "period"]


# ---------------------------------------------------------------------------
# execute_query_metric / execute_fact_query 行为回归（ADR 0004 步骤 10）
# ---------------------------------------------------------------------------

_FACT = Fact(
    metric_code="REVENUE", entity="ACME_HK", geography="HK", channel="AGENCY",
    period_type="FY", period="2024", value=1234.0, unit="USD_M",
    source_doc_id="ACME_FY2024.pptx", source_locator="slide=1,table=1,row=1,col=1",
)


@pytest.fixture
def store(tmp_db_path):
    fs = FactStore(tmp_db_path)
    fs.init_schema()
    fs.upsert_facts([_FACT])
    yield fs
    fs.close()


def test_channel_typo_is_not_coerced_to_total(store):
    """识别得了 metric/entity/period 但 channel 是不认识的字符串 -> 字面直通，store 查不到
    -> not_found（绝不被静默强转 TOTAL）。冻结 channel 直通语义。"""
    res = execute_query_metric(store, "REVENUE", "ACME_HK", "FY2024", "AGENTS_TYPO")
    assert res["status"] == "not_found"
    assert "value" not in res
    # 归一记录里 channel 必须保留原 typo 的大写形态，证明未回退 TOTAL。
    assert res["normalized"]["channel"] == "AGENTS_TYPO"


def test_known_channel_found_keeps_period_type_and_period_separate(store):
    """传对了渠道同义词（agency -> AGENCY）-> found；found-dict 把 period_type 与 period
    作为两个分开的键返回（agent 两期差值比较 + _period_label 依赖）。"""
    res = execute_query_metric(store, "REVENUE", "ACME_HK", "FY2024", "AGENCY")
    assert res["status"] == "found"
    assert res["value"] == 1234.0
    assert res["period_type"] == "FY"
    assert res["period"] == "2024"
    assert res["channel"] == "AGENCY"
    assert res["source"] == {"doc": "ACME_FY2024.pptx", "locator": "slide=1,table=1,row=1,col=1"}


def test_execute_query_metric_delegates_to_execute_fact_query(store):
    """对外冻结的 execute_query_metric 与泛化 execute_fact_query 同结果（默认 profile）。"""
    via_wrapper = execute_query_metric(store, "REVENUE", "ACME_HK", "FY2024", "AGENCY")
    via_generic = execute_fact_query(
        store, load_company_profile(),
        metric="REVENUE", entity="ACME_HK", period="FY2024", channel="AGENCY",
    )
    assert via_wrapper == via_generic


def test_empty_channel_defaults_to_total(store):
    """缺省 / 空 channel 才触发 default TOTAL（与 typo 区别对待）。"""
    fs = store
    fs.upsert_facts([
        Fact(
            metric_code="PROFIT", entity="ACME_HK", geography="HK", channel="TOTAL",
            period_type="FY", period="2024", value=99.0, unit="USD_M",
            source_doc_id="d", source_locator="loc",
        )
    ])
    res = execute_fact_query(
        store, load_company_profile(), metric="PROFIT", entity="ACME_HK", period="FY2024", channel=""
    )
    assert res["status"] == "found"
    assert res["channel"] == "TOTAL"
