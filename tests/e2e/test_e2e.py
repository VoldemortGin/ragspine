"""端到端测试：生成 → 抽取 → 入库 → 查询 → 对真值全量比对。

覆盖：全量数值一致、not_found 防编造、normalize_period 边界、function-calling schema。
"""

import json
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from scripts.make_synthetic_deck import GT_PATH, PPTX_PATH, XLSX_PATH, main as make_synthetic
from ragspine.extraction.extractors import pptx_extractor, xlsx_extractor
from ragspine.storage.fact_store import FactStore
from ragspine.common.glossary import normalize_entity, normalize_metric, normalize_period
from ragspine.agent.query_tools import (
    QUERY_METRIC_TOOL_ANTHROPIC,
    QUERY_METRIC_TOOL_OPENAI,
    execute_query_metric,
)


@pytest.fixture(scope="module", autouse=True)
def _ensure_synthetic():
    """合成数据在本模块任何测试前生成一次。test_extractors_no_warnings 不经 store
    fixture 却直读 data/synthetic/*，故不能只靠 store 触发生成——否则收集顺序一变
    （如新增 tests/conformance 后）该用例可能先跑而 FileNotFoundError。"""
    make_synthetic()


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """抽取合成数据入临时库，供整模块复用（合成数据由 _ensure_synthetic 预生成）。"""
    db_path = tmp_path_factory.mktemp("db") / "fact_metric.db"
    fs = FactStore(db_path)
    fs.init_schema()
    xf, _ = xlsx_extractor.extract_facts(XLSX_PATH)
    pf, _ = pptx_extractor.extract_facts(PPTX_PATH)
    fs.upsert_facts(xf + pf)
    yield fs
    fs.close()


@pytest.fixture(scope="module")
def ground_truth():
    """真值按 (metric,entity,period_type,period) 去重。"""
    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    index: dict[tuple, dict] = {}
    for row in gt:
        key = (row["metric"], row["entity"], row["period_type"], row["period"])
        assert key not in index or index[key]["value"] == row["value"], f"真值矛盾 {key}"
        index[key] = row
    return index


def test_extractors_no_warnings():
    xf, xw = xlsx_extractor.extract_facts(XLSX_PATH)
    pf, pw = pptx_extractor.extract_facts(PPTX_PATH)
    assert xw == []
    assert pw == []
    assert len(xf) == 12
    assert len(pf) == 24


def test_chart_path_lineage(store):
    """图表内嵌数据路径：血缘必须回指到 chart/series，证明零 OCR。"""
    res = execute_query_metric(store, "PROFIT", "ACME Group", "FY2024")
    assert res["status"] == "found"
    assert res["value"] == 6950.0
    assert "chart=1" in res["source"]["locator"]
    assert res["source"]["doc"] == "ACME_FY2024_Review.pptx"


def test_full_ground_truth_match(store, ground_truth):
    """对每条去重真值都应能查到且值/单位一致。"""
    for (metric, entity, ptype, period), truth in ground_truth.items():
        query_period = f"FY{period}" if ptype == "FY" else period
        res = execute_query_metric(store, metric, entity, query_period)
        assert res["status"] == "found", f"{(metric, entity, ptype, period)} -> {res}"
        assert abs(res["value"] - truth["value"]) < 1e-6
        assert res["unit"] == truth["unit"]
        assert res["source"]["doc"]
        assert res["source"]["locator"]


def test_chinese_synonyms(store):
    res = execute_query_metric(store, "营收", "香港", "2023")
    assert res["status"] == "found"
    assert res["metric_code"] == "REVENUE"
    assert res["entity"] == "ACME_HK"
    assert res["value"] == 2350.0


def test_not_found_no_fabrication(store):
    """中国页无 ROE：必须 not_found，绝不编造。"""
    res = execute_query_metric(store, "ROE", "ACME China", "FY2024")
    assert res["status"] == "not_found"
    assert "value" not in res
    assert res["normalized"]["metric_code"] == "ROE"
    assert res["normalized"]["entity"] == "ACME_CN"


def test_unrecognized_param(store):
    res = execute_query_metric(store, "净现金流量", "香港", "FY2024")
    assert res["status"] == "unrecognized_param"
    assert res["param"] == "metric"


def test_yoy_computation(store):
    cur = execute_query_metric(store, "REVENUE", "ACME_HK", "FY2024")
    prev = execute_query_metric(store, "REVENUE", "ACME_HK", "FY2023")
    yoy = (cur["value"] - prev["value"]) / prev["value"] * 100
    assert abs(yoy - 14.0425531914894) < 1e-6


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("FY2024", ("FY", "2024")),
        ("2024", ("FY", "2024")),
        ("FY 2024", ("FY", "2024")),
        ("2024H1", ("HY", "2024H1")),
        ("2024 H1", ("HY", "2024H1")),
        ("FY2024H1", ("HY", "2024H1")),
        ("2025Q1", ("QUARTER", "2025Q1")),
        ("2025 Q1", ("QUARTER", "2025Q1")),
        ("garbage", None),
        ("", None),
        (None, None),
        ("20245", None),
    ],
)
def test_normalize_period(raw, expected):
    assert normalize_period(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("REVENUE", "REVENUE"),
        ("营收", "REVENUE"),
        ("新签金额", "NEWSALES"),
        ("营运利润", "PROFIT"),
        ("ROE", "ROE"),
        ("REVENUE (US$m)", "REVENUE"),
        ("unknown metric", None),
        (None, None),
    ],
)
def test_normalize_metric(raw, expected):
    assert normalize_metric(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ACME Hong Kong", "ACME_HK"),
        ("香港", "ACME_HK"),
        ("ACME香港", "ACME_HK"),
        ("ACME China", "ACME_CN"),
        ("中国", "ACME_CN"),
        ("ACME Group", "ACME_GROUP"),
        ("ACME_HK", "ACME_HK"),
        ("ACME Hong Kong — Financial Performance", "ACME_HK"),
        ("nowhere", None),
    ],
)
def test_normalize_entity(raw, expected):
    assert normalize_entity(raw) == expected


def test_tool_schemas_shape():
    """function-calling schema：两种格式都齐备且参数定义对齐。"""
    assert QUERY_METRIC_TOOL_ANTHROPIC["name"] == "query_metric"
    assert set(QUERY_METRIC_TOOL_ANTHROPIC["input_schema"]["required"]) == {"metric", "entity", "period"}

    assert QUERY_METRIC_TOOL_OPENAI["type"] == "function"
    assert QUERY_METRIC_TOOL_OPENAI["function"]["name"] == "query_metric"
    props = QUERY_METRIC_TOOL_OPENAI["function"]["parameters"]["properties"]
    assert {"metric", "entity", "period", "channel"} <= set(props)


def test_upsert_idempotent(store):
    """重复入库不应增加条数（唯一索引去重）。"""
    before = store.count()
    xf, _ = xlsx_extractor.extract_facts(XLSX_PATH)
    store.upsert_facts(xf)
    assert store.count() == before
