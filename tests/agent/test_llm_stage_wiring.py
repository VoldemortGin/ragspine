"""LLM 调用点的阶段标签与请求 trace（ADR 0028）。

每个接入的 stage 至少一个用例（stub provider 加了 instrument_llm_call）；ask 路径上不出现 other；
分解计入：1 个子问题时一条 trace 同时有 decompose 与 synthesis，2 个子问题时共 3 条 trace，父 trace 只有
decompose（加上 Adaptive 的 classify），子 trace 里没有 decompose。
"""

import logging
import os
from datetime import date
from pathlib import Path

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion, Choice, ResponseMessage, Usage

from ragspine import RAGSpine
from ragspine.agent.agent import answer_question
from ragspine.agent.decompose import ROUTE_DECOMPOSED, LLMQueryDecomposer
from ragspine.agent.llm_provider import MockProvider
from ragspine.agent.query_transform import (
    AdaptiveDecomposer,
    HyDERetriever,
    LLMComplexityClassifier,
    RAGFusionRetriever,
    StepBackRetriever,
)
from ragspine.common.observability.llm_calls import (
    STAGE_OTHER,
    instrument_llm_call,
    record_llm_calls,
)
from ragspine.eval.nl_gold_ragspine import ForcedNarrativeIntentParser
from ragspine.retrieval.link.narrative_link import ProviderListwiseJudge
from ragspine.retrieval.translation.translator import LLMQueryTranslator
from ragspine.storage.fact_store import SqliteFactStore

REF = date(2026, 9, 24)


class _Scripted:
    """按 system prompt 选回复的 stub provider（加了装饰器，模拟接入埋点的 provider）。"""

    def __init__(self, replies: dict[str, str] | None = None, default: str = "ok"):
        self.replies = replies or {}
        self.default = default

    @instrument_llm_call
    def chat(self, messages, *, tools=None):
        system = str(messages[0].get("content") or "") if messages else ""
        text = next((v for k, v in self.replies.items() if k in system), self.default)
        msg = ResponseMessage(role="assistant", content=text)
        return ChatCompletion(
            choices=(Choice(index=0, message=msg, finish_reason="stop"),),
            usage=Usage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        )


class _Base:
    def retrieve(self, query, *, filters=None, top_k=50):
        return [
            {
                "chunk_id": "c1",
                "text": "1H26 VONB was US$514m, up 15 per cent.",
                "doc_id": "deck.md",
                "locator": "deck.md@page=10#para1-5",
            }
        ]


def _stages(bucket):
    return [c.stage for c in bucket.calls]


# ---------------------------------------------------------------------------
# 每个 stage 至少一个用例
# ---------------------------------------------------------------------------


def test_decompose_stage():
    with record_llm_calls() as bucket:
        LLMQueryDecomposer(_Scripted(default='["a", "b"]')).decompose("q")
    assert _stages(bucket) == ["decompose"]


def test_classify_stage():
    with record_llm_calls() as bucket:
        LLMComplexityClassifier(_Scripted(default="single")).classify("q")
    assert _stages(bucket) == ["classify"]


@pytest.mark.parametrize(
    ("cls", "stage", "reply"),
    [
        (HyDERetriever, "hyde", "假想文档"),
        (RAGFusionRetriever, "rag_fusion", '["变体一"]'),
        (StepBackRetriever, "step_back", "更宽的问题"),
    ],
)
def test_query_transform_stages(cls, stage, reply):
    with record_llm_calls() as bucket:
        cls(_Base(), _Scripted(default=reply)).retrieve("q")
    assert _stages(bucket) == [stage]


def test_translation_stage():
    with record_llm_calls() as bucket:
        LLMQueryTranslator(_Scripted(default="What is VONB")).translate(
            "VONB 是多少", target_language="en"
        )
    assert _stages(bucket) == ["translation"]


def test_listwise_rerank_stage():
    with record_llm_calls() as bucket:
        ProviderListwiseJudge(_Scripted(default="[2] > [1]")).judge("q", ["a", "b"])
    assert _stages(bucket) == ["listwise_rerank"]


@pytest.fixture
def store(tmp_path):
    fs = SqliteFactStore(tmp_path / "f.db")
    fs.init_schema()
    yield fs
    fs.close()


def _traces(caplog):
    return [r for r in caplog.records if r.name == "ragspine.trace"]


def test_tool_round_stage_and_trace_fields(store, caplog):
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        answer_question("香港FY2025的REVENUE是多少", store, _Scripted(), reference_date=REF)
    (trace,) = _traces(caplog)
    assert [c["stage"] for c in trace.llm_calls] == ["tool_round"]
    assert (trace.llm_n_calls, trace.llm_n_retried) == (1, 0)
    assert trace.llm_ms == sum(c["ms"] for c in trace.llm_calls)
    assert trace.llm_calls[0]["in_tokens"] == 5 and trace.llm_calls[0]["out_tokens"] == 3


def test_synthesis_stage(store, caplog):
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        answer_question(
            "What was VONB in 1H26?",
            store,
            _Scripted(default="1H26 VONB 为 US$514m。"),
            reference_date=REF,
            narrative_retriever=_Base(),
            intent_parser=ForcedNarrativeIntentParser(),
        )
    (trace,) = _traces(caplog)
    assert [c["stage"] for c in trace.llm_calls] == ["synthesis"]


def test_zero_llm_trace_has_no_llm_keys(store, caplog):
    """越权拒答（零 LLM）：trace 不出现任何 llm_* 键（逐字节不变）。"""
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        answer_question("竞安去年REVENUE多少", store, _Scripted(), reference_date=REF)
    (trace,) = _traces(caplog)
    assert not [k for k in vars(trace) if k.startswith("llm_")]


# ---------------------------------------------------------------------------
# ask 路径上不出现 other
# ---------------------------------------------------------------------------


class _LLMBase(_Base):
    """检索内部也有 LLM 调用：翻译 + listwise（与真实 NarrativeIndex 同样经 provider.chat）。"""

    def __init__(self, provider):
        self.translator = LLMQueryTranslator(provider)
        self.judge = ProviderListwiseJudge(provider)

    def retrieve(self, query, *, filters=None, top_k=50):
        self.translator.translate(query, target_language="en")
        self.judge.judge(query, ["a", "b"])
        return super().retrieve(query, filters=filters, top_k=top_k)


def test_every_wired_stage_on_one_request_and_no_other(store, caplog):
    provider = _Scripted(
        replies={"查询分解器": '["What was VONB in 1H26?"]'},
        default="multi",
    )
    retriever = StepBackRetriever(
        RAGFusionRetriever(HyDERetriever(_LLMBase(provider), provider), provider), provider
    )
    decomposer = AdaptiveDecomposer(LLMComplexityClassifier(provider), LLMQueryDecomposer(provider))
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        answer_question(
            "What was VONB in 1H26?",
            store,
            provider,
            reference_date=REF,
            narrative_retriever=retriever,
            intent_parser=ForcedNarrativeIntentParser(),
            decomposer=decomposer,
        )
    llm_traces = [t for t in _traces(caplog) if hasattr(t, "llm_calls")]
    (trace,) = llm_traces
    stages = [c["stage"] for c in trace.llm_calls]
    assert STAGE_OTHER not in stages
    assert set(stages) == {
        "classify",
        "decompose",
        "step_back",
        "rag_fusion",
        "hyde",
        "translation",
        "listwise_rerank",
        "synthesis",
    }
    assert stages[:2] == ["classify", "decompose"] and stages[-1] == "synthesis"


def test_real_ask_path_has_no_other(tmp_path: Path, caplog):
    """RAGSpine.ask（真实检索装配：跨语言翻译 + provider listwise + 合成 / tool 循环）不出现 other。"""
    ws = tmp_path / "ws"
    deck = tmp_path / "deck.md"
    deck.write_text(
        "# Results\n\nAgency contributed 72% of VONB in 1H26.\n\n"
        "<!-- PageBreak -->\n\nThe record ROE of 17.5% was achieved.\n",
        encoding="utf-8",
    )
    RAGSpine.local(ws).ingest(deck)
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        with RAGSpine.local(ws, provider=MockProvider(reference_date=REF)) as rag:
            rag.ask("代理渠道占 VONB 的比例是多少")
    stages = [c["stage"] for t in _traces(caplog) for c in getattr(t, "llm_calls", [])]
    assert stages, "ask 路径应至少有一次 LLM 调用"
    assert STAGE_OTHER not in stages
    assert {"translation", "listwise_rerank"} <= set(stages)


# ---------------------------------------------------------------------------
# 分解计入
# ---------------------------------------------------------------------------


def test_decompose_single_subquestion_one_trace(store, caplog):
    provider = _Scripted(
        replies={"查询分解器": '["What was VONB in 1H26?"]'}, default="1H26 VONB 为 US$514m。"
    )
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        answer_question(
            "What was VONB in 1H26?",
            store,
            provider,
            reference_date=REF,
            narrative_retriever=_Base(),
            intent_parser=ForcedNarrativeIntentParser(),
            decomposer=LLMQueryDecomposer(provider),
        )
    (trace,) = _traces(caplog)
    assert [c["stage"] for c in trace.llm_calls] == ["decompose", "synthesis"]


def test_decompose_two_subquestions_three_traces(store, caplog):
    provider = _Scripted(
        replies={"查询分解器": '["What was VONB in 1H26?", "Why did VONB grow?"]'},
        default="1H26 VONB 为 US$514m。",
    )
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        result = answer_question(
            "What was VONB in 1H26 and why?",
            store,
            provider,
            reference_date=REF,
            narrative_retriever=_Base(),
            intent_parser=ForcedNarrativeIntentParser(),
            decomposer=LLMQueryDecomposer(provider),
        )
    assert result.route == ROUTE_DECOMPOSED
    traces = _traces(caplog)
    assert len(traces) == 3
    *children, parent = traces
    assert parent.route == ROUTE_DECOMPOSED and parent.n_subquestions == 2
    assert [c["stage"] for c in parent.llm_calls] == ["decompose"]
    assert not hasattr(parent, "tool_status_counts")  # batch 的 requests 计数不受影响
    for child in children:
        assert [c["stage"] for c in child.llm_calls] == ["synthesis"]
        assert child.request_id != parent.request_id
