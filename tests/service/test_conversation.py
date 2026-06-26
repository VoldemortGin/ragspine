"""W6c 多轮会话记忆（opt-in 骨架）测试。

核心安全性质：每轮都重新过 answer_question 的安全门——记忆只承载上一轮解析出的 home 受控
槽位（entity 代码 + period），且只在跟进问句缺槽位、且本轮非越权时确定性回填；竞品跟进照样被
越权拒答，home 上下文绝不被带进越权问句、绝不泄露 home 数字。
"""

import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.intent import parse_intent
from ragspine.agent.llm_provider import MockProvider
from ragspine.service.conversation import (
    ConversationMemory,
    ConversationSession,
    ConversationTurn,
    resolve_followup,
)
from ragspine.storage.fact_store import Fact, FactStore

REF = date(2026, 6, 12)

REVENUE_HK_FY2025 = Fact(
    metric_code="REVENUE", entity="ACME_HK", geography="HK", channel="TOTAL",
    period_type="FY", period="2025", value=1702.0, unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx", source_locator="slide=5,table=1,row=2,col=3",
)
PROFIT_HK_FY2025 = Fact(
    metric_code="PROFIT", entity="ACME_HK", geography="HK", channel="TOTAL",
    period_type="FY", period="2025", value=311.0, unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx", source_locator="slide=6,table=1,row=2,col=3",
)


@pytest.fixture
def store(tmp_db_path):
    fs = FactStore(tmp_db_path)
    fs.init_schema()
    fs.upsert_facts([REVENUE_HK_FY2025, PROFIT_HK_FY2025])
    yield fs
    fs.close()


# ---------------------------------------------------------------------------
# ConversationMemory：有界
# ---------------------------------------------------------------------------

def test_memory_is_bounded():
    mem = ConversationMemory(maxlen=2)
    mem.remember(ConversationTurn(question="q1", route="structured", entity="ACME_HK", period=("FY", "2023")))
    mem.remember(ConversationTurn(question="q2", route="structured", entity="ACME_CN", period=("FY", "2024")))
    mem.remember(ConversationTurn(question="q3", route="structured", entity="ACME_GROUP", period=("FY", "2025")))
    assert len(mem) == 2
    assert mem.last_entity() == "ACME_GROUP"
    assert mem.last_period() == ("FY", "2025")


def test_memory_last_scans_back_for_non_null():
    """最近一轮若无 entity（如纯叙事），last_entity 回扫到上一条有值的轮。"""
    mem = ConversationMemory()
    mem.remember(ConversationTurn(question="q1", route="structured", entity="ACME_HK", period=("FY", "2025")))
    mem.remember(ConversationTurn(question="q2", route="narrative", entity=None, period=None))
    assert mem.last_entity() == "ACME_HK"
    assert mem.last_period() == ("FY", "2025")


# ---------------------------------------------------------------------------
# resolve_followup：确定性回填
# ---------------------------------------------------------------------------

def test_followup_carries_entity_and_period():
    """跟进问句缺 entity/period → 回填上一轮 home 槽位，重解析得回同一受控代码。"""
    mem = ConversationMemory()
    mem.remember(ConversationTurn(question="香港FY2025 REVENUE多少", route="structured",
                                  entity="ACME_HK", period=("FY", "2025")))
    augmented = resolve_followup(mem, "PROFIT多少", reference_date=REF)
    assert "接续上文" in augmented
    intent = parse_intent(augmented, reference_date=REF)
    assert intent.metric == "PROFIT"
    assert intent.entity == "ACME_HK"
    assert intent.period == ("FY", "2025")


def test_followup_no_carry_when_slots_present():
    """跟进问句已自带 entity+period → 不回填（无"接续上文"）。"""
    mem = ConversationMemory()
    mem.remember(ConversationTurn(question="香港FY2025 REVENUE多少", route="structured",
                                  entity="ACME_HK", period=("FY", "2025")))
    augmented = resolve_followup(mem, "中国FY2024 PROFIT多少", reference_date=REF)
    assert augmented == "中国FY2024 PROFIT多少"


def test_followup_no_carry_for_pure_narrative():
    """纯叙事问句（无指标/数字意图）不回填——回填只服务结构化/复合跟进。"""
    mem = ConversationMemory()
    mem.remember(ConversationTurn(question="香港FY2025 REVENUE多少", route="structured",
                                  entity="ACME_HK", period=("FY", "2025")))
    augmented = resolve_followup(mem, "最近有什么监管动态", reference_date=REF)
    assert augmented == "最近有什么监管动态"


def test_followup_no_carry_into_competitor_question():
    """跟进问句命中竞品 → 绝不回填 home 上下文（安全门会越权拒答，记忆不得污染）。"""
    mem = ConversationMemory()
    mem.remember(ConversationTurn(question="香港FY2025 REVENUE多少", route="structured",
                                  entity="ACME_HK", period=("FY", "2025")))
    augmented = resolve_followup(mem, "竞安REVENUE多少", reference_date=REF)
    assert augmented == "竞安REVENUE多少"
    assert "接续上文" not in augmented


def test_followup_empty_memory_passthrough():
    mem = ConversationMemory()
    assert resolve_followup(mem, "PROFIT多少", reference_date=REF) == "PROFIT多少"


# ---------------------------------------------------------------------------
# ConversationSession：多轮端到端 + 每轮过安全门
# ---------------------------------------------------------------------------

def test_session_multi_turn_carries_context(store):
    session = ConversationSession(store, MockProvider(reference_date=REF), reference_date=REF)
    r1 = session.ask("香港FY2025 REVENUE多少")
    assert "1702" in r1.answer
    # 第二轮只说指标，靠记忆补 entity + period
    r2 = session.ask("PROFIT多少")
    assert r2.tool_results[0]["status"] == "found"
    assert "311" in r2.answer
    assert "ACME_FY2025_Results.pptx" in r2.answer


def test_session_competitor_followup_refused_no_leak(store):
    """竞品跟进：每轮重过安全门 → 越权拒答；绝不泄露上一轮 home 数字。"""
    session = ConversationSession(store, MockProvider(reference_date=REF), reference_date=REF)
    r1 = session.ask("香港FY2025 REVENUE多少")
    assert "1702" in r1.answer
    r2 = session.ask("竞安FY2025 REVENUE多少")
    assert "竞安" in r2.answer
    assert "不掌握" in r2.answer or "无法回答" in r2.answer
    assert "1702" not in r2.answer  # home 数字绝不泄露给竞品问句


def test_session_does_not_remember_refused_turn(store):
    """被拒答/反问的轮不污染记忆（不存竞品，也不把 home 槽位错记到竞品轮）。"""
    session = ConversationSession(store, MockProvider(reference_date=REF), reference_date=REF)
    session.ask("香港FY2025 REVENUE多少")
    session.ask("竞安FY2025 REVENUE多少")  # 被拒答
    # 记忆里仍是上一轮 home 槽位，竞品轮没有写入任何 entity
    assert session.memory.last_entity() == "ACME_HK"
