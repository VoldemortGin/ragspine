"""qa_eval 新增 groundedness 闸接线测试（W5，TDD：先红后绿）。

证明新 gate 有牙齿（非平凡）：
- 合成"不忠实答案"（夹带未被检索 context 蕴含的 claim）→ faithfulness gate 抓到 → fail；
  echo 忠实答案 → pass；
- 在【真实】eval KB + 检索链上重放：对真实检索 context 夹带编造主张同样被抓；
- 新 gate 并入 baseline ratchet（faithfulness/answer_accuracy 退化 → compare_to_baseline fail）；
- 全 golden 双模式 faithfulness/answer_accuracy 满分（忠实 KB 的自证）；
- 既有四命门 + fabrication 语义不被新 gate 改动。
"""

import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.eval.qa_eval import (
    ANSWER_ACCURACY,
    FAITHFULNESS,
    GATE_METRICS,
    GROUNDEDNESS_METRICS,
    CaseOutcome,
    GoldenCase,
    build_eval_kb,
    compare_to_baseline,
    eval_narrative_reference_texts,
    evaluate,
    make_baseline_entry,
    run_qa_eval,
)
from ragspine.retrieval.link.narrative_link import build_narrative_retriever

REF = date(2026, 6, 12)
GOLDEN_PATH = ROOT_DIR / "data" / "golden" / "qa_golden_set.jsonl"

_HK_REG_CTX = "香港监管动态：MPFA 强积金新规要求披露管理费，IA 加强销售流程审查。"


def _narrative_case(case_id: str = "n1", doc: str = "REG_WATCH_HK.pptx") -> GoldenCase:
    return GoldenCase(
        id=case_id, question="香港最近有什么监管动态", case_type="narrative",
        expected={"clarification": "none", "refuse": False, "narrative_doc": doc},
        tags={"topic": "REG", "scope": "ACME_HK", "qtype": "narrative"},
        reference_date=REF,
    )


def _faithful_outcome(case_id: str = "n1") -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id, clarification_mode="none",
        answer=f"{_HK_REG_CTX}（来源：REG_WATCH_HK.pptx para1）",
        narrative_answer=f"{_HK_REG_CTX}（来源：REG_WATCH_HK.pptx para1）",
        retrieved_context=[_HK_REG_CTX],
        sources=[{"doc": "REG_WATCH_HK.pptx", "locator": "para1"}],
    )


# ---------------------------------------------------------------------------
# 牙齿①：合成不忠实答案 → faithfulness gate 抓到
# ---------------------------------------------------------------------------

def test_faithful_narrative_answer_passes_faithfulness_gate():
    report = evaluate([_narrative_case()], {"n1": _faithful_outcome()}, mode="tool")
    m = report.metrics[FAITHFULNESS]
    assert (m.total, m.passed) == (1, 1)


def test_unfaithful_narrative_answer_fails_faithfulness_gate():
    bad = _faithful_outcome()
    # 夹带一条检索 context 里根本没有的编造主张。
    bad.narrative_answer = (
        f"{_HK_REG_CTX}\n香港 REVENUE 暴增到 9999 亿美元，利润翻倍。"
    )
    report = evaluate([_narrative_case()], {"n1": bad}, mode="tool")
    m = report.metrics[FAITHFULNESS]
    assert (m.total, m.passed) == (1, 0)
    assert m.failures[0]["id"] == "n1"


# ---------------------------------------------------------------------------
# 牙齿②：在真实 eval KB + 检索链上重放（非平凡——对真实 context 也能抓）
# ---------------------------------------------------------------------------

def test_faithfulness_catches_fabrication_against_real_retrieved_context(tmp_path):
    _, chunk_db = build_eval_kb(tmp_path)
    retriever, store = build_narrative_retriever(chunk_db)
    try:
        snippets = retriever.retrieve(
            "香港最近有什么监管动态", filters={"entity": "ACME_HK"}, top_k=50
        )
        context = [str(s.get("text", "")) for s in snippets]
        assert context, "真实检索应返回 context"
        grounded = "\n".join(context)
        fabricated = grounded + "\n香港 REVENUE 暴增到 9999 亿美元，利润翻倍。"

        ok_outcome = CaseOutcome(
            case_id="n1", narrative_answer=grounded, retrieved_context=context
        )
        bad_outcome = CaseOutcome(
            case_id="n1", narrative_answer=fabricated, retrieved_context=context
        )
        case = _narrative_case()
        assert evaluate([case], {"n1": ok_outcome}).metrics[FAITHFULNESS].passed == 1
        assert evaluate([case], {"n1": bad_outcome}).metrics[FAITHFULNESS].passed == 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# free-text answer-accuracy gate
# ---------------------------------------------------------------------------

def test_answer_accuracy_gate_passes_for_on_topic_and_fails_offtopic():
    refs = {"REG_WATCH_HK.pptx": _HK_REG_CTX}
    on_topic = _faithful_outcome()
    off_topic = _faithful_outcome()
    off_topic.narrative_answer = "中国 REVENUE 增长由代理人产能提升与银保渠道扩张驱动。"
    case = _narrative_case()
    assert evaluate(
        [case], {"n1": on_topic}, reference_texts=refs
    ).metrics[ANSWER_ACCURACY].passed == 1
    assert evaluate(
        [case], {"n1": off_topic}, reference_texts=refs
    ).metrics[ANSWER_ACCURACY].passed == 0


# ---------------------------------------------------------------------------
# 并入 baseline ratchet：新 gate 退化 → compare_to_baseline fail
# ---------------------------------------------------------------------------

def test_groundedness_metrics_folded_into_baseline_entry():
    report = evaluate(
        [_narrative_case()], {"n1": _faithful_outcome()},
        reference_texts={"REG_WATCH_HK.pptx": _HK_REG_CTX},
    )
    entry = make_baseline_entry(report)
    for name in GROUNDEDNESS_METRICS:
        assert name in entry["metrics"]
        assert entry["metrics"][name] == 1.0


def test_faithfulness_regression_blocks_ratchet():
    good = evaluate(
        [_narrative_case()], {"n1": _faithful_outcome()},
        reference_texts={"REG_WATCH_HK.pptx": _HK_REG_CTX},
    )
    baseline = make_baseline_entry(good)
    bad_outcome = _faithful_outcome()
    bad_outcome.narrative_answer = f"{_HK_REG_CTX}\n香港 REVENUE 暴增到 9999 亿美元。"
    regressed = evaluate(
        [_narrative_case()], {"n1": bad_outcome},
        reference_texts={"REG_WATCH_HK.pptx": _HK_REG_CTX},
    )
    cmp = compare_to_baseline(regressed, baseline)
    assert not cmp.passed
    assert FAITHFULNESS in [r["metric"] for r in cmp.regressions]


def test_eval_narrative_reference_texts_covers_expected_docs():
    refs = eval_narrative_reference_texts()
    for doc in ("REG_WATCH_HK.pptx", "HK_QBR_2025Q4.pptx",
                "CN_QBR_2025.pptx", "REG_WATCH_CN.pptx"):
        assert doc in refs and refs[doc].strip()


# ---------------------------------------------------------------------------
# 端到端：双模式全 golden faithfulness/answer_accuracy 满分 + 四命门不受影响
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["tool", "agent"])
def test_e2e_full_golden_groundedness_perfect(tmp_path, mode):
    report = run_qa_eval(GOLDEN_PATH, mode=mode, kb_dir=tmp_path)
    for name in GROUNDEDNESS_METRICS:
        m = report.metrics[name]
        assert m.total > 0, f"{name} 在 {mode} 下没有任何样本"
        assert m.pass_rate == 1.0, f"{name}({mode}) 失败：{m.failures}"
    # 既有四命门仍满分（新 gate 不改其语义）。
    for name in GATE_METRICS:
        assert report.metrics[name].pass_rate == 1.0
    assert report.fabrication_count == 0
