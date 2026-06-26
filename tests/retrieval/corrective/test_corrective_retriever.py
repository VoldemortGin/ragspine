"""W6b 纠错检索（Corrective Retrieval / CRAG）单元测试 + grade→act 环语义 + 工厂选型。

设计意图（docs/prd-quality-depth.md W6b）：把 narrative_link 的单点 retry_without_filters 兜底泛化为
一个【有界、确定性、可观测】的 grade→act 环——先打相关性分，不达标按序纠错（drop_filters /
rewrite_query），仍不达标诚实拒答（返回空）。默认 grader 纯词面、零模型、零网络；opt-in 默认关，
make_corrective_retriever('none') 返回 base 本身（字节不变）。

红色策略：全部用 FakeBase 替身（记录调用、按脚本返回片段），零网络、零真实模型。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

import ragspine.retrieval.corrective as corrective_mod
from ragspine.retrieval.corrective import (
    CORRECTIVE_ENV,
    CorrectiveRetriever,
    GradeAction,
    LexicalOverlapGrader,
    RelevanceGrader,
    make_corrective_retriever,
)

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class FakeBase:
    """NarrativeRetriever 替身：记录每次 retrieve 调用，按 responder(query, filters) 返回片段。"""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict] = []

    def retrieve(self, query, *, filters=None, top_k=50):
        self.calls.append({"query": query, "filters": filters, "top_k": top_k})
        return self.responder(query, filters)


def _snip(text: str, doc_id: str = "D1.pptx") -> dict[str, object]:
    return {"text": text, "doc_id": doc_id, "chunk_id": f"{doc_id}#c0"}


RELEVANT = _snip("香港 REVENUE 下降 MCV 客群 收缩")
IRRELEVANT = _snip("巧克力 蛋糕 食谱 周末 板球 比赛", "X.pptx")
QUERY = "香港 REVENUE 下降 MCV"


# ---------------------------------------------------------------------------
# grade→act 环：高分直返 / drop_filters / rewrite_query / 拒答 / 有界 / 确定性
# ---------------------------------------------------------------------------

def test_high_grade_first_attempt_returns_snippets():
    """首次检索即高分 -> 直接返回，恰一次 base 调用，last_actions 仅 retrieve，无拒答。"""
    base = FakeBase(lambda q, f: [RELEVANT])
    cr = CorrectiveRetriever(base)
    out = cr.retrieve(QUERY, filters={"entity": "ACME_HK"})
    assert out == [RELEVANT]
    assert len(base.calls) == 1
    assert [a.action for a in cr.last_actions] == ["retrieve"]
    assert isinstance(cr.last_actions[0], GradeAction)
    assert cr.last_actions[0].grade >= cr.min_grade
    assert cr.last_actions[0].snippet_count == 1
    assert all(a.action != "refuse" for a in cr.last_actions)


def test_low_grade_with_filters_drops_filters_and_returns():
    """带过滤低分 -> drop_filters 去过滤重检；若达标返回该结果；trace 按序记两步。"""
    def responder(q, f):
        return [IRRELEVANT] if f else [RELEVANT]

    base = FakeBase(responder)
    cr = CorrectiveRetriever(base)
    out = cr.retrieve(QUERY, filters={"entity": "ACME_TH"})
    assert out == [RELEVANT]
    assert [a.action for a in cr.last_actions] == ["retrieve", "drop_filters"]
    assert len(base.calls) == 2
    assert base.calls[0]["filters"] == {"entity": "ACME_TH"}
    assert base.calls[1]["filters"] is None


def test_all_low_grade_refuses_and_is_bounded():
    """所有尝试都低分 -> 返回 [] 拒答，last_actions 末位为 refuse，且有界（base 调用 ≤ max_retries+1）。"""
    base = FakeBase(lambda q, f: [IRRELEVANT])
    cr = CorrectiveRetriever(base, query_rewriter=lambda q: q + " 改写")
    out = cr.retrieve(QUERY, filters={"entity": "ACME_TH"})
    assert out == []
    assert cr.last_actions[-1].action == "refuse"
    assert cr.last_actions[-1].snippet_count == 0
    assert len(base.calls) <= cr.max_retries + 1
    assert len(base.calls) == 3  # retrieve + drop_filters + rewrite_query


def test_max_retries_clamped_to_two():
    """max_retries=99 -> clamp 到 2：至多 2 次纠错重检（base 调用 ≤ 3）。"""
    base = FakeBase(lambda q, f: [IRRELEVANT])
    cr = CorrectiveRetriever(
        base, max_retries=99, query_rewriter=lambda q: q + " 改写"
    )
    assert cr.max_retries == 2
    cr.retrieve(QUERY, filters={"entity": "ACME_TH"})
    assert len(base.calls) <= 3


def test_max_retries_zero_refuses_immediately():
    """max_retries=0 -> 首尝试低分即拒答，无任何纠错重检（仅 1 次 base 调用）。"""
    base = FakeBase(lambda q, f: [IRRELEVANT])
    cr = CorrectiveRetriever(base, max_retries=0, query_rewriter=lambda q: q + " x")
    out = cr.retrieve(QUERY, filters={"entity": "ACME_TH"})
    assert out == []
    assert len(base.calls) == 1
    assert [a.action for a in cr.last_actions] == ["retrieve", "refuse"]


def test_query_rewriter_used_for_third_attempt():
    """drop_filters 仍低分且给了 rewriter -> 用改写后的 query 做第三次尝试。"""
    def responder(q, f):
        return [RELEVANT] if "客群收缩" in q else [IRRELEVANT]

    base = FakeBase(responder)
    cr = CorrectiveRetriever(base, query_rewriter=lambda q: q + " 客群收缩")
    out = cr.retrieve(QUERY, filters={"entity": "ACME_TH"})
    assert out == [RELEVANT]
    assert [a.action for a in cr.last_actions] == [
        "retrieve", "drop_filters", "rewrite_query",
    ]
    assert base.calls[2]["query"] == QUERY + " 客群收缩"
    assert base.calls[2]["filters"] is None


def test_no_corrective_actions_when_no_filters_no_rewriter():
    """无过滤、无 rewriter 且首尝试低分 -> 直接拒答（无可执行的纠错动作）。"""
    base = FakeBase(lambda q, f: [IRRELEVANT])
    cr = CorrectiveRetriever(base)
    out = cr.retrieve(QUERY, filters=None)
    assert out == []
    assert len(base.calls) == 1
    assert [a.action for a in cr.last_actions] == ["retrieve", "refuse"]


def test_determinism_same_base_two_calls():
    """同一 fake base 上两次相同 retrieve -> 输出一致、last_actions 分数一致（确定性）。"""
    base = FakeBase(lambda q, f: [RELEVANT] if f else [IRRELEVANT])
    cr = CorrectiveRetriever(base)
    out_a = cr.retrieve(QUERY, filters={"entity": "ACME_HK"})
    grades_a = [a.grade for a in cr.last_actions]
    out_b = cr.retrieve(QUERY, filters={"entity": "ACME_HK"})
    grades_b = [a.grade for a in cr.last_actions]
    assert out_a == out_b
    assert grades_a == grades_b


def test_last_actions_reset_each_call():
    """last_actions 每次 retrieve 重置，不跨调用累积。"""
    base = FakeBase(lambda q, f: [RELEVANT])
    cr = CorrectiveRetriever(base)
    cr.retrieve(QUERY)
    first = list(cr.last_actions)
    cr.retrieve(QUERY)
    assert cr.last_actions == first  # 重置后重建，长度不累积


# ---------------------------------------------------------------------------
# 可观测性：emit 仅传非敏感元数据键
# ---------------------------------------------------------------------------

def test_emit_passes_only_nonsensitive_keys(monkeypatch):
    """emit=True -> 仅发 corrective_actions / corrective_grades（非敏感，绝不含片段文本）。"""
    captured: dict = {}
    monkeypatch.setattr(
        corrective_mod, "emit_trace", lambda *a, **f: captured.update(f)
    )
    base = FakeBase(lambda q, f: [RELEVANT])
    CorrectiveRetriever(base).retrieve(QUERY)
    assert set(captured) == {"corrective_actions", "corrective_grades"}
    assert captured["corrective_actions"] == ["retrieve"]


def test_emit_false_suppresses_trace(monkeypatch):
    """emit=False -> 不发任何 trace。"""
    called = {"n": 0}
    monkeypatch.setattr(
        corrective_mod, "emit_trace", lambda *a, **f: called.__setitem__("n", called["n"] + 1)
    )
    base = FakeBase(lambda q, f: [RELEVANT])
    CorrectiveRetriever(base, emit=False).retrieve(QUERY)
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# LexicalOverlapGrader：词面重叠相关性分
# ---------------------------------------------------------------------------

def test_grader_implements_relevance_grader_protocol():
    assert isinstance(LexicalOverlapGrader(), RelevanceGrader)


def test_grader_echoing_snippet_scores_high():
    """片段回显 query 的内容词 -> 高分（≥ 默认阈值）。"""
    g = LexicalOverlapGrader()
    assert g.grade(QUERY, [_snip("香港 REVENUE 下降 MCV 客群 收缩")]) >= 0.5


def test_grader_unrelated_snippet_scores_zero():
    """与 query 无共词的片段 -> 0.0。"""
    g = LexicalOverlapGrader()
    assert g.grade("香港 REVENUE 下降", [_snip("巧克力 蛋糕 食谱 周末")]) == 0.0


def test_grader_empty_snippets_zero():
    """片段为空 -> 0.0。"""
    assert LexicalOverlapGrader().grade("香港 REVENUE", []) == 0.0


def test_grader_no_content_tokens_one():
    """query 无内容词（纯标点）-> 1.0。"""
    assert LexicalOverlapGrader().grade("！？。", [_snip("anything")]) == 1.0


def test_grader_reads_content_fallback_field():
    """片段文本访问器：text 缺失时回落 content。"""
    g = LexicalOverlapGrader()
    assert g.grade("revenue", [{"content": "revenue grew"}]) == 1.0


def test_grader_partial_overlap_fraction():
    """部分重叠 -> 命中内容词占比。"""
    g = LexicalOverlapGrader()
    # query 内容词 ["revenue", "drop"]，片段含 revenue 不含 drop -> 1/2。
    assert g.grade("revenue drop", [_snip("revenue grew strongly")]) == 0.5


# ---------------------------------------------------------------------------
# 工厂 make_corrective_retriever：none 字节不变 / crag 别名 / 未知 / env 驱动
# ---------------------------------------------------------------------------

def test_make_none_returns_same_object(monkeypatch):
    """'none' / 缺省 -> 返回 base 本身（is base，opt-out 字节不变，默认）。"""
    monkeypatch.delenv(CORRECTIVE_ENV, raising=False)
    base = FakeBase(lambda q, f: [])
    assert make_corrective_retriever(base, "none") is base
    assert make_corrective_retriever(base) is base


@pytest.mark.parametrize("spec", ["crag", "corrective", "on", "CRAG", " corrective "])
def test_make_on_specs_return_corrective(spec):
    """'crag' / 'corrective' / 'on'（含大小写/留白/连字符归一）-> CorrectiveRetriever。"""
    base = FakeBase(lambda q, f: [])
    cr = make_corrective_retriever(base, spec)
    assert isinstance(cr, CorrectiveRetriever)
    assert cr.base is base


def test_make_unknown_spec_raises():
    """未知 spec -> ValueError（列清可用 spec）。"""
    base = FakeBase(lambda q, f: [])
    with pytest.raises(ValueError):
        make_corrective_retriever(base, "definitely-not-a-spec")


def test_make_forwards_kwargs_and_grader():
    """crag 路径透传 grader / min_grade / max_retries 等 kwargs。"""
    base = FakeBase(lambda q, f: [])
    grader = LexicalOverlapGrader()
    cr = make_corrective_retriever(
        base, "crag", grader=grader, min_grade=0.9, max_retries=1
    )
    assert isinstance(cr, CorrectiveRetriever)
    assert cr.grader is grader
    assert cr.min_grade == 0.9
    assert cr.max_retries == 1


def test_make_env_driven_selection(monkeypatch):
    """缺省 spec 读环境变量 RAGSPINE_CORRECTIVE。"""
    base = FakeBase(lambda q, f: [])
    monkeypatch.setenv(CORRECTIVE_ENV, "crag")
    assert isinstance(make_corrective_retriever(base), CorrectiveRetriever)
    monkeypatch.setenv(CORRECTIVE_ENV, "none")
    assert make_corrective_retriever(base) is base
