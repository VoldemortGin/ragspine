"""nl-gold 判分器 v2 + gold v2 + 重复运行 + 重判（nl_gold_ragspine）。

被测规格：
- 拒答：编排层确定性文案（行首“查不到”等）位置不限；LLM 措辞只看答案开头（标题 + 首句），
  开头是“猜测式作答”（most likely / 很可能…）不算拒答；正文里的“未披露”等补充说明不算拒答。
- 数字单位：带币种的“亿 / billion / bn / b”金额在同一数量级内换算成百万（不补零、不丢精度）。
- 跨语言引用：quote 的关键英文片段全部出现在答案的一个窄窗口内即算命中（放宽，单独计数）。
- 重复运行：每题通过率、总体均值±标准差、不稳定题；不做多数票。
- gold v2：schema ``nl-answers-gold-v2`` 须带变更日志；报告 meta 记录判分器版本与 gold 版本。
"""

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.eval.nl_gold_ragspine import (
    JUDGE_VERSION,
    ROUTE_FORCED_NARRATIVE,
    CaseRun,
    ClaimAnchor,
    GoldCase,
    GoldFormatError,
    contains_normalized,
    gold_version,
    is_refusal,
    judge_case,
    load_nl_gold,
    normalize_answer,
    rejudge_report,
    repeat_stats,
    summarize,
    write_report,
)

BENCH = ROOT_DIR / "data/benchmarks/enterprise-pdf-rag/aia-2026-interim"
GOLD_V1 = BENCH / "nl-answers-gold-v1.json"
GOLD_V2 = BENCH / "nl-answers-gold-v2.json"
DI_MARKDOWN = ROOT_DIR / "data/di-markdown/aia-group-2026-interim-results-presentation.md"

# ---------------------------------------------------------------------------
# 拒答识别：正例（真拒答）与反例（真作答）
# ---------------------------------------------------------------------------

REFUSALS = [
    # 编排层确定性文案（ADR 0023 缺指标回落无依据）
    "查不到：资料中没有能回答该问题的依据，不提供任何推测数字。想查询哪个指标？目前支持：REVENUE、ROE。",
    "【假设】实体默认按 X 口径（如需收窄：改查 Y）\n查不到：ROE / X / 2026H1（渠道 TOTAL）未在事实表中找到。",
    # LLM 措辞（开头即结论）
    "**The snippets do not explicitly state which stage follows Foundation.**\n\n"
    "- The pathway's first stage is **Foundation: 100% Digitalised Agency** [2].",
    "**Answer:** The retrieved snippets do not explicitly state which stage comes after "
    '"Foundation."\n\n**What the snippets do show:**\n- Page 6 lists Foundation.',
    "**The retrieved excerpts contain no forecast of AIA's full-year 2027 VONB.** The materials "
    "report first-half 2026 figures such as $3,212m only.",
    "**None of the excerpts or images I was given include a VONB forecast for full-year 2027.** "
    "Everything provided is 1H 2026 results.",
    "**The provided excerpts do not say how many tied agents AIA has in Vietnam.**\n\n"
    "The excerpts mention Vietnam only as one of 18 markets.",
    "# 结论：片段中没有 AIA 2027 全年 VONB 预测\n\n检索片段均来自 2026 年中期业绩演示，1H26 VONB 为 $3,212m。",
    "# Foundation 之后的阶段\n\n**片段无法明确确定 Foundation 之后是哪个阶段。**\n\n## 片段中的信息\n- 第 6 页",
    "**所提供的片段中没有 AIA 越南专属代理人（tied agents）数量的信息。**\n\n片段中提到越南的只有两处。",
    "**答案：**\n片段未说明 Foundation 之后的阶段。",
]

ANSWERS = [
    # 正文表格里的“未披露”只是补充说明，开头已作答
    "# 代理人渠道占 VONB 比重（1H26）\n\n**集团整体：代理人渠道占 VONB 的 72%，合作伙伴渠道占 28%。**\n\n"
    "| 市场 | Agency | Partnership |\n|---|---|---|\n| 友邦中国 | 87% | 未披露 |",
    "**1H26 的中期股息为每股 53.90 港仙，同比增长 10%。**\n\n注：片段未提供全年股息数据。",
    "The stage after Foundation is Growth. The snippets do not state a timeline for it.",
    # 猜测式作答不是拒答（abstain 题应判失败）
    "Based on the page-6 slide, **Growth: Data-Driven Lead Generation** most likely comes after "
    "Foundation. However, the fragments don't explicitly call this a sequential pathway.",
    "**根据片段，Foundation 之后的阶段很可能是 Growth，但片段没有明确说明各阶段的先后顺序，因此这只是推断。**",
    # 开头括号里的附注不是拒答
    "根据片段，代理人科技投入分为以下三个层次（片段未明确称其为“阶段”，但按递进顺序排列）：\n\n"
    "1. **基础（Foundation）：100% 数字化代理人（100% Digitalised Agency）**",
    # 组合查询的单个子项查不到，不是整题拒答
    "- REVENUE：120（来源 fact#1）\n- PROFIT：查不到（未在事实表中找到，不提供推测数字）",
]


@pytest.mark.parametrize("answer", REFUSALS)
def test_v2_refusals_are_recognized(answer: str) -> None:
    assert is_refusal(answer)


@pytest.mark.parametrize("answer", ANSWERS)
def test_v2_real_answers_are_not_refusals(answer: str) -> None:
    assert not is_refusal(answer)


# ---------------------------------------------------------------------------
# 数字单位归一（normalize_answer / contains_normalized 签名不变）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        ("1H26 VONB 为 5.14 亿美元", "514"),
        ("VONB was US$514 million", "514"),
        ("VONB 为 514百万美元", "514"),
        ("VONB was US$3.212 billion", "3212"),
        ("Hong Kong VONB was $1.168b", "1168"),
        ("约 5.145 亿美元", "514.5"),
        ("5.14 亿港元", "514"),
    ],
)
def test_scaled_currency_amounts_match_millions(answer: str, needle: str) -> None:
    assert contains_normalized(normalize_answer(answer), needle)


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        ("VONB 为 5.14 亿美元", "514%"),  # 单位不同：百分比不匹配金额
        ("Margin was 5.14%", "514"),  # 没有数量级词，不换算
        ("Growth of 5.14", "514"),
        ("约 5.1 亿美元", "510"),  # 需要补零 → 精度不够，不换算
        ("VONB was $3.2b", "3200"),
        ("VONB was US$5.14 billion", "5140"),
        ("约 5.145 亿美元", "514"),  # 换算结果 514.5 不能冒充 514
        ("客户 5.14 亿人", "514"),  # 无币种不换算
        ("margin of 1.5bps", "1500"),
    ],
)
def test_scaled_amounts_do_not_overmatch(answer: str, needle: str) -> None:
    assert not contains_normalized(normalize_answer(answer), needle)


def test_normalize_keeps_the_original_scaled_text() -> None:
    normalized = normalize_answer("VONB 为 5.14 亿美元")
    assert contains_normalized(normalized, "5.14 亿美元")
    assert contains_normalized(normalized, "5.14")


def test_value_claim_hits_through_scaled_amount() -> None:
    case = GoldCase(
        case_id="k",
        case_class="positive",
        questions=(("zh", "泰国 1H26 VONB"),),
        required_claims=(
            (ClaimAnchor(kind="chart_value", page_index=12, value="514", unit="$m"),),
        ),
    )
    judgement = judge_case(case, answer="1H26 VONB 为 5.14 亿美元。", locators=["d@page=13#p1"])
    assert judgement.passed


# ---------------------------------------------------------------------------
# 跨语言引用：关键英文片段（放宽，单独计数）
# ---------------------------------------------------------------------------


def _diagram_case() -> GoldCase:
    return GoldCase(
        case_id="p-diagram",
        case_class="positive",
        questions=(("zh", "三个阶段"),),
        required_claims=(
            (
                ClaimAnchor(
                    kind="diagram_node", page_index=5, quote="Foundation: 100% Digitalised Agency"
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    "answer",
    [
        "1. **基础（Foundation）**：100% 数字化代理人（100% Digitalised Agency）",
        "| **1. Foundation（基础）** | 100% Digitalised Agency：代理人渠道全面数字化 |",
        "1. **Foundation:** a 100% digitalised agency.",
    ],
)
def test_fragment_rule_hits_cross_lingual_quote(answer: str) -> None:
    judgement = judge_case(_diagram_case(), answer=answer, locators=["d@page=6#p1"])
    assert judgement.passed
    claim = judgement.claims[0]
    assert claim.content_hit and claim.fragment_hit


def test_exact_quote_is_not_counted_as_fragment_hit() -> None:
    judgement = judge_case(
        _diagram_case(), answer="Foundation: 100% Digitalised Agency", locators=["d@page=6#p1"]
    )
    assert judgement.passed and not judgement.claims[0].fragment_hit


@pytest.mark.parametrize(
    "answer",
    [
        "基础（Foundation）：100% 数字化代理人",  # 缺关键片段 digitalised / agency
        "1. **基础（Foundation）**：数字化代理人（Digitalised Agency）",  # 缺 100%
        "Foundation 是第一阶段。"
        + "其他内容与本题无关，" * 20
        + "100% Digitalised Agency 另见附录。",
        "Foundation: 1000% Digitalised Agency",  # 数字边界
    ],
)
def test_fragment_rule_misses(answer: str) -> None:
    judgement = judge_case(_diagram_case(), answer=answer, locators=["d@page=6#p1"])
    assert not judgement.claims[0].content_hit


def test_fragment_rule_never_applies_to_short_quotes() -> None:
    # 少于 3 个关键片段（如 "72%"、"record ROE"）只走逐字匹配。
    case = GoldCase(
        case_id="p",
        case_class="positive",
        questions=(("en", "q"),),
        required_claims=((ClaimAnchor(kind="quote", page_index=0, quote="Digitalised Agency"),),),
    )
    judgement = judge_case(case, answer="Agency is digitalised", locators=["d@page=1#p"])
    assert not judgement.passed


def test_summary_counts_fragment_hits_separately() -> None:
    case = _diagram_case()
    runs = [
        _run(case, "1. **基础（Foundation）**：100% 数字化代理人（100% Digitalised Agency）"),
        _run(case, "Foundation: 100% Digitalised Agency"),
    ]
    claims = summarize(runs)["claims"]
    assert claims["satisfied"] == 2
    assert claims["fragment_hits"] == 1
    assert claims["cases_passed_via_fragment"] == 1


# ---------------------------------------------------------------------------
# 重复运行
# ---------------------------------------------------------------------------


def _run(
    case: GoldCase,
    answer: str,
    *,
    repeat: int = 0,
    language: str = "zh",
    route_label: str = "narrative",
    locators: tuple[str, ...] = ("d@page=6#p1",),
) -> CaseRun:
    return CaseRun(
        route=ROUTE_FORCED_NARRATIVE,
        case_id=case.case_id,
        case_class=case.case_class,
        language=language,
        question="q",
        answer=answer,
        answer_plain=answer,
        agent_route="narrative",
        route_label=route_label,
        sources=[{"doc": "d", "locator": loc} for loc in locators],
        retrieved_locators=list(locators),
        judgement=judge_case(case, answer=answer, locators=list(locators)),
        llm_calls=1,
        seconds=0.1,
        repeat=repeat,
    )


def _repeat_runs() -> list[CaseRun]:
    stable = _diagram_case()
    flaky = GoldCase(
        case_id="a-flaky",
        case_class="abstain",
        questions=(("en", "q"),),
        expect_abstain=True,
    )
    good = "Foundation: 100% Digitalised Agency"
    return [
        _run(stable, good, repeat=0),
        _run(stable, good, repeat=1),
        _run(stable, good, repeat=2),
        _run(
            flaky,
            "查不到：资料中没有能回答该问题的依据。",
            repeat=0,
            language="en",
            route_label="not_found",
        ),
        _run(flaky, "It is Growth.", repeat=1, language="en"),
        _run(flaky, "It is Growth.", repeat=2, language="en"),
    ]


def test_repeat_stats_reports_mean_std_and_unstable_cases() -> None:
    stats = repeat_stats(_repeat_runs())
    assert stats["repeats"] == 3
    assert stats["main_passed"] == [2, 1, 1]
    assert stats["main_rates"] == [1.0, 0.5, 0.5]
    assert stats["mean"] == pytest.approx(2 / 3, abs=1e-4)
    assert stats["std"] == pytest.approx(0.2887, abs=1e-4)
    per_case = {(c["case_id"], c["language"]): c for c in stats["per_case"]}
    assert per_case[("p-diagram", "zh")]["passed"] == 3
    assert per_case[("a-flaky", "en")] | {"route_labels": None} == {
        "case_id": "a-flaky",
        "language": "en",
        "case_class": "abstain",
        "passed": 1,
        "total": 3,
        "rate": 0.3333,
        "route_labels": None,
    }
    assert [(c["case_id"], c["passed"], c["total"]) for c in stats["unstable"]] == [
        ("a-flaky", 1, 3)
    ]


def test_repeat_summary_counts_every_run_without_majority_vote() -> None:
    summary = summarize(_repeat_runs())
    # 主分按全部运行次数计（6 次里 4 次通过），不做多数票（多数票会得 1/2 题）。
    assert summary["main"] == {"passed": 4, "total": 6, "rate": 0.6667}
    assert summary["route_distribution"] == {"narrative": 5, "not_found": 1}
    assert summary["repeat"]["unstable"][0]["case_id"] == "a-flaky"


def test_single_repeat_has_zero_std() -> None:
    stats = repeat_stats([r for r in _repeat_runs() if r.repeat == 0])
    assert stats["repeats"] == 1 and stats["std"] == 0.0 and stats["unstable"] == []


def test_report_with_repeats_writes_every_run_and_stability(tmp_path: Path) -> None:
    runs = _repeat_runs()
    cases = [_diagram_case(), GoldCase("a-flaky", "abstain", (("en", "q"),), expect_abstain=True)]
    out = write_report(
        tmp_path / "r", meta={"label": "t"}, cases=cases, runs={ROUTE_FORCED_NARRATIVE: runs}
    )
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["meta"]["judge_version"] == JUDGE_VERSION
    stats = report["routes"][ROUTE_FORCED_NARRATIVE]["repeat"]
    assert stats["repeats"] == 3 and len(stats["unstable"]) == 1
    names = sorted(p.name for p in (out / "cases" / ROUTE_FORCED_NARRATIVE).iterdir())
    assert names == [
        "a-flaky-en-r1.json",
        "a-flaky-en-r2.json",
        "a-flaky-en-r3.json",
        "p-diagram-zh-r1.json",
        "p-diagram-zh-r2.json",
        "p-diagram-zh-r3.json",
    ]
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "Repeat stability" in md and "a-flaky" in md and "±" in md
    assert "fragment" in md


# ---------------------------------------------------------------------------
# 重判：已有报告的答案用当前判分器重新判定（不重新生成）
# ---------------------------------------------------------------------------


def test_rejudge_report_rescores_recorded_answers(tmp_path: Path) -> None:
    case = _diagram_case()
    run = _run(case, "1. **基础（Foundation）**：100% 数字化代理人（100% Digitalised Agency）")
    stale = CaseRun(**{**run.__dict__, "judgement": run.judgement.__class__(False, "old", False)})
    out = write_report(
        tmp_path / "old",
        meta={"label": "old"},
        cases=[case],
        runs={ROUTE_FORCED_NARRATIVE: [stale]},
    )
    rejudged = rejudge_report(out / "report.json", [case])
    assert rejudged[ROUTE_FORCED_NARRATIVE][0].judgement.passed
    assert rejudged[ROUTE_FORCED_NARRATIVE][0].answer_plain == run.answer_plain


def test_rejudge_takes_case_class_from_the_new_gold(tmp_path: Path) -> None:
    old_case = GoldCase("a04", "abstain", (("en", "dividend?"),), expect_abstain=True)
    new_case = GoldCase(
        "a04",
        "positive",
        (("en", "dividend?"),),
        required_claims=((ClaimAnchor(kind="quote", page_index=16, value="53.90"),),),
    )
    run = _run(old_case, "It was 53.90 HK cents.", locators=("d@page=17#p1",), language="en")
    assert not run.judgement.passed
    out = write_report(tmp_path / "old", meta={"label": "old"}, cases=[old_case], runs={"B": [run]})
    rejudged = rejudge_report(out / "report.json", [new_case])["B"][0]
    assert rejudged.case_class == "positive" and rejudged.judgement.passed


# ---------------------------------------------------------------------------
# gold v2
# ---------------------------------------------------------------------------


def _v2_payload(tmp_path: Path, **overrides: object) -> Path:
    payload = json.loads(GOLD_V2.read_text(encoding="utf-8"))
    payload.update(overrides)
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_v2_requires_a_changelog(tmp_path: Path) -> None:
    with pytest.raises(GoldFormatError, match="changelog"):
        load_nl_gold(_v2_payload(tmp_path, changelog=[]))
    entry = {"case_id": "nope", "change": "x", "old": 1, "new": 2, "evidence": "y"}
    with pytest.raises(GoldFormatError, match="nope"):
        load_nl_gold(_v2_payload(tmp_path, changelog=[entry]))
    with pytest.raises(GoldFormatError, match="evidence"):
        load_nl_gold(
            _v2_payload(tmp_path, changelog=[{"case_id": "k01-region-thailand-en", "change": "x"}])
        )


def test_gold_version_is_reported() -> None:
    assert gold_version(GOLD_V1) == "nl-answers-gold-v1"
    assert gold_version(GOLD_V2) == "nl-answers-gold-v2"


def test_v1_stays_frozen() -> None:
    cases = {c.case_id: c for c in load_nl_gold(GOLD_V1)}
    assert cases["a04-beyond-selected-pages-en"].case_class == "abstain"
    assert [a.page_index for a in cases["k01-region-thailand-en"].required_claims[0]] == [12]


def test_v2_fixes_a04_and_thailand_pages() -> None:
    cases = {c.case_id: c for c in load_nl_gold(GOLD_V2)}
    a04 = cases["a04-beyond-selected-pages-en"]
    assert a04.case_class == "positive" and not a04.expect_abstain
    assert {a.page_index + 1 for a in a04.required_claims[0]} >= {25, 17}
    for case_id in ("k01-region-thailand-en", "k02-region-thailand-zh"):
        pages = {a.page_index + 1 for a in cases[case_id].required_claims[0]}
        assert pages >= {13, 32, 39}
    raw = json.loads(GOLD_V2.read_text(encoding="utf-8"))
    changed = {entry["case_id"] for entry in raw["changelog"]}
    assert {
        "a04-beyond-selected-pages-en",
        "k01-region-thailand-en",
        "k02-region-thailand-zh",
    } <= changed
    for entry in raw["changelog"]:
        assert entry["evidence"] and "old" in entry and "new" in entry


def _di_pages() -> list[str]:
    text = DI_MARKDOWN.read_text(encoding="utf-8")
    return re.split(r"<!--\s*PageBreak\s*-->", text)


@pytest.mark.skipif(not DI_MARKDOWN.exists(), reason="DI markdown of the AIA sample not present")
def test_v2_anchors_are_printed_on_their_pages() -> None:
    """v2 的每个锚点：value / quote 在规范化后确实出现在所标的物理页上。"""
    pages = [normalize_answer(p) for p in _di_pages()]
    for case in load_nl_gold(GOLD_V2):
        for group in case.required_claims:
            for anchor in group:
                page = pages[anchor.page_index]
                needle = anchor.quote or anchor.text or anchor.value
                assert needle and contains_normalized(page, needle), (case.case_id, anchor)


# ---------------------------------------------------------------------------
# 脚本参数：主口径全文、重复次数
# ---------------------------------------------------------------------------


def _script():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location(
        "run_nl_gold_ragspine", ROOT_DIR / "scripts/run_nl_gold_ragspine.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_defaults_to_full_document_and_single_repeat() -> None:
    script = _script()
    args = script._parse_args([])
    assert args.pages == "all" and args.repeat == 1
    assert args.gold.name == "nl-answers-gold-v2.json"
    assert script._parse_args(["--pages", "gold", "--repeat", "3"]).repeat == 3
    with pytest.raises(SystemExit):
        script._parse_args(["--repeat", "0"])


def test_v2_is_registered_in_the_benchmark_manifest() -> None:
    registry = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))["gold_sets"]
    entry = next(item for item in registry["sets"] if item["file"] == GOLD_V2.name)
    raw = json.loads(GOLD_V2.read_text(encoding="utf-8"))
    assert entry["schema_version"] == raw["schema_version"] == "nl-answers-gold-v2"
    assert entry["case_count"] == len(raw["cases"])
    assert entry["pinned_document_sha256"] == raw["pinned"]["document_sha256"]
