"""Claude listwise 二审测试（叙事通路检索侧，TDD 红色阶段）。

只验证外部行为：prompt 构造（确定性、编号清单）、模型回文鲁棒解析（乱序/重复/越界/
缺漏/乱文容错）、rerank 编排（judge 排序生效、top_n、judge 缺省/异常/非法返回退化为
RRF 序、Restricted 文本绝不送 judge 且原位保留、全 Restricted 不调 judge）。

judge 全部用测试内确定性替身，零网络、零 SDK。
红色预期：三个行为入口因 stub raise NotImplementedError 而全部 FAIL。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.rerank.listwise_rerank import (
    DEFAULT_TOP_N,
    RESTRICTED_SENSITIVITY,
    build_listwise_prompt,
    listwise_rerank,
    parse_listwise_response,
)
from ragspine.retrieval.lexical.retrieval import RetrievalResult


# ---------------------------------------------------------------------------
# 测试专用替身与构造器
# ---------------------------------------------------------------------------

class FakeJudge:
    """固定返回预设排序（或调用预设函数）的确定性 judge。"""

    def __init__(self, order=None, fn=None):
        self._order = order
        self._fn = fn

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        if self._fn is not None:
            return self._fn(query, candidates)
        if self._order is not None:
            return self._order
        return list(range(len(candidates)))


class ReverseJudge:
    """按倒序返回的 judge。"""

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        return list(range(len(candidates)))[::-1]


class SpyJudge(ReverseJudge):
    """录下每次调用的倒序 judge。"""

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        self.calls.append((query, list(candidates)))
        return super().judge(query, candidates)


class FailJudge:
    """一调用就抛异常的 judge（退化路径用）。"""

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        raise RuntimeError("judge unavailable")


def _result(i: int, text: str, sensitivity: str = "INTERNAL") -> RetrievalResult:
    chunk = Chunk(
        chunk_id=f"d{i}#c0",
        doc_id=f"d{i}",
        seq=0,
        text=text,
        source_locator=f"d{i}#para1",
        para_start=1,
        para_end=1,
        sensitivity=sensitivity,
    )
    return RetrievalResult(
        chunk=chunk, bm25_score=1.0, vector_score=0.0, fused_score=1.0 / (i + 1)
    )


def _results(n: int) -> list[RetrievalResult]:
    return [_result(i, f"候选文本 {i}") for i in range(n)]


# ===========================================================================
# prompt 构造
# ===========================================================================

def test_prompt_contains_query_and_candidates():
    """prompt 含查询与全部候选文本。"""
    prompt = build_listwise_prompt("REVENUE 表现", ["候选甲", "candidate B", "候选丙"])
    assert "REVENUE 表现" in prompt
    assert "候选甲" in prompt and "candidate B" in prompt and "候选丙" in prompt


def test_prompt_numbers_candidates():
    """候选以 [i] 编号呈现（0-based），便于模型按下标作答。"""
    prompt = build_listwise_prompt("q", ["a", "b", "c"])
    assert "[0]" in prompt and "[1]" in prompt and "[2]" in prompt


def test_prompt_deterministic():
    """同输入两次构造完全一致。"""
    args = ("查询", ["x", "y"])
    assert build_listwise_prompt(*args) == build_listwise_prompt(*args)


# ===========================================================================
# 回文解析：鲁棒容错
# ===========================================================================

def test_parse_comma_separated():
    assert parse_listwise_response("2,0,1", 3) == [2, 0, 1]


def test_parse_bracketed_ranking():
    assert parse_listwise_response("[2] > [0] > [1]", 3) == [2, 0, 1]


def test_parse_dedup_keeps_first():
    """重复下标去重保首现，缺漏升序补尾。"""
    assert parse_listwise_response("1, 1, 0", 3) == [1, 0, 2]


def test_parse_out_of_range_dropped():
    """越界下标丢弃。"""
    assert parse_listwise_response("7, 2, 0", 3) == [2, 0, 1]


def test_parse_missing_appended_in_order():
    """缺漏下标按升序补到尾部。"""
    assert parse_listwise_response("2", 4) == [2, 0, 1, 3]


def test_parse_verbose_narrative():
    """叙述式回文按出现顺序取下标。"""
    assert parse_listwise_response("最相关的是 [1]，其次是 0。", 3) == [1, 0, 2]


def test_parse_garbage_identity():
    """完全无合法下标 -> 恒等排列。"""
    assert parse_listwise_response("我无法对这些候选排序", 3) == [0, 1, 2]


def test_parse_empty_inputs():
    assert parse_listwise_response("", 3) == [0, 1, 2]
    assert parse_listwise_response("anything", 0) == []


# ===========================================================================
# rerank 编排：排序 / top_n / 退化
# ===========================================================================

def test_rerank_orders_by_judge():
    """judge 倒序 -> 输出倒序。"""
    results = _results(5)
    out = listwise_rerank("q", results, ReverseJudge())
    assert [r.chunk.chunk_id for r in out] == [
        r.chunk.chunk_id for r in reversed(results)
    ]


def test_rerank_default_top_n_10():
    """默认 top_n=10：15 条输入只出 10 条。"""
    assert DEFAULT_TOP_N == 10
    out = listwise_rerank("q", _results(15), FakeJudge())
    assert len(out) == 10
    assert [r.chunk.chunk_id for r in out] == [
        r.chunk.chunk_id for r in _results(15)[:10]
    ]


def test_rerank_top_n_parameterized():
    out = listwise_rerank("q", _results(5), ReverseJudge(), top_n=3)
    assert len(out) == 3


def test_rerank_judge_none_keeps_rrf_order():
    """judge=None -> 直接截 top_n、保持输入（RRF）序。"""
    results = _results(12)
    out = listwise_rerank("q", results, None)
    assert [r.chunk.chunk_id for r in out] == [r.chunk.chunk_id for r in results[:10]]


def test_rerank_judge_exception_falls_back():
    """judge 抛异常 -> 退化为输入顺序，绝不抛死。"""
    results = _results(4)
    out = listwise_rerank("q", results, FailJudge())
    assert [r.chunk.chunk_id for r in out] == [r.chunk.chunk_id for r in results]


def test_rerank_judge_illegal_return_falls_back():
    """judge 返回非法内容（None / 非整数 / 全越界）-> 退化为输入顺序。"""
    results = _results(3)
    expect = [r.chunk.chunk_id for r in results]
    for bad in (None, ["b", "a"], [99, 42]):
        out = listwise_rerank("q", results, FakeJudge(order=bad))
        assert [r.chunk.chunk_id for r in out] == expect


def test_rerank_empty_results():
    assert listwise_rerank("q", [], ReverseJudge()) == []


# ===========================================================================
# Restricted 不出域（拍板硬约束，钉死）
# ===========================================================================

def test_restricted_text_never_sent_to_judge():
    """Restricted 块文本绝不出现在 judge 候选里。"""
    secret = "SECRET-EXEC-PR 高管评级"
    results = [
        _result(0, "普通文本甲"),
        _result(1, secret, sensitivity=RESTRICTED_SENSITIVITY),
        _result(2, "普通文本乙"),
    ]
    judge = SpyJudge()
    listwise_rerank("q", results, judge)
    assert len(judge.calls) == 1
    _, candidates = judge.calls[0]
    assert candidates == ["普通文本甲", "普通文本乙"]
    assert all(secret not in c for c in candidates)


def test_restricted_keeps_position_others_reranked():
    """Restricted 原位保留，非 Restricted 按 judge 序回填其余位置。

    输入 [n0, r1, n2, n3]，judge 对 [n0,n2,n3] 倒序 -> [n3,n2,n0]，
    合并后 = [n3, r1, n2, n0]。
    """
    results = [
        _result(0, "n0"),
        _result(1, "r1", sensitivity=RESTRICTED_SENSITIVITY),
        _result(2, "n2"),
        _result(3, "n3"),
    ]
    out = listwise_rerank("q", results, ReverseJudge())
    assert [r.chunk.text for r in out] == ["n3", "r1", "n2", "n0"]


def test_all_restricted_judge_not_called():
    """候选全 Restricted -> judge 完全不被调用，整体退化为 RRF 序。"""
    results = [
        _result(i, f"机密 {i}", sensitivity=RESTRICTED_SENSITIVITY) for i in range(3)
    ]
    judge = SpyJudge()
    out = listwise_rerank("q", results, judge)
    assert judge.calls == []
    assert [r.chunk.text for r in out] == ["机密 0", "机密 1", "机密 2"]


def test_restricted_case_insensitive():
    """sensitivity 大小写不敏感：'restricted' 同样不出域。"""
    results = [_result(0, "秘密文本", sensitivity="restricted")]
    judge = SpyJudge()
    listwise_rerank("q", results, judge)
    assert judge.calls == []
