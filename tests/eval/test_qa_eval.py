"""qa_eval 评测闭环测试（TDD：先冻结红色测试，再实现到绿）。

覆盖：
- golden set 文件格式校验（必填字段、case_type、按类型的 expected 结构、id 唯一）；
- 四命门指标计算正确性（手造期望/实际对算分，绝不合并成笼统 pass rate）：
    ①数字准确率（exact match：数值+单位）②citation validity（答案对+来源错判 fail）
    ③refusal appropriateness（该拒答时拒答 + 不该拒答时不拒答，两个方向）
    ④clarification appropriateness（该澄清时澄清 + 完整问题不许反问，两个方向）；
- 编造数字检测（拒答类回答中除期间外出现数字即判 fabrication，目标 0）；
- 基线门禁（任一命门指标退化即 gate fail，仿 extraction_eval 模式）；
- 合成 KB 构建器确定性与幂等；
- tool-direct / agent（MockProvider）双模式端到端 + CLI 接线。
全链路零网络、零真实 LLM。
"""

import json
import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from scripts.run_qa_eval import main as qa_eval_main
import ragspine.eval.qa_eval as qa_eval_mod
from ragspine.common.company_profile import DimensionSpec, DomainProfile
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.storage.fact_store import FactStore
from ragspine.eval.qa_eval import (
    CITATION_VALIDITY,
    CLARIFICATION_APPROPRIATENESS,
    FABRICATION,
    GATE_METRICS,
    NUMERIC_ACCURACY,
    REFUSAL_APPROPRIATENESS,
    CaseOutcome,
    GoldenCase,
    build_eval_kb,
    compare_to_baseline,
    detect_fabricated_numbers,
    evaluate,
    load_golden_set,
    run_qa_eval,
)

REF = date(2026, 6, 12)
GOLDEN_PATH = ROOT_DIR / "data" / "golden" / "qa_golden_set.jsonl"


# ---------------------------------------------------------------------------
# 手造 case / outcome 工厂
# ---------------------------------------------------------------------------

def _numeric_case(case_id: str = "c1", **expected_over) -> GoldenCase:
    expected = {
        "clarification": "none",
        "refuse": False,
        "value": 1702.0,
        "unit": "USD_M",
        "source": {"doc": "R25.pptx", "locator": "slide=5"},
    }
    expected.update(expected_over)
    return GoldenCase(
        id=case_id, question="香港FY2025的REVENUE是多少", case_type="numeric",
        expected=expected,
        tags={"topic": "FIN", "scope": "ACME_HK", "qtype": "phrasing"},
        reference_date=REF,
    )


def _refusal_case(case_id: str = "r1") -> GoldenCase:
    return GoldenCase(
        id=case_id, question="中国去年ROE多少", case_type="refusal",
        expected={"clarification": "none", "refuse": True},
        tags={"topic": "FIN", "scope": "ACME_CN", "qtype": "adversarial"},
        reference_date=REF,
    )


def _clarify_case(case_id: str = "k1", mode: str = "ask_first") -> GoldenCase:
    return GoldenCase(
        id=case_id, question="香港去年多少", case_type="clarification",
        expected={"clarification": mode, "refuse": False},
        tags={"topic": "FIN", "scope": "ACME_HK", "qtype": "should_clarify"},
        reference_date=REF,
    )


def _ok_numeric_outcome(case_id: str = "c1") -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id, clarification_mode="none",
        answer="ACME_HK FY2025 REVENUE 为 1702 USD_M（来源：R25.pptx · slide=5）",
        found_value=1702.0, found_unit="USD_M",
        found_source={"doc": "R25.pptx", "locator": "slide=5"},
        refused=False,
        sources=[{"doc": "R25.pptx", "locator": "slide=5"}],
    )


def _ok_refusal_outcome(case_id: str = "r1") -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id, clarification_mode="none",
        answer="查不到：ROE / ACME_CN / 2025（渠道 TOTAL）未在事实表中找到，不提供任何推测数字。",
        refused=True,
    )


# ---------------------------------------------------------------------------
# golden set 文件：存在性、规模、覆盖面、格式
# ---------------------------------------------------------------------------

def test_golden_set_exists_and_loads_with_min_40_cases():
    cases = load_golden_set(GOLDEN_PATH)
    assert len(cases) >= 40
    assert len({c.id for c in cases}) == len(cases)  # id 唯一
    for c in cases:
        assert c.question.strip()
        assert c.reference_date == REF  # 固定参考日期，保证相对期间可复现
        assert set(c.tags) >= {"topic", "scope", "qtype"}


def test_golden_set_covers_required_question_types():
    cases = load_golden_set(GOLDEN_PATH)
    qtypes = {c.tags["qtype"] for c in cases}
    assert {
        "phrasing", "relative_period", "mixed_lang", "synonym",
        "numeric_trap", "period_trap", "channel", "adversarial",
        "should_clarify", "must_not_clarify", "assume_default",
        "narrative", "composite",
    } <= qtypes
    assert {c.case_type for c in cases} == {
        "numeric", "clarification", "refusal", "narrative", "composite"
    }
    # 中英都要有
    zh = [c for c in cases if any("一" <= ch <= "鿿" for ch in c.question)]
    en = [c for c in cases if c.question.isascii()]
    assert len(zh) >= 20
    assert len(en) >= 5
    # 措辞多样性：同一指标（HK REVENUE FY2025=1702）至少 3 种问法
    same_fact = [c for c in cases if c.expected.get("value") == 1702.0]
    assert len(same_fact) >= 3


def _write_golden(tmp_path, records: list[dict]):
    path = tmp_path / "golden.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
        encoding="utf-8",
    )
    return path


_VALID_RECORD = {
    "id": "x1",
    "question": "香港FY2025的REVENUE是多少",
    "case_type": "numeric",
    "expected": {
        "clarification": "none", "refuse": False,
        "value": 1702.0, "unit": "USD_M",
        "source": {"doc": "R25.pptx", "locator": "slide=5"},
    },
    "tags": {"topic": "FIN", "scope": "ACME_HK", "qtype": "phrasing"},
    "reference_date": "2026-06-12",
}


def test_load_rejects_duplicate_ids(tmp_path):
    path = _write_golden(tmp_path, [_VALID_RECORD, _VALID_RECORD])
    with pytest.raises(ValueError):
        load_golden_set(path)


def test_load_rejects_unknown_case_type(tmp_path):
    bad = {**_VALID_RECORD, "case_type": "fancy"}
    with pytest.raises(ValueError):
        load_golden_set(_write_golden(tmp_path, [bad]))


def test_load_rejects_numeric_without_source(tmp_path):
    bad = {**_VALID_RECORD,
           "expected": {"clarification": "none", "refuse": False,
                        "value": 1702.0, "unit": "USD_M"}}
    with pytest.raises(ValueError):
        load_golden_set(_write_golden(tmp_path, [bad]))


def test_load_rejects_missing_clarification_key(tmp_path):
    bad = {**_VALID_RECORD,
           "expected": {"refuse": False, "value": 1702.0, "unit": "USD_M",
                        "source": {"doc": "R25.pptx", "locator": "slide=5"}}}
    with pytest.raises(ValueError):
        load_golden_set(_write_golden(tmp_path, [bad]))


def test_load_rejects_refusal_with_refuse_false(tmp_path):
    bad = {**_VALID_RECORD, "case_type": "refusal",
           "expected": {"clarification": "none", "refuse": False}}
    with pytest.raises(ValueError):
        load_golden_set(_write_golden(tmp_path, [bad]))


# ---------------------------------------------------------------------------
# 命门①：数字准确率（exact match：数值+单位）
# ---------------------------------------------------------------------------

def test_numeric_exact_match_pass():
    case = _numeric_case()
    report = evaluate([case], {"c1": _ok_numeric_outcome()}, mode="tool")
    m = report.metrics[NUMERIC_ACCURACY]
    assert (m.total, m.passed, m.pass_rate) == (1, 1, 1.0)
    assert m.failures == []


def test_numeric_value_mismatch_fails():
    case = _numeric_case()
    outcome = _ok_numeric_outcome()
    outcome.found_value = 170.2  # 数量级错误（8.18b vs 818m 同类陷阱）
    report = evaluate([case], {"c1": outcome}, mode="tool")
    m = report.metrics[NUMERIC_ACCURACY]
    assert (m.total, m.passed) == (1, 0)
    assert m.failures[0]["id"] == "c1"


def test_numeric_unit_mismatch_fails():
    outcome = _ok_numeric_outcome()
    outcome.found_unit = "USD_B"  # 数值对、单位错也必须 fail
    report = evaluate([_numeric_case()], {"c1": outcome}, mode="tool")
    assert report.metrics[NUMERIC_ACCURACY].passed == 0


# ---------------------------------------------------------------------------
# 命门②：citation validity（答案对 + 来源错 = citation fail，数字仍可 pass）
# ---------------------------------------------------------------------------

def test_citation_valid_pass():
    report = evaluate([_numeric_case()], {"c1": _ok_numeric_outcome()}, mode="tool")
    m = report.metrics[CITATION_VALIDITY]
    assert (m.total, m.passed) == (1, 1)


def test_right_answer_wrong_source_fails_citation_only():
    outcome = _ok_numeric_outcome()
    outcome.found_source = {"doc": "WRONG.pptx", "locator": "slide=99"}
    outcome.sources = [outcome.found_source]
    outcome.answer = "ACME_HK FY2025 REVENUE 为 1702 USD_M（来源：WRONG.pptx · slide=99）"
    report = evaluate([_numeric_case()], {"c1": outcome}, mode="tool")
    assert report.metrics[NUMERIC_ACCURACY].passed == 1   # 数字本身是对的
    assert report.metrics[CITATION_VALIDITY].passed == 0  # 来源错 → citation fail
    assert report.metrics[CITATION_VALIDITY].failures[0]["id"] == "c1"


def test_narrative_citation_checks_expected_doc():
    case = GoldenCase(
        id="n1", question="香港最近有什么监管动态", case_type="narrative",
        expected={"clarification": "none", "refuse": False,
                  "narrative_doc": "REG_WATCH_HK.pptx"},
        tags={"topic": "REG", "scope": "ACME_HK", "qtype": "narrative"},
        reference_date=REF,
    )
    ok = CaseOutcome(
        case_id="n1", clarification_mode="none",
        answer="MPFA 新规……（来源：REG_WATCH_HK.pptx REG_WATCH_HK.pptx#para1）",
        sources=[{"doc": "REG_WATCH_HK.pptx", "locator": "REG_WATCH_HK.pptx#para1"}],
    )
    bad = CaseOutcome(
        case_id="n1", clarification_mode="none",
        answer="MPFA 新规……（来源：OTHER.pptx OTHER.pptx#para1）",
        sources=[{"doc": "OTHER.pptx", "locator": "OTHER.pptx#para1"}],
    )
    assert evaluate([case], {"n1": ok}).metrics[CITATION_VALIDITY].passed == 1
    assert evaluate([case], {"n1": bad}).metrics[CITATION_VALIDITY].passed == 0


# ---------------------------------------------------------------------------
# 命门③：refusal appropriateness（两个方向）
# ---------------------------------------------------------------------------

def test_refusal_expected_and_refused_passes():
    report = evaluate([_refusal_case()], {"r1": _ok_refusal_outcome()}, mode="tool")
    m = report.metrics[REFUSAL_APPROPRIATENESS]
    assert (m.total, m.passed) == (1, 1)


def test_refusal_expected_but_answered_fails():
    outcome = CaseOutcome(
        case_id="r1", clarification_mode="none",
        answer="ACME_CN FY2025 ROE 为 9.9 PCT", found_value=9.9, found_unit="PCT",
        refused=False,
    )
    report = evaluate([_refusal_case()], {"r1": outcome}, mode="tool")
    assert report.metrics[REFUSAL_APPROPRIATENESS].passed == 0


def test_should_answer_but_refused_fails():
    """不该拒答时拒答 → refusal 方向二 fail。"""
    outcome = CaseOutcome(case_id="c1", clarification_mode="none",
                          answer="查不到该数据。", refused=True)
    report = evaluate([_numeric_case()], {"c1": outcome}, mode="tool")
    assert report.metrics[REFUSAL_APPROPRIATENESS].passed == 0


def test_should_answer_and_answered_passes():
    report = evaluate([_numeric_case()], {"c1": _ok_numeric_outcome()}, mode="tool")
    assert report.metrics[REFUSAL_APPROPRIATENESS].passed == 1


# ---------------------------------------------------------------------------
# 命门④：clarification appropriateness（两个方向）
# ---------------------------------------------------------------------------

def test_should_clarify_and_asked_first_passes():
    outcome = CaseOutcome(case_id="k1", clarification_mode="ask_first",
                          answer="想查询哪个指标？")
    report = evaluate([_clarify_case()], {"k1": outcome}, mode="tool")
    assert report.metrics[CLARIFICATION_APPROPRIATENESS].passed == 1


def test_should_clarify_but_did_not_fails():
    outcome = CaseOutcome(case_id="k1", clarification_mode="none", answer="……")
    report = evaluate([_clarify_case()], {"k1": outcome}, mode="tool")
    assert report.metrics[CLARIFICATION_APPROPRIATENESS].passed == 0


def test_complete_question_must_not_ask_back():
    """完整问题被反问 → clarification 方向二 fail。"""
    outcome = _ok_numeric_outcome()
    outcome.clarification_mode = "ask_first"
    report = evaluate([_numeric_case()], {"c1": outcome}, mode="tool")
    assert report.metrics[CLARIFICATION_APPROPRIATENESS].passed == 0


def test_answer_with_assumptions_mode_matched():
    case = _clarify_case("k2", mode="answer_with_assumptions")
    ok = CaseOutcome(case_id="k2", clarification_mode="answer_with_assumptions",
                     answer="【假设】……")
    bad = CaseOutcome(case_id="k2", clarification_mode="ask_first", answer="？")
    assert evaluate([case], {"k2": ok}).metrics[CLARIFICATION_APPROPRIATENESS].passed == 1
    assert evaluate([case], {"k2": bad}).metrics[CLARIFICATION_APPROPRIATENESS].passed == 0


# ---------------------------------------------------------------------------
# 编造数字检测（拒答类 case 单列报告，目标 0）
# ---------------------------------------------------------------------------

def test_detect_fabricated_numbers_ignores_period_tokens():
    assert detect_fabricated_numbers(
        "查不到：REVENUE / ACME_HK / 2030（渠道 TOTAL）未在事实表中找到。") == []
    assert detect_fabricated_numbers("查不到 FY2024 与 2025H1 的数据。") == []
    assert detect_fabricated_numbers("查不到 2025Q1 数据，不提供推测数字。") == []


def test_detect_fabricated_numbers_flags_real_numbers():
    assert detect_fabricated_numbers("ROE 为 9999%，表现亮眼。") == ["9999"]
    assert "8.18" in detect_fabricated_numbers("大约 8.18b 左右。")


def test_fabrication_metric_flags_refusal_case_with_number():
    outcome = _ok_refusal_outcome()
    outcome.answer = "查不到，但估计大约是 1234 左右。"
    outcome.refused = True
    report = evaluate([_refusal_case()], {"r1": outcome}, mode="tool")
    assert report.fabrication_count == 1
    assert report.fabrication.failures[0]["id"] == "r1"


def test_fabrication_zero_when_clean():
    report = evaluate([_refusal_case()], {"r1": _ok_refusal_outcome()}, mode="tool")
    assert report.fabrication_count == 0


# ---------------------------------------------------------------------------
# 反编造白名单：字面 byte-pin + call-time 读 _PROFILE（ADR 0004 STEP 11 blocker）
# ---------------------------------------------------------------------------

# 期间白名单字面（年份锚 19xx/20xx 是 '9999' 能被判编造的唯一原因，绝不可丢）。
_PERIOD_LITERAL = (
    r"(?:FY\s*)?(?:19|20)\d{2}\s*年?\s*(?:H\s*[12]|Q\s*[1-4]|上半年|下半年)?"
)


def test_period_token_re_and_profile_regex_are_byte_identical():
    """字面 byte-pin：qa_eval._PERIOD_TOKEN_RE 与默认 profile 的 period 维
    fabrication_whitelist_regex 必须与该字面逐字节相等（绝不被重构/派生改动）。"""
    assert qa_eval_mod._PERIOD_TOKEN_RE.pattern == _PERIOD_LITERAL
    by_name = {d.name: d for d in qa_eval_mod._PROFILE.dimensions}
    assert by_name["period"].fabrication_whitelist_regex == _PERIOD_LITERAL
    # 两者 byte-for-byte 相等（同一字面源）。
    assert (
        by_name["period"].fabrication_whitelist_regex
        == qa_eval_mod._PERIOD_TOKEN_RE.pattern
    )


def test_detect_reads_profile_at_call_time_no_temporal_dim_flags_period_digits(
    monkeypatch,
):
    """call-time 契约回归：换上无 temporal 维的 profile 后，detect_fabricated_numbers
    必须把期间形数字（如 '2024'）也标记为编造——证明 detect 调用期读 _PROFILE，
    且无 temporal 维则不剥离任何数字（period 白名单从未被放宽的最强证明）。"""
    no_temporal = DomainProfile(
        home_company_name="Lab",
        home_entity_code="LAB_SITE",
        dimensions=(
            DimensionSpec("measurement", "测量", kind="measure"),
            DimensionSpec("batch", "批次", kind="categorical"),
        ),
    )
    monkeypatch.setattr(qa_eval_mod, "_PROFILE", no_temporal, raising=False)
    assert qa_eval_mod._fabrication_whitelist_re() is None
    assert "2024" in detect_fabricated_numbers("查不到 2024 数据。")


# ---------------------------------------------------------------------------
# 报告结构：分层细分 + JSON 可序列化
# ---------------------------------------------------------------------------

def test_report_by_tag_breakdown():
    trap = _numeric_case("c2")
    trap.tags = {"topic": "FIN", "scope": "ACME_GROUP", "qtype": "numeric_trap"}
    outcomes = {"c1": _ok_numeric_outcome("c1"), "c2": _ok_numeric_outcome("c2")}
    outcomes["c2"].found_value = 8.18  # 陷阱题答错
    report = evaluate([_numeric_case("c1"), trap], outcomes, mode="tool")
    by_qtype = report.metrics[NUMERIC_ACCURACY].by_tag["qtype"]
    assert by_qtype["numeric_trap"] == {"total": 1, "passed": 0}
    assert by_qtype["phrasing"] == {"total": 1, "passed": 1}


def test_report_is_json_serializable():
    report = evaluate(
        [_numeric_case(), _refusal_case()],
        {"c1": _ok_numeric_outcome(), "r1": _ok_refusal_outcome()}, mode="tool",
    )
    data = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
    assert data["mode"] == "tool"
    assert data["n_cases"] == 2
    assert set(data["metrics"]) == set(GATE_METRICS)
    assert data["fabrication"]["name"] == FABRICATION
    for m in data["metrics"].values():
        assert {"name", "total", "passed", "pass_rate", "failures", "by_tag"} <= set(m)


# ---------------------------------------------------------------------------
# 基线门禁：任一命门指标退化即 gate fail
# ---------------------------------------------------------------------------

def _perfect_report():
    return evaluate(
        [_numeric_case(), _refusal_case(), _clarify_case()],
        {"c1": _ok_numeric_outcome(), "r1": _ok_refusal_outcome(),
         "k1": CaseOutcome(case_id="k1", clarification_mode="ask_first", answer="？")},
        mode="tool",
    )


def _perfect_baseline() -> dict:
    return {
        "metrics": {
            NUMERIC_ACCURACY: 1.0,
            CITATION_VALIDITY: 1.0,
            REFUSAL_APPROPRIATENESS: 1.0,
            CLARIFICATION_APPROPRIATENESS: 1.0,
        },
        "fabrication_count": 0,
    }


def test_baseline_equal_passes():
    cmp = compare_to_baseline(_perfect_report(), _perfect_baseline())
    assert cmp.passed
    assert cmp.regressions == []


def test_baseline_single_gate_regression_blocks():
    outcome = _ok_numeric_outcome()
    outcome.found_source = {"doc": "WRONG.pptx", "locator": "x"}
    outcome.sources = [outcome.found_source]
    outcome.answer = "1702 USD_M（来源：WRONG.pptx）"
    report = evaluate(
        [_numeric_case(), _refusal_case()],
        {"c1": outcome, "r1": _ok_refusal_outcome()}, mode="tool",
    )
    cmp = compare_to_baseline(report, _perfect_baseline())
    assert not cmp.passed
    assert [r["metric"] for r in cmp.regressions] == [CITATION_VALIDITY]
    assert cmp.regressions[0]["current"] == 0.0
    assert cmp.regressions[0]["baseline"] == 1.0


def test_baseline_fabrication_increase_blocks():
    outcome = _ok_refusal_outcome()
    outcome.answer = "查不到，估计 1234。"
    report = evaluate([_refusal_case()], {"r1": outcome}, mode="tool")
    cmp = compare_to_baseline(report, _perfect_baseline())
    assert not cmp.passed
    assert FABRICATION in [r["metric"] for r in cmp.regressions]


# ---------------------------------------------------------------------------
# 合成 KB 构建器：确定性 + 幂等 + 与 golden set 对齐
# ---------------------------------------------------------------------------

def test_build_eval_kb_deterministic_and_idempotent(tmp_path):
    fact_db, chunk_db = build_eval_kb(tmp_path)
    fs = FactStore(fact_db)
    cs = ChunkStore(chunk_db)
    try:
        n_facts, n_chunks = fs.count(), cs.count()
        assert n_facts > 0 and n_chunks > 0
        hits = fs.query("REVENUE", "ACME_HK", "FY", "2025", "TOTAL")
        assert len(hits) == 1 and hits[0].value == 1702.0
    finally:
        fs.close()
        cs.close()
    # 幂等重建：活跃数据集不变
    build_eval_kb(tmp_path)
    fs2, cs2 = FactStore(fact_db), ChunkStore(chunk_db)
    try:
        assert fs2.count() == n_facts
        assert cs2.count() == n_chunks
    finally:
        fs2.close()
        cs2.close()


# ---------------------------------------------------------------------------
# 端到端：双模式整套 golden set 全指标满分（KB 与 golden 严格对齐的自证）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["tool", "agent"])
def test_e2e_full_golden_all_gates_perfect(tmp_path, mode):
    report = run_qa_eval(GOLDEN_PATH, mode=mode, kb_dir=tmp_path)
    assert report.mode == mode
    assert report.n_cases >= 40
    for name in GATE_METRICS:
        m = report.metrics[name]
        assert m.total > 0, f"{name} 没有任何样本"
        assert m.pass_rate == 1.0, f"{name} 失败明细：{m.failures}"
    assert report.fabrication_count == 0, report.fabrication.failures


def test_run_qa_eval_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError):
        run_qa_eval(GOLDEN_PATH, mode="yolo", kb_dir=tmp_path)


# ---------------------------------------------------------------------------
# CLI：首跑生成基线 → 复跑过门禁 → 篡改基线触发退化拦截 → --update-baseline 重置
# ---------------------------------------------------------------------------

def test_cli_first_run_creates_baseline_then_passes(tmp_path, capsys):
    baseline = tmp_path / "qa_baseline.json"
    report_path = tmp_path / "report.json"
    rc = qa_eval_main([
        "--mode", "tool",
        "--baseline", str(baseline),
        "--report", str(report_path),
    ])
    assert rc == 0
    data = json.loads(baseline.read_text(encoding="utf-8"))
    assert "tool" in data
    assert data["tool"]["metrics"][NUMERIC_ACCURACY] == 1.0
    rep = json.loads(report_path.read_text(encoding="utf-8"))
    assert rep["mode"] == "tool"
    capsys.readouterr()
    # 第二次：与基线比较，全部达标
    assert qa_eval_main(["--mode", "tool", "--baseline", str(baseline)]) == 0


def test_cli_gate_fails_on_baseline_regression(tmp_path, capsys):
    baseline = tmp_path / "qa_baseline.json"
    impossible = {
        "tool": {
            "metrics": {
                NUMERIC_ACCURACY: 2.0,  # 不可能达到 → 必触发退化拦截
                CITATION_VALIDITY: 1.0,
                REFUSAL_APPROPRIATENESS: 1.0,
                CLARIFICATION_APPROPRIATENESS: 1.0,
            },
            "fabrication_count": 0,
        }
    }
    baseline.write_text(json.dumps(impossible), encoding="utf-8")
    rc = qa_eval_main(["--mode", "tool", "--baseline", str(baseline)])
    assert rc == 1
    # --update-baseline：显式重置基线为当前结果 → 过
    assert qa_eval_main(["--mode", "tool", "--baseline", str(baseline),
                         "--update-baseline"]) == 0
    data = json.loads(baseline.read_text(encoding="utf-8"))
    assert data["tool"]["metrics"][NUMERIC_ACCURACY] == 1.0


def test_cli_agent_mode_runs(tmp_path):
    baseline = tmp_path / "qa_baseline.json"
    rc = qa_eval_main(["--mode", "agent", "--baseline", str(baseline)])
    assert rc == 0
    data = json.loads(baseline.read_text(encoding="utf-8"))
    assert "agent" in data
