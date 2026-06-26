"""groundedness 度量单测（W5，TDD：先红后绿）。

覆盖离线确定性默认方法（词面重叠蕴含启发式）：
- claim 切分（剥引用尾注/序号标记/框架模板句）；
- faithfulness：答案每条 claim 是否被检索 context 蕴含（echo→忠实；夹带新主张→抓到）；
- free-text answer-accuracy：答案是否覆盖期望文档的实质内容（recall 取向重叠）；
- 蕴含判定缝（EntailmentJudge）+ 工厂（默认 lexical；nli/llm 为 opt-in follow-up）。
全链路零网络、零模型。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.eval.groundedness import (
    ANSWER_ACCURACY_RECALL_THRESHOLD,
    FAITHFULNESS_COVERAGE_THRESHOLD,
    EntailmentJudge,
    LexicalOverlapJudge,
    answer_accuracy,
    faithfulness,
    make_entailment_judge,
    split_claims,
)


# ---------------------------------------------------------------------------
# claim 切分
# ---------------------------------------------------------------------------

def test_split_claims_strips_citation_and_index_markers():
    answer = "[1] 香港监管动态：MPFA 新规要求披露管理费。（来源：REG_WATCH_HK.pptx para1）"
    claims = split_claims(answer)
    assert claims == ["香港监管动态：MPFA 新规要求披露管理费"]


def test_split_claims_drops_framing_templates():
    answer = "基于检索到的资料：\n香港 REVENUE 下降主因是 MCV 客群收缩。"
    claims = split_claims(answer)
    # "基于检索到的资料" 是系统框架句，不是世界断言 → 不计入 claim。
    assert claims == ["香港 REVENUE 下降主因是 MCV 客群收缩"]


def test_split_claims_empty_answer():
    assert split_claims("") == []
    assert split_claims("（来源：X.pptx p1）") == []


# ---------------------------------------------------------------------------
# 词面重叠蕴含判定器
# ---------------------------------------------------------------------------

def test_lexical_judge_entails_substring_echo():
    judge = LexicalOverlapJudge()
    ctx = "香港 REVENUE 下降主因是 MCV 客群收缩与银保渠道调整。"
    assert judge.entails("香港 REVENUE 下降主因是 MCV 客群收缩", ctx)


def test_lexical_judge_rejects_unrelated_claim():
    judge = LexicalOverlapJudge()
    ctx = "香港 REVENUE 下降主因是 MCV 客群收缩与银保渠道调整。"
    # 引入 context 里完全没有的新主张/新数字。
    assert not judge.entails("香港 REVENUE 暴增到 9999 亿美元，利润翻倍。", ctx)


def test_lexical_judge_coverage_threshold_is_module_constant():
    assert LexicalOverlapJudge().threshold == FAITHFULNESS_COVERAGE_THRESHOLD


def test_make_entailment_judge_default_is_lexical():
    judge = make_entailment_judge()
    assert isinstance(judge, LexicalOverlapJudge)
    assert isinstance(judge, EntailmentJudge)


def test_make_entailment_judge_optin_kinds_are_followups():
    for kind in ("nli", "llm"):
        with pytest.raises(NotImplementedError):
            make_entailment_judge(kind)


# ---------------------------------------------------------------------------
# faithfulness：答案每条 claim 必须被 context 蕴含
# ---------------------------------------------------------------------------

_CTX = ["香港 REVENUE 下降主因是 MCV 客群收缩与银保渠道调整，趋势上短期仍有压力。"]


def test_faithfulness_passes_when_answer_echoes_context():
    res = faithfulness("香港 REVENUE 下降主因是 MCV 客群收缩与银保渠道调整。", _CTX)
    assert res.ok
    assert res.unsupported == []
    assert res.score == 1.0


def test_faithfulness_flags_unentailed_claim():
    answer = (
        "香港 REVENUE 下降主因是 MCV 客群收缩。\n"
        "香港 REVENUE 暴增到 9999 亿美元，利润翻倍。"  # context 里没有的编造主张
    )
    res = faithfulness(answer, _CTX)
    assert not res.ok
    assert any("9999" in c for c in res.unsupported)
    assert res.score < 1.0


def test_faithfulness_empty_answer_is_vacuously_ok():
    res = faithfulness("", _CTX)
    assert res.ok
    assert res.n_claims == 0
    assert res.score == 1.0


# ---------------------------------------------------------------------------
# free-text answer-accuracy：覆盖期望文档实质内容
# ---------------------------------------------------------------------------

_REF = "中国 REVENUE 增长由代理人产能提升与银保渠道扩张驱动。"


def test_answer_accuracy_high_when_answer_conveys_reference():
    acc = answer_accuracy(
        "基于检索到的资料：中国 REVENUE 增长由代理人产能提升与银保渠道扩张驱动。", _REF
    )
    assert acc >= ANSWER_ACCURACY_RECALL_THRESHOLD


def test_answer_accuracy_low_when_answer_is_offtopic():
    acc = answer_accuracy("香港监管动态：MPFA 强积金新规要求披露管理费。", _REF)
    assert acc < ANSWER_ACCURACY_RECALL_THRESHOLD


def test_answer_accuracy_empty_reference_is_vacuously_one():
    assert answer_accuracy("任意答案", "") == 1.0
