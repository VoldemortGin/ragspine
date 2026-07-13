"""AgentResult.answer_plain：与 answer 同文但剥离内联来源后缀（产品层用 .sources 自渲染引用）。

不变量：answer 逐字节不变（与既有 found/multi/narrative 路径一致）；answer_plain 由同一
「body」构造得到（不经正则事后剥离），故与 answer 的正文部分严格一致，仅去掉「（来源：…）」
/「（资料来源：…）」后缀。not_found 防编造：answer_plain 与 answer 同为被守卫改写的拒答。
"""

import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion, Choice, ResponseMessage

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.agent.llm_provider import MockProvider
from ragspine.storage.fact_store import Fact, SqliteFactStore

REF = date(2026, 6, 12)

REVENUE_HK_FY2025 = Fact(
    metric_code="REVENUE", entity="ACME_HK", geography="HK", channel="TOTAL",
    period_type="FY", period="2025", value=1702.0, unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx", source_locator="slide=5,table=1,row=2,col=3",
)
REVENUE_CN_FY2025 = Fact(
    metric_code="REVENUE", entity="ACME_CN", geography="CN", channel="TOTAL",
    period_type="FY", period="2025", value=800.0, unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx", source_locator="slide=6,table=1,row=2,col=3",
)


@pytest.fixture
def store(tmp_db_path):
    fs = SqliteFactStore(tmp_db_path)
    fs.init_schema()
    fs.upsert_facts([REVENUE_HK_FY2025, REVENUE_CN_FY2025])
    yield fs
    fs.close()


class CleanProseProvider:
    """叙事 provider：返回不含任何来源标注的裸散文 → 触发编排层「资料来源」血缘兜底。"""

    def chat(self, messages, *, tools=None):
        msg = ResponseMessage(
            role="assistant", content="香港营收下降主因是 MCV 客群收缩与银保渠道调整。"
        )
        return ChatCompletion(
            choices=(Choice(index=0, message=msg, finish_reason="stop"),)
        )


class FakeRetriever:
    def retrieve(self, query, *, filters=None, top_k=50):
        return [{
            "text": "香港 REVENUE 下降主因是 MCV 客群收缩。",
            "doc_id": "HK_QBR_2025Q4.pptx",
            "locator": "slide=12",
        }]


# ---------------------------------------------------------------------------
# found（结构化单发）
# ---------------------------------------------------------------------------

def test_structured_found_answer_plain_strips_source_suffix(store):
    result = answer_question(
        "香港去年REVENUE多少", store, MockProvider(reference_date=REF), reference_date=REF
    )
    # answer 逐字节不变：内联来源后缀仍在
    assert result.answer == (
        "ACME_HK FY2025 REVENUE：1702 USD_M"
        "（来源：ACME_FY2025_Results.pptx · slide=5,table=1,row=2,col=3）"
    )
    # answer_plain 同一正文，但无任何内联来源后缀
    assert result.answer_plain == "ACME_HK FY2025 REVENUE：1702 USD_M"
    assert "1702" in result.answer_plain
    assert "（来源：" not in result.answer_plain
    assert "（资料来源：" not in result.answer_plain
    # sources 仍完整
    assert result.sources == [
        {"doc": "ACME_FY2025_Results.pptx", "locator": "slide=5,table=1,row=2,col=3"}
    ]


# ---------------------------------------------------------------------------
# multi-subtask 对比
# ---------------------------------------------------------------------------

def test_multi_subtask_answer_plain_strips_source_suffix(store):
    result = answer_question(
        "香港和中国去年REVENUE各是多少", store, MockProvider(reference_date=REF),
        reference_date=REF,
    )
    assert "（来源：" in result.answer  # answer 仍内联来源
    assert result.answer_plain == (
        "对比结果：\n"
        "- ACME_HK FY2025 REVENUE：1702 USD_M\n"
        "- ACME_CN FY2025 REVENUE：800 USD_M"
    )
    assert "（来源：" not in result.answer_plain
    assert "（资料来源：" not in result.answer_plain
    assert [s["doc"] for s in result.sources] == [
        "ACME_FY2025_Results.pptx", "ACME_FY2025_Results.pptx"
    ]


# ---------------------------------------------------------------------------
# narrative
# ---------------------------------------------------------------------------

def test_narrative_answer_plain_strips_lineage_fallback(store):
    result = answer_question(
        "香港最近有什么监管动态", store, CleanProseProvider(),
        reference_date=REF, narrative_retriever=FakeRetriever(),
    )
    assert result.route == "narrative"
    # answer 带血缘兜底
    assert "（资料来源：" in result.answer
    assert "HK_QBR_2025Q4.pptx" in result.answer
    # answer_plain 是裸散文：无来源标注
    assert result.answer_plain == "香港营收下降主因是 MCV 客群收缩与银保渠道调整。"
    assert "（来源：" not in result.answer_plain
    assert "（资料来源：" not in result.answer_plain
    # sources 仍完整
    assert result.sources == [{"doc": "HK_QBR_2025Q4.pptx", "locator": "slide=12"}]


# ---------------------------------------------------------------------------
# not_found（反编造）：answer_plain == answer，均为被守卫的拒答
# ---------------------------------------------------------------------------

def test_not_found_answer_plain_equals_guarded_refusal(store):
    result = answer_question(
        "中国去年ROE多少", store, MockProvider(reference_date=REF), reference_date=REF
    )
    assert result.tool_results[0]["status"] == "not_found"
    assert result.answer_plain == result.answer
    assert "查不到" in result.answer_plain
    # 无任何编造数字泄漏
    for number in ("9999", "888"):
        assert number not in result.answer_plain


# ---------------------------------------------------------------------------
# backward compat：新字段可省略，默认空串
# ---------------------------------------------------------------------------

def test_agent_result_answer_plain_defaults_to_empty():
    result = AgentResult(answer="hi", route="structured")
    assert result.answer_plain == ""
