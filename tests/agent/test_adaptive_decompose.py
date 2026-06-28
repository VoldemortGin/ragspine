"""W9 Adaptive-RAG（落在 decomposer 缝）单测：复杂度分类 + 仅多跳才拆 + 工厂选型。

反编造宪章：adaptive 只在【单跳/多跳】间路由，绝无"不检索直接答"一档。simple -> [原问题]
（回落正常单发）；complex -> 委托 base 分解器拆问。
"""

import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion, Choice, ProviderError, ResponseMessage

from ragspine.agent.decompose import (
    COMPLEXITY_COMPLEX,
    COMPLEXITY_SIMPLE,
    AdaptiveDecomposer,
    HeuristicComplexityClassifier,
    LLMComplexityClassifier,
    make_decomposer,
)


def _text_response(text: str) -> ChatCompletion:
    msg = ResponseMessage(role="assistant", content=text)
    return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="stop"),))


class ScriptedProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        return self._responses.pop(0)


class BoomProvider:
    def chat(self, messages, *, tools=None):
        raise ProviderError("boom")


class FakeDecomposer:
    def __init__(self, subquestions):
        self.subquestions = subquestions
        self.seen = []

    def decompose(self, question, *, reference_date=None):
        self.seen.append(question)
        return list(self.subquestions)


# --- 启发式分类 ---


def test_heuristic_flags_comparison_as_complex():
    c = HeuristicComplexityClassifier()
    assert c.classify("各区域的收入对比如何") == COMPLEXITY_COMPLEX
    assert c.classify("香港和新加坡分别增长多少") == COMPLEXITY_COMPLEX


def test_heuristic_flags_factual_plus_causal_as_complex():
    c = HeuristicComplexityClassifier()
    assert c.classify("香港FY2024收入是多少，又为什么下降") == COMPLEXITY_COMPLEX


def test_heuristic_simple_for_single_fact():
    c = HeuristicComplexityClassifier()
    assert c.classify("香港FY2024收入是多少") == COMPLEXITY_SIMPLE


# --- LLM 分类（带启发式兜底）---


def test_llm_classifier_reads_label():
    assert LLMComplexityClassifier(ScriptedProvider([_text_response("complex")])).classify("q") == COMPLEXITY_COMPLEX
    assert LLMComplexityClassifier(ScriptedProvider([_text_response("simple")])).classify("q") == COMPLEXITY_SIMPLE


def test_llm_classifier_falls_back_on_error():
    """provider 故障 -> 启发式兜底（这里问句含比较信号 -> complex）。"""
    c = LLMComplexityClassifier(BoomProvider())
    assert c.classify("各区域对比") == COMPLEXITY_COMPLEX


def test_llm_classifier_falls_back_on_garbage():
    """回文不含 simple/complex -> 启发式兜底（简单事实问 -> simple）。"""
    c = LLMComplexityClassifier(ScriptedProvider([_text_response("我不知道")]))
    assert c.classify("香港收入是多少") == COMPLEXITY_SIMPLE


# --- AdaptiveDecomposer ---


def test_adaptive_only_decomposes_complex():
    base = FakeDecomposer(["子问题1", "子问题2"])

    class AlwaysComplex:
        def classify(self, q, *, reference_date=None):
            return COMPLEXITY_COMPLEX

    out = AdaptiveDecomposer(base, AlwaysComplex()).decompose("q")
    assert out == ["子问题1", "子问题2"]
    assert base.seen == ["q"], "complex 才委托 base 分解器"


def test_adaptive_simple_returns_original_without_decomposing():
    base = FakeDecomposer(["不该被调用"])

    class AlwaysSimple:
        def classify(self, q, *, reference_date=None):
            return COMPLEXITY_SIMPLE

    out = AdaptiveDecomposer(base, AlwaysSimple()).decompose("q")
    assert out == ["q"]
    assert base.seen == [], "simple 不调用 base 分解器（省调用、降误拆）"


# --- make_decomposer('adaptive') ---


def test_make_decomposer_adaptive_needs_provider():
    assert make_decomposer("adaptive", provider=None) is None


def test_make_decomposer_adaptive_builds_adaptive():
    d = make_decomposer("adaptive", provider=ScriptedProvider([]))
    assert isinstance(d, AdaptiveDecomposer)


def test_make_decomposer_unknown_lists_adaptive():
    with pytest.raises(ValueError, match="adaptive"):
        make_decomposer("bogus", provider=ScriptedProvider([]))
