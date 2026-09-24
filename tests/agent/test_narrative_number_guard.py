"""叙事数字防编造（ADR 0024）：叙事答案里的每个数字都必须能在检索片段里原样找到。

- 算出来的数（片段里没有的差值、换算、推测）→ 确定性改写为“资料中没有直接给出该数值”
  + 片段原值句子，不再调 LLM；来源照常强制附上。
- 放行：片段原值（含格式变体）、问句里的数字、[n] 引用标号、列表序号 / 页码引用、
  与问句或片段一致的年份 / 期间标记。
- 开关 RAGSPINE_NARRATIVE_NUMBER_GUARD=on|off；off 时 prompt 与答案逐字节不变（快照）。
"""

import logging
import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion, Choice, ResponseMessage

from ragspine.agent.agent import NARRATIVE_FALLBACK_ENV, answer_question
from ragspine.agent.llm_provider import MockProvider
from ragspine.agent.number_guard import (
    NARRATIVE_NUMBER_GUARD_ENV,
    NUMBER_GUARD_NOTICE,
    NUMBER_GUARD_RULE,
    guard_narrative_answer,
    ungrounded_numbers,
)
from ragspine.common.company_profile import load_company_profile
from ragspine.eval.nl_gold_ragspine import ForcedNarrativeIntentParser, is_refusal
from ragspine.storage.fact_store import SqliteFactStore

REF = date(2026, 9, 24)
DOC = "deck.md"
MIX_SNIPPET = {
    "text": "Attractive New Business Profile Distribution Mix Agency 72% VONB 28% Partnerships VONB 1H26",
    "doc_id": DOC,
    "locator": f"{DOC}@page=18#para1-19",
}
VONB_SNIPPET = {
    "text": "1H26 VONB was US$514m, up 15 per cent; ANP US$4,712 million.",
    "doc_id": DOC,
    "locator": f"{DOC}@page=10#para1-5",
}
PATHWAY_SNIPPET = {
    "text": "Foundation 100% Digitalised Agency #1 MDRT Growth Data-Driven Lead Generation",
    "doc_id": DOC,
    "locator": f"{DOC}@page=6#para1-23",
}

A02_QUESTION = (
    "In the 1H26 Distribution Mix, how many percentage points higher is the Agency share of "
    "VONB than the Partnerships share?"
)
A02_ANSWER = (
    "**答案：高 44 个百分点。**\n\n"
    "根据 1H26 的分销渠道结构，Agency 占 VONB 的 72%，Partnerships 占 28%。"
    "两者相差 72% − 28% = **44 个百分点**。\n\n"
    f"来源：\n- [1] Distribution Mix：“Agency 72% VONB 28% Partnerships VONB … 1H26”（{DOC}@page=18）"
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(NARRATIVE_NUMBER_GUARD_ENV, raising=False)
    monkeypatch.delenv(NARRATIVE_FALLBACK_ENV, raising=False)


@pytest.fixture
def store(tmp_db_path):
    fs = SqliteFactStore(tmp_db_path)
    fs.init_schema()
    yield fs
    fs.close()


def _text(content: str) -> ChatCompletion:
    msg = ResponseMessage(role="assistant", content=content)
    return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="stop"),))


class ScriptedProvider:
    def __init__(self, *answers: str):
        self._answers = list(answers)
        self.messages: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.messages.append(messages)
        return _text(self._answers.pop(0))


class FakeRetriever:
    def __init__(self, *snippets: dict):
        self.snippets = snippets

    def retrieve(self, query, *, filters=None, top_k=50):
        return [dict(s) for s in self.snippets]


def _ask(question, answer, snippets, store, *, guard=None):
    provider = ScriptedProvider(answer)
    result = answer_question(
        question,
        store,
        provider,
        reference_date=REF,
        narrative_retriever=FakeRetriever(*snippets),
        intent_parser=ForcedNarrativeIntentParser(),
        narrative_number_guard=guard,
    )
    return result, provider


# ---------------------------------------------------------------------------
# 纯函数：哪些数字算“无依据”
# ---------------------------------------------------------------------------


def _ungrounded(answer: str, question: str = "", snippets=(MIX_SNIPPET,)) -> list[str]:
    return ungrounded_numbers(
        answer,
        question=question,
        evidence=[s["text"] for s in snippets],
        source_refs=[r for s in snippets for r in (s["doc_id"], s["locator"])],
    )


def test_computed_difference_is_ungrounded():
    assert _ungrounded("Agency 比 Partnerships 高 44 个百分点。", A02_QUESTION) == ["44"]


def test_snippet_raw_values_are_grounded():
    assert _ungrounded("Agency 占 VONB 的 72%，Partnerships 占 28%。", A02_QUESTION) == []


@pytest.mark.parametrize(
    "answer",
    [
        "VONB 为 US$514 million。",
        "VONB 为 514 百万美元。",
        "VONB 为 5.14 亿美元。",
        "VONB 为 US$0.514 billion。",
        "VONB 增长 15%。",
        "VONB grew 15 percent.",
        "ANP 为 4712 百万美元。",
        "ANP 为 US$4,712m。",
    ],
)
def test_format_variants_are_grounded(answer):
    assert _ungrounded(answer, snippets=(VONB_SNIPPET,)) == []


def test_scaled_amount_matches_table_figure_in_millions():
    """表格以 US$m 计（裸数 1,168）：“11.68 亿美元” / “US$1.168 billion” 与之等价。"""
    table = {
        "text": "<tr><td>Hong Kong</td><td>1,168</td><td>1,062</td></tr>",
        "doc_id": DOC,
        "locator": "t",
    }
    assert _ungrounded("香港：11.68 亿美元；上年 10.62 亿美元。", snippets=(table,)) == []
    assert _ungrounded("Hong Kong: US$1.168 billion.", snippets=(table,)) == []
    assert _ungrounded("香港：11.69 亿美元。", snippets=(table,)) == ["11.69", "1169"]


@pytest.mark.parametrize(
    "answer",
    ["VONB 为 5.15 亿美元。", "VONB 增长 16%。", "VONB 增长 15 个百分点以上，达 600m。"],
)
def test_near_miss_values_are_ungrounded(answer):
    assert _ungrounded(answer, snippets=(VONB_SNIPPET,))


def test_percent_is_not_a_bare_number():
    """片段里的 44% 不能为 “44 个百分点” 作证（单位不同）。"""
    snippet = {"text": "Chinese Mainland 48% 44% A+", "doc_id": DOC, "locator": "x"}
    assert _ungrounded("高 44 个百分点。", snippets=(snippet,)) == ["44"]


def test_question_numbers_are_exempt():
    question = "如果目标是 3000 百万美元，1H26 VONB 距离目标还有多少？"
    assert _ungrounded("目标是 3000 百万美元，VONB 为 514m。", question, (VONB_SNIPPET,)) == []


@pytest.mark.parametrize(
    "answer",
    [
        "Agency 占 72% [1]。",
        "Agency 占 72% [7][12]。",
        "Agency 占 72%。〔1〕〔10〕【5】［6］",
        "Agency 占 72%（见 [2, 3]）。",
        f"Agency 占 72%（来源：{DOC}@page=18#para1-19）。",
        "Agency 占 72%（slide 18，page 6，第 18 页，图：p18.png）。",
        "阶段如下：\n1. Foundation\n2. Growth\n3. Intelligence",
        "（1）Agency 72%；（2）Partnerships 28%。",
        "Agency 占 72%（来源：[1] page=27#para13；[6] page=2#para8；@page=10）。",
    ],
)
def test_citation_markers_list_numbers_and_page_refs_are_exempt(answer):
    assert _ungrounded(answer, A02_QUESTION) == []


@pytest.mark.parametrize(
    "answer",
    [
        "1H26 Agency 占 72%。",
        "2026 年上半年 Agency 占 72%。",
        "In 1H 2026 Agency was 72%.",
        "FY26 上半年（1H26）Agency 占 72%。",
    ],
)
def test_period_markers_consistent_with_evidence_are_exempt(answer):
    assert _ungrounded(answer, "Distribution Mix 里 Agency 占多少？") == []


def test_period_marker_not_in_question_or_evidence_is_ungrounded():
    assert _ungrounded("2H25 Agency 占 72%。", "Agency 占多少？") == ["2h25"]


# ---------------------------------------------------------------------------
# 确定性改写
# ---------------------------------------------------------------------------


def test_rewrite_is_deterministic_and_keeps_grounded_raw_values():
    evidence = [MIX_SNIPPET["text"]]
    refs = [DOC, MIX_SNIPPET["locator"]]
    first = guard_narrative_answer(A02_ANSWER, A02_QUESTION, evidence, refs)
    second = guard_narrative_answer(A02_ANSWER, A02_QUESTION, evidence, refs)
    assert first == second
    rewritten, n_ungrounded = first
    assert n_ungrounded == 1
    assert rewritten.startswith(NUMBER_GUARD_NOTICE)
    assert "44" not in rewritten
    assert "72%" in rewritten and "28%" in rewritten


def test_rewrite_without_grounded_values_is_notice_only():
    rewritten, n = guard_narrative_answer(
        "差值为 44 个百分点。", A02_QUESTION, [MIX_SNIPPET["text"]], []
    )
    assert (rewritten, n) == (NUMBER_GUARD_NOTICE, 1)


def test_short_heading_joins_lead():
    """首句过短（“**答案：**”）时开头并入下一句：下一句里算出的数仍算开头 → 整体改写。"""
    answer = "**答案：**\n差值为 44 个百分点，Agency 72%。"
    rewritten, _ = guard_narrative_answer(answer, A02_QUESTION, [MIX_SNIPPET["text"]], [])
    assert rewritten.startswith(NUMBER_GUARD_NOTICE)


def test_incidental_computed_number_is_dropped_but_answer_kept():
    """开头有依据、后文顺带算了个数：只删那一句，其余原样保留，末尾注明。"""
    answer = (
        "**1H26 Agency 占 VONB 的 72%。**\n\n"
        "- Partnerships 占 28%。两者合计 100%。\n"
        "- 该结构体现代理人渠道的优势。"
    )
    rewritten, n = guard_narrative_answer(answer, A02_QUESTION, [MIX_SNIPPET["text"]], [])
    assert n == 1
    assert rewritten == (
        "**1H26 Agency 占 VONB 的 72%。**\n\n"
        "- Partnerships 占 28%。\n"
        "- 该结构体现代理人渠道的优势。\n"
        "（注：已移除 1 处在检索片段中找不到原文的数字，不做推算。）"
    )
    assert not is_refusal(rewritten)


def test_grounded_answer_is_returned_unchanged():
    answer = "Agency 占 VONB 的 72% [1]。"
    assert guard_narrative_answer(answer, A02_QUESTION, [MIX_SNIPPET["text"]], []) == (answer, 0)


# ---------------------------------------------------------------------------
# 编排层端到端
# ---------------------------------------------------------------------------


def test_orchestrator_blocks_computed_difference(store):
    result, _ = _ask(A02_QUESTION, A02_ANSWER, [MIX_SNIPPET], store, guard=True)
    assert "44" not in result.answer and "44" not in result.answer_plain
    assert result.answer_plain.startswith(NUMBER_GUARD_NOTICE)
    assert "72%" in result.answer_plain and "28%" in result.answer_plain
    # nl-gold 判分器按拒答识别（开头即“资料中没有…”）。
    assert is_refusal(result.answer_plain)


def test_rewritten_answer_still_carries_sources(store):
    answer = "差值为 44 个百分点。"
    result, _ = _ask(A02_QUESTION, answer, [MIX_SNIPPET], store, guard=True)
    assert result.sources == [{"doc": DOC, "locator": MIX_SNIPPET["locator"]}]
    assert result.answer == f"{NUMBER_GUARD_NOTICE}\n（资料来源：{DOC} {MIX_SNIPPET['locator']}）"


def test_orchestrator_passes_grounded_answer_unchanged(store):
    answer = f"Agency 占 VONB 的 72%，Partnerships 占 28% [1]（{DOC}）。"
    result, _ = _ask(A02_QUESTION, answer, [MIX_SNIPPET], store, guard=True)
    assert result.answer == answer
    assert result.answer_plain == answer


def test_fallback_route_answer_is_guarded_too(store):
    """A 路：结构化缺指标 → 回落叙事（ADR 0023 有依据：含片段数字 72）→ 仍要拦下算出的 44。"""
    provider = ScriptedProvider(A02_ANSWER)
    result = answer_question(
        A02_QUESTION,
        store,
        provider,
        reference_date=REF,
        narrative_retriever=FakeRetriever(MIX_SNIPPET),
        narrative_number_guard=True,
    )
    assert result.fallback is not None
    assert "44" not in result.answer_plain
    assert result.answer_plain.startswith(NUMBER_GUARD_NOTICE)


def test_system_prompt_carries_inference_rule_only_when_on(store):
    _, on = _ask(A02_QUESTION, "Agency 占 72%。", [MIX_SNIPPET], store, guard=True)
    _, off = _ask(A02_QUESTION, "Agency 占 72%。", [MIX_SNIPPET], store, guard=False)
    assert on.messages[0][0]["content"].endswith(NUMBER_GUARD_RULE)
    assert NUMBER_GUARD_RULE not in off.messages[0][0]["content"]


def test_off_is_byte_identical_snapshot(store):
    """off：system prompt 与答案逐字节等于开关引入前（快照），算出的 44 原样保留。"""
    company = load_company_profile().home_company_name
    result, provider = _ask(A02_QUESTION, A02_ANSWER, [MIX_SNIPPET], store, guard=False)
    system = provider.messages[0][0]
    assert system == {
        "role": "system",
        "content": f"你是 {company} 管理层经营洞察助手，只依据给定片段作答并标注来源。",
    }
    assert result.answer_plain == A02_ANSWER
    # 答案已含来源文件名 → 不追加血缘后缀（开关引入前的行为）。
    assert result.answer == A02_ANSWER


def test_env_off_is_byte_identical_to_explicit_off(store, monkeypatch):
    explicit, p1 = _ask(A02_QUESTION, A02_ANSWER, [MIX_SNIPPET], store, guard=False)
    monkeypatch.setenv(NARRATIVE_NUMBER_GUARD_ENV, " OFF ")
    via_env, p2 = _ask(A02_QUESTION, A02_ANSWER, [MIX_SNIPPET], store)
    assert via_env == explicit
    assert p1.messages == p2.messages


def test_env_on_and_default(store, monkeypatch):
    default, _ = _ask(A02_QUESTION, A02_ANSWER, [MIX_SNIPPET], store)
    monkeypatch.setenv(NARRATIVE_NUMBER_GUARD_ENV, "on")
    via_env, _ = _ask(A02_QUESTION, A02_ANSWER, [MIX_SNIPPET], store)
    assert via_env.answer_plain.startswith(NUMBER_GUARD_NOTICE)
    assert default == via_env


def test_env_invalid_value_raises(store, monkeypatch):
    monkeypatch.setenv(NARRATIVE_NUMBER_GUARD_ENV, "maybe")
    with pytest.raises(ValueError, match=NARRATIVE_NUMBER_GUARD_ENV):
        _ask(A02_QUESTION, A02_ANSWER, [MIX_SNIPPET], store)


def test_trace_records_counts_only_on_rewrite(store, caplog):
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        _ask(A02_QUESTION, A02_ANSWER, [MIX_SNIPPET], store, guard=True)
    traces = [r for r in caplog.records if hasattr(r, "narrative_number_guard")]
    assert len(traces) == 1
    assert traces[0].narrative_number_guard == {"ungrounded": 1, "rewritten": 1}

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        _ask(A02_QUESTION, "Agency 占 72%。", [MIX_SNIPPET], store, guard=True)
    assert not any(hasattr(r, "narrative_number_guard") for r in caplog.records)


def test_mock_provider_echo_is_not_rewritten(store):
    """MockProvider 回显问句 + 片段 + 来源定位：全部有依据，离线 demo / QA 棘轮不受影响。"""
    snippet = {
        "text": "香港 1H26 REVENUE 下降主因是 MCV 客群收缩。",
        "doc_id": "HK_QBR.pptx",
        "locator": "slide=12,para=3",
    }
    result = answer_question(
        "香港 2026 上半年 REVENUE 为什么下降？",
        store,
        MockProvider(reference_date=REF),
        reference_date=REF,
        narrative_retriever=FakeRetriever(snippet),
        intent_parser=ForcedNarrativeIntentParser(),
        narrative_number_guard=True,
    )
    assert NUMBER_GUARD_NOTICE not in result.answer
    assert "MCV 客群收缩" in result.answer
