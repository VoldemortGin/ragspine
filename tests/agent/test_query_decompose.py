"""W6a LLM 查询分解（opt-in，默认关）测试。

分解经注入的 decomposer 才生效，默认 None＝行为字节不变（确定性笛卡尔不受影响）。
子答案仍走既有结构化/叙事通路 + guard：分解只影响"问什么"，绝不绕过 found/not-found 改写，
也绝不绕过安全门（每个子问题独立复核越权/竞品）。
"""

import json
import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import (
    ChatCompletion,
    Choice,
    ProviderError,
    ResponseMessage,
)

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.agent.decompose import (
    ROUTE_DECOMPOSED,
    LLMQueryDecomposer,
    make_decomposer,
)
from ragspine.agent.llm_provider import MockProvider
from ragspine.storage.fact_store import Fact, SqliteFactStore

REF = date(2026, 6, 12)

REVENUE_HK_FY2025 = Fact(
    metric_code="REVENUE", entity="ACME_HK", geography="HK", channel="TOTAL",
    period_type="FY", period="2025", value=1702.0, unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx", source_locator="slide=5,table=1,row=2,col=3",
)


@pytest.fixture
def store(tmp_db_path):
    fs = SqliteFactStore(tmp_db_path)
    fs.init_schema()
    fs.upsert_facts([REVENUE_HK_FY2025])
    yield fs
    fs.close()


class FakeDecomposer:
    """duck-typed QueryDecomposer：返回固定子问题表（记录被分解的原问句）。"""

    def __init__(self, subquestions: list[str]):
        self.subquestions = subquestions
        self.seen: list[str] = []

    def decompose(self, question: str, *, reference_date: date | None = None) -> list[str]:
        self.seen.append(question)
        return list(self.subquestions)


class ScriptedProvider:
    """按脚本依次吐响应的测试 provider。"""

    def __init__(self, responses: list[ChatCompletion]):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        return self._responses.pop(0)


class BoomProvider:
    """chat 一律抛 ProviderError（网络/API 故障模拟）。"""

    def chat(self, messages, *, tools=None):
        raise ProviderError("boom")


def _text_response(text: str) -> ChatCompletion:
    msg = ResponseMessage(role="assistant", content=text)
    return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="stop"),))


# ---------------------------------------------------------------------------
# 默认行为字节不变（不注入 decomposer / decomposer=None）
# ---------------------------------------------------------------------------

def test_default_no_decomposer_is_byte_identical(store):
    """不传 decomposer 与显式 decomposer=None 必须逐字段等价（默认 loop 字节不变）。"""
    q = "香港去年REVENUE多少"
    a = answer_question(q, store, MockProvider(reference_date=REF), reference_date=REF)
    b = answer_question(
        q, store, MockProvider(reference_date=REF), reference_date=REF, decomposer=None
    )
    assert (a.answer, a.route, a.sources, a.tool_results) == (
        b.answer, b.route, b.sources, b.tool_results
    )
    assert a.route == "structured"


def test_single_subquestion_falls_through_to_normal_route(store):
    """分解器只返回一个子问题（不可分解）→ 走正常路由，不是 decomposed。"""
    decomposer = FakeDecomposer(["香港去年REVENUE多少"])
    result = answer_question(
        "香港去年REVENUE多少", store, MockProvider(reference_date=REF),
        reference_date=REF, decomposer=decomposer,
    )
    assert result.route == "structured"
    assert "1702" in result.answer


# ---------------------------------------------------------------------------
# 分解 fan-out：多子问题分别回答再确定性合成
# ---------------------------------------------------------------------------

def test_decompose_fans_out_and_aggregates(store):
    """注入分解器把问题拆成 2 个子问题 → 各自走结构化通路、确定性合成、来源聚合。"""
    decomposer = FakeDecomposer(["香港FY2025 REVENUE多少", "香港FY2025 PROFIT多少"])
    result = answer_question(
        "香港FY2025经营怎么样", store, MockProvider(reference_date=REF),
        reference_date=REF, decomposer=decomposer,
    )
    assert isinstance(result, AgentResult)
    assert result.route == ROUTE_DECOMPOSED
    # 子问题 1 命中
    assert "1702" in result.answer
    assert "ACME_FY2025_Results.pptx" in result.answer
    # 子问题 2 查不到（确定性"查不到"改写，绝不编造）
    assert "查不到" in result.answer
    # 来源聚合（命中子项的血缘进 sources）
    assert {"doc": "ACME_FY2025_Results.pptx",
            "locator": "slide=5,table=1,row=2,col=3"} in result.sources
    # 原问句被分解器消费
    assert decomposer.seen == ["香港FY2025经营怎么样"]


def test_decompose_subanswers_keep_anti_fabrication_guard(store):
    """子答案仍走防编造 guard：not_found 子问题绝不出现编造数字。"""
    decomposer = FakeDecomposer(["香港FY2025 REVENUE多少", "中国FY2024 ROE多少"])
    result = answer_question(
        "整体经营如何", store, MockProvider(reference_date=REF),
        reference_date=REF, decomposer=decomposer,
    )
    assert result.route == ROUTE_DECOMPOSED
    assert "1702" in result.answer        # found 子项
    assert "查不到" in result.answer       # not_found 子项被确定性改写


# ---------------------------------------------------------------------------
# 隔离继承：分解产出的竞品子问题仍被安全门拒答（不绕过越权拒答）
# ---------------------------------------------------------------------------

def test_decompose_competitor_subquestion_is_refused(store):
    """分解器若产出竞品子问题，该子问题必须被安全门越权拒答，绝不用 home 数字作答。"""
    decomposer = FakeDecomposer(["香港FY2025 REVENUE多少", "竞安FY2025 REVENUE多少"])
    result = answer_question(
        "对比一下", store, MockProvider(reference_date=REF),
        reference_date=REF, decomposer=decomposer,
    )
    assert result.route == ROUTE_DECOMPOSED
    # home 子问题正常命中
    assert "1702" in result.answer
    # 竞品子问题被拒答（系统不掌握外部主体数据），且绝不把 home 数字当竞品答案
    assert "竞安" in result.answer
    assert "不掌握" in result.answer or "无法回答" in result.answer


# ---------------------------------------------------------------------------
# LLMQueryDecomposer：解析 + 有界 + 确定性降级
# ---------------------------------------------------------------------------

def test_llm_decomposer_parses_json_array():
    provider = ScriptedProvider([
        _text_response('["哪个区域增长最快", "增长最快的区域为什么增长"]'),
    ])
    dec = LLMQueryDecomposer(provider)
    subs = dec.decompose("哪个区域增长最快、为什么", reference_date=REF)
    assert subs == ["哪个区域增长最快", "增长最快的区域为什么增长"]


def test_llm_decomposer_degrades_on_provider_error():
    """provider 故障 → 确定性降级为原问句单元素表（不崩、不分解）。"""
    dec = LLMQueryDecomposer(BoomProvider())
    assert dec.decompose("原问句", reference_date=REF) == ["原问句"]


def test_llm_decomposer_degrades_on_bad_json():
    dec = LLMQueryDecomposer(ScriptedProvider([_text_response("这不是 JSON 数组")]))
    assert dec.decompose("原问句", reference_date=REF) == ["原问句"]


def test_llm_decomposer_is_bounded():
    """子问题数量有界（防发散）：超出上限时截断。"""
    many = json.dumps([f"子问题{i}" for i in range(10)], ensure_ascii=False)
    dec = LLMQueryDecomposer(ScriptedProvider([_text_response(many)]), max_subquestions=4)
    subs = dec.decompose("复杂问题", reference_date=REF)
    assert len(subs) == 4


def test_llm_decomposer_single_item_passthrough():
    """模型认为不可分解（返回单元素或空）→ 回退原问句，不触发 fan-out。"""
    dec = LLMQueryDecomposer(ScriptedProvider([_text_response('["只有一个"]')]))
    assert dec.decompose("简单问题", reference_date=REF) == ["只有一个"]


# ---------------------------------------------------------------------------
# make_decomposer 工厂 + env 选型
# ---------------------------------------------------------------------------

def test_make_decomposer_none_default():
    assert make_decomposer(None) is None
    assert make_decomposer("none") is None


def test_make_decomposer_llm_needs_provider():
    """'llm' 但未注入 provider → None（注入 provider 才生效，诚实降级为不分解）。"""
    assert make_decomposer("llm", provider=None) is None
    dec = make_decomposer("llm", provider=MockProvider())
    assert isinstance(dec, LLMQueryDecomposer)


def test_make_decomposer_env(monkeypatch):
    monkeypatch.setenv("RAGSPINE_QUERY_DECOMPOSE", "llm")
    dec = make_decomposer(provider=MockProvider())
    assert isinstance(dec, LLMQueryDecomposer)
    monkeypatch.setenv("RAGSPINE_QUERY_DECOMPOSE", "none")
    assert make_decomposer(provider=MockProvider()) is None


def test_make_decomposer_unknown_spec_raises():
    with pytest.raises(ValueError):
        make_decomposer("nope", provider=MockProvider())
