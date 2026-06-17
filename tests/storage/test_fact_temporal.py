"""fact_metric 时效列（provenance）的红色测试（TDD red 阶段，任务 3B 收尾）。

接通入库主路径的「这个数哪天/哪版」能力：给 fact_metric 加两列并打通到答案。

    valid_as_of —— 事实「截至 / 生效」日期（调用方 / CLI 提供，可空），业务时点。
    ingested_at —— 写入时间戳（store 在 upsert 时统一盖 UTC ISO），审计用。

覆盖 user story（每个用例 docstring 带中文 user story）：
- T1 schema：建表后 PRAGMA 含 ingested_at 与 valid_as_of 两列。
- T2 迁移幂等：对仅含旧列的既有库再 init_schema → 补列、不报错、旧数据保留。
- T3 ingested_at 盖戳：upsert_facts(ingested_at=...) 可控；不传时为非空时间戳。
- T4 valid_as_of round-trip：Fact(valid_as_of=...) upsert → query 取回一致。
- T5 替换语义：同键 re-upsert（新 ingested_at）→ count 不增、取回新值 / 新戳。
- T6 答案打通：execute_query_metric found 含 valid_as_of；带 valid_as_of 的 fact
  文案含「截至 2025-12-31」；无 valid_as_of 的 fact 文案与现状字节级一致（回归守护）。
- T9 回归：既有无 valid_as_of 的 fact 全链路（query / 答案）不变。

诚实边界：本批是 provenance（盖戳 + 生效日 + 可见性），不是完整 bitemporal
版本史——唯一键仍替换、不保留历史版本。

红色预期（绿阶段才实现）：
- T1/T2：fact_metric 现无 valid_as_of / ingested_at 两列 → PRAGMA 缺列断言 FAIL。
- T3：upsert_facts 暂无 ingested_at 形参 → TypeError；读回 Fact 无 ingested_at 属性。
- T4：Fact 暂无 valid_as_of 字段 → 构造 TypeError。
- T5：依赖 ingested_at 形参与读回，FAIL。
- T6：execute_query_metric found 结果暂无 valid_as_of 键；_structured_answer /
  MockProvider._final_answer 暂不追加「截至」标注 → 断言 FAIL。
"""

import os
import re
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import AgentResult, answer_question, _structured_answer
from ragspine.storage.fact_store import Fact, FactStore
from ragspine.agent.llm_provider import MockProvider
from ragspine.agent.query_tools import execute_query_metric

REF = date(2026, 6, 13)


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
        period="2025",
        value=1702.0,
        unit="USD_M",
        source_doc_id="ACME_FY2025_Results.pptx",
        source_locator="slide=5,table=1,row=2,col=3",
    )
    data.update(overrides)
    return Fact(**data)


def _columns(fs: FactStore) -> set[str]:
    """fact_metric 当前列名集合（PRAGMA table_info）。"""
    return {
        row["name"]
        for row in fs.execute_read("PRAGMA table_info(fact_metric)")
    }


# ===========================================================================
# T1 — schema：建表后含 ingested_at 与 valid_as_of
# ===========================================================================
def test_t1_schema_has_temporal_columns(tmp_db_path):
    """user story：作为接手方，我要 fact_metric 自带时效列（valid_as_of /
    ingested_at），这样数字通路才能回答「这个数截至哪天、哪版入库」。"""
    fs = _fresh_store(tmp_db_path)
    cols = _columns(fs)
    assert "valid_as_of" in cols
    assert "ingested_at" in cols
    fs.close()


# ===========================================================================
# T2 — 迁移幂等：对仅含旧列的既有库再 init_schema → 补列、不丢数据
# ===========================================================================
def test_t2_migration_adds_columns_to_legacy_db(tmp_db_path):
    """user story：作为接手方，旧库（无时效列、已有事实）再次 init_schema 时应
    平滑补列、绝不报错、旧数据一条不丢——沿用 v2 幂等 ALTER 迁移模式。"""
    import sqlite3

    # 手工建一张「仅含基础 + v2 列、无时效列」的旧表，并塞一条事实。
    conn = sqlite3.connect(str(tmp_db_path))
    conn.execute(
        """
        CREATE TABLE fact_metric (
            metric_code   TEXT NOT NULL,
            entity        TEXT NOT NULL,
            geography     TEXT NOT NULL,
            channel       TEXT NOT NULL,
            period_type   TEXT NOT NULL,
            period        TEXT NOT NULL,
            value         REAL NOT NULL,
            unit          TEXT NOT NULL,
            source_doc_id TEXT NOT NULL,
            source_locator TEXT NOT NULL,
            tags          TEXT NOT NULL DEFAULT '{}',
            source_file_hash TEXT,
            extractor_version TEXT,
            mapping_version INTEGER,
            confidence    REAL,
            review_status TEXT NOT NULL DEFAULT 'auto_approved'
        )
        """
    )
    conn.execute(
        "INSERT INTO fact_metric (metric_code, entity, geography, channel, "
        "period_type, period, value, unit, source_doc_id, source_locator) "
        "VALUES ('REVENUE','ACME_HK','HK','TOTAL','FY','2025',1702.0,'USD_M',"
        "'legacy.xlsx','legacy!B2')",
    )
    conn.commit()
    conn.close()

    # 再次以 FactStore 打开并迁移：补列、不报错、旧数据保留。
    fs = FactStore(tmp_db_path)
    fs.init_schema()  # 不应抛
    cols = _columns(fs)
    assert "valid_as_of" in cols
    assert "ingested_at" in cols
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2025")
    assert got.value == 1702.0
    assert got.source_doc_id == "legacy.xlsx"
    fs.close()


def test_t2_migration_is_idempotent_second_call(tmp_db_path):
    """user story：init_schema 可反复调用（同一进程 / 重启后）都安全——第二次再
    迁移不重复加列、不报错。"""
    fs = _fresh_store(tmp_db_path)
    fs.init_schema()  # 第二次：幂等，不应抛
    cols = _columns(fs)
    # 两列各只出现一次（PRAGMA 不会出现重复列名）。
    assert "valid_as_of" in cols
    assert "ingested_at" in cols
    fs.close()


# ===========================================================================
# T3 — ingested_at 盖戳：可控形参 + 默认非空时间戳
# ===========================================================================
def test_t3_ingested_at_explicit_round_trip(tmp_db_path):
    """user story：为可测，upsert_facts 接受 ingested_at 形参显式盖戳，读回一致
    （审计可复现）。"""
    fs = _fresh_store(tmp_db_path)
    stamp = "2026-06-13T00:00:00+00:00"
    fs.upsert_facts([_base_fact(source_doc_id="t3.xlsx")], ingested_at=stamp)
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2025")
    assert got.ingested_at == stamp
    fs.close()


def test_t3_ingested_at_defaults_to_nonempty_timestamp(tmp_db_path):
    """user story：不传 ingested_at 时由 store 自动盖 UTC ISO 时间戳——读回非空、
    形如 ISO（含 'T'），证明每条入库事实都有审计时点。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(source_doc_id="t3b.xlsx")])
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2025")
    assert got.ingested_at
    assert "T" in got.ingested_at
    # 形如 ISO 日期时间（YYYY-MM-DDThh:mm:ss...）。
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", got.ingested_at)
    fs.close()


def test_t3_ingested_at_store_overrides_fact_value(tmp_db_path):
    """user story：ingested_at 是 store 盖的审计戳，Fact 上即便带值也应被 store
    在 upsert 时覆盖（写入以 store 为准，调用方不能伪造入库时间）。"""
    fs = _fresh_store(tmp_db_path)
    stamp = "2026-06-13T12:34:56+00:00"
    # Fact 自带一个假戳，但 upsert 显式 ingested_at 应当胜出。
    fs.upsert_facts(
        [_base_fact(source_doc_id="t3c.xlsx", ingested_at="1999-01-01T00:00:00+00:00")],
        ingested_at=stamp,
    )
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2025")
    assert got.ingested_at == stamp
    fs.close()


# ===========================================================================
# T4 — valid_as_of round-trip
# ===========================================================================
def test_t4_valid_as_of_round_trip(tmp_db_path):
    """user story：作为接手方，我入库时提供事实的「截至日期」（如年报截至
    2025-12-31），它应原样落库并能读回，使答案可标注业务时效。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(source_doc_id="t4.xlsx", valid_as_of="2025-12-31")])
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2025")
    assert got.valid_as_of == "2025-12-31"
    fs.close()


def test_t4_valid_as_of_defaults_to_none(tmp_db_path):
    """user story（边界）：不提供 valid_as_of 时读回为 None，而非空串——既有
    fixture / golden 都无此字段，默认 None 保证答案与现状一致。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(source_doc_id="t4b.xlsx")])
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2025")
    assert got.valid_as_of is None
    fs.close()


# ===========================================================================
# T5 — 替换语义：同键 re-upsert（新 ingested_at）→ count 不增、取回新值/新戳
# ===========================================================================
def test_t5_reupsert_replaces_value_and_stamp_no_count_growth(tmp_db_path):
    """user story：同一指标×实体×期间×渠道重复入库（修订数 + 新入库时间）应替换
    而非新增——count 不增长，取回新值与新 ingested_at（provenance 以最新写为准）。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts(
        [_base_fact(source_doc_id="t5.xlsx", value=1702.0, valid_as_of="2025-06-30")],
        ingested_at="2026-06-01T00:00:00+00:00",
    )
    assert fs.count() == 1
    # 同键 re-upsert：新值 / 新生效日 / 新戳。
    fs.upsert_facts(
        [_base_fact(source_doc_id="t5.xlsx", value=1750.0, valid_as_of="2025-12-31")],
        ingested_at="2026-06-13T00:00:00+00:00",
    )
    assert fs.count() == 1  # 替换语义：不增
    [got] = fs.query("REVENUE", "ACME_HK", "FY", "2025")
    assert got.value == 1750.0
    assert got.valid_as_of == "2025-12-31"
    assert got.ingested_at == "2026-06-13T00:00:00+00:00"
    fs.close()


# ===========================================================================
# T6 — 答案打通：execute_query_metric / _structured_answer / MockProvider 标注
# ===========================================================================
def test_t6_execute_query_metric_found_includes_valid_as_of(tmp_db_path):
    """user story：作为答案侧，我从 execute_query_metric 的 found 结果就能拿到
    valid_as_of（消除「存而未用」），无需再回查 store。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(source_doc_id="t6.xlsx", valid_as_of="2025-12-31")])
    res = execute_query_metric(fs, "REVENUE", "ACME_HK", "FY2025")
    assert res["status"] == "found"
    assert res["valid_as_of"] == "2025-12-31"
    fs.close()


def test_t6_structured_answer_annotates_when_valid_as_of_present():
    """user story：当事实带 valid_as_of 时，结构化回答应追加「截至 2025-12-31」
    标注，让高管一眼知道数字的业务时点。"""
    tool_results = [
        {
            "status": "found",
            "value": 1702.0,
            "unit": "USD_M",
            "metric_code": "REVENUE",
            "entity": "ACME_HK",
            "geography": "HK",
            "channel": "TOTAL",
            "period_type": "FY",
            "period": "2025",
            "valid_as_of": "2025-12-31",
            "source": {
                "doc": "ACME_FY2025_Results.pptx",
                "locator": "slide=5,table=1,row=2,col=3",
            },
        }
    ]
    answer, sources = _structured_answer("香港 FY2025 REVENUE 为 1702 USD_M。", tool_results)
    assert "截至 2025-12-31" in answer
    assert "ACME_FY2025_Results.pptx" in answer  # 血缘仍在
    assert sources == [tool_results[0]["source"]]


def test_t6_structured_answer_byte_identical_when_no_valid_as_of():
    """user story（回归守护）：valid_as_of 为 None（既有 fixture / golden 即如此）时，
    结构化回答文案必须与未引入时效列前字节级一致——不得多出任何「截至」标注。"""
    base_result = {
        "status": "found",
        "value": 1702.0,
        "unit": "USD_M",
        "metric_code": "REVENUE",
        "entity": "ACME_HK",
        "geography": "HK",
        "channel": "TOTAL",
        "period_type": "FY",
        "period": "2025",
        "source": {
            "doc": "ACME_FY2025_Results.pptx",
            "locator": "slide=5,table=1,row=2,col=3",
        },
    }
    model_text = "香港 FY2025 REVENUE 为 1702 百万美元。"

    # 不带 valid_as_of 键（旧结果形态）。
    answer_legacy, _ = _structured_answer(model_text, [dict(base_result)])
    # 带 valid_as_of=None（新结果形态、但值为空）。
    result_with_none = dict(base_result)
    result_with_none["valid_as_of"] = None
    answer_none, _ = _structured_answer(model_text, [result_with_none])

    # 现状文案：确定性合成行（实体 期间 指标：值 单位 + 来源），无任何「截至」标注。
    # 注：found 路径已弃用模型散文（反幻觉确定性合成），model_text 仅证其被忽略。
    expected = (
        "ACME_HK FY2025 REVENUE：1702 USD_M"
        "（来源：ACME_FY2025_Results.pptx · slide=5,table=1,row=2,col=3）"
    )
    assert answer_legacy == expected
    assert answer_none == expected
    assert "截至" not in answer_legacy
    assert "截至" not in answer_none


def test_t6_mock_provider_final_answer_annotates_valid_as_of():
    """user story：MockProvider 的 found 文案在 valid_as_of 存在时同样追加
    「截至 2025-12-31」，保证 mock 端到端链路与真实链路口径一致。"""
    result = {
        "status": "found",
        "value": 1702.0,
        "unit": "USD_M",
        "metric_code": "REVENUE",
        "entity": "ACME_HK",
        "geography": "HK",
        "channel": "TOTAL",
        "period_type": "FY",
        "period": "2025",
        "valid_as_of": "2025-12-31",
        "source": {
            "doc": "ACME_FY2025_Results.pptx",
            "locator": "slide=5,table=1,row=2,col=3",
        },
    }
    resp = MockProvider._final_answer(result)
    assert "截至 2025-12-31" in resp.text


def test_t6_mock_provider_final_answer_byte_identical_when_no_valid_as_of():
    """user story（回归守护）：MockProvider found 文案在无 valid_as_of 时与引入
    时效列前字节级一致——不得多出「截至」标注。"""
    result = {
        "status": "found",
        "value": 1702.0,
        "unit": "USD_M",
        "metric_code": "REVENUE",
        "entity": "ACME_HK",
        "geography": "HK",
        "channel": "TOTAL",
        "period_type": "FY",
        "period": "2025",
        "source": {
            "doc": "ACME_FY2025_Results.pptx",
            "locator": "slide=5,table=1,row=2,col=3",
        },
    }
    expected = (
        "ACME_HK FY2025 REVENUE 为 1702 USD_M"
        "（来源：ACME_FY2025_Results.pptx · slide=5,table=1,row=2,col=3）"
    )
    resp = MockProvider._final_answer(result)
    assert resp.text == expected
    assert "截至" not in resp.text


# ===========================================================================
# T9 — 回归：既有无 valid_as_of 的 fact 全链路（query / 答案）不变
# ===========================================================================
def test_t9_end_to_end_answer_unchanged_without_valid_as_of(tmp_db_path):
    """user story（回归守护）：入库一条无 valid_as_of 的事实，端到端问答应与引入
    时效列前完全一致——既不报错、答案里也不出现「截至」标注。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact()])  # 无 valid_as_of
    result = answer_question(
        "香港去年REVENUE多少", fs, MockProvider(reference_date=REF), reference_date=REF
    )
    assert isinstance(result, AgentResult)
    assert "1702" in result.answer
    assert "ACME_FY2025_Results.pptx" in result.answer  # 血缘仍在
    assert "截至" not in result.answer  # 无时效标注
    assert result.tool_results[0]["status"] == "found"
    fs.close()


def test_t9_end_to_end_answer_includes_valid_as_of_when_present(tmp_db_path):
    """user story：入库一条带 valid_as_of 的事实，端到端问答应在回答里带上
    「截至 2025-12-31」，证明时效列已真正打通到最终答案（非仅落库）。"""
    fs = _fresh_store(tmp_db_path)
    fs.upsert_facts([_base_fact(valid_as_of="2025-12-31")])
    result = answer_question(
        "香港去年REVENUE多少", fs, MockProvider(reference_date=REF), reference_date=REF
    )
    assert "1702" in result.answer
    assert "截至 2025-12-31" in result.answer
    fs.close()
