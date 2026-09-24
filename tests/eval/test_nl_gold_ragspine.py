"""ragspine 主链路 nl-gold 评测（nl_gold_ragspine）：判定规则 + 两路编排 + 报告落盘。

全部离线：MockProvider + 测试内的小 gold 文件 + 小 DI markdown，零模型零网络。
判定口径（被测规格）：
- positive：每条 required claim（any_of 任一锚点）要求 答案含 quote/value（规范化后）且
  sources 有 locator 命中 ``@page={page_index+1}#``；内容/页码分开统计。
- abstain：答案须为拒答 / 未找到（agent 文案 + LLM 拒答措辞），且不含 forbidden number。
- known-gap：照跑、单列，不计主分；adversarial：跳过并记录原因。
"""

import json
import logging
import os
from datetime import date
from pathlib import Path

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import AgentResult
from ragspine.agent.intent import (
    CLARIFY_ASK_FIRST,
    ROUTE_COMPOSITE,
    ROUTE_NARRATIVE,
    ROUTE_STRUCTURED,
    ClarificationResult,
)
from ragspine.agent.llm_provider import MockProvider
from ragspine.common.observability.trace import TRACE_LOGGER_NAME
from ragspine.eval.nl_gold_ragspine import (
    ROUTE_ASK,
    ROUTE_FORCED_NARRATIVE,
    CaseRun,
    ClaimAnchor,
    CountingProvider,
    ForcedNarrativeIntentParser,
    GoldCase,
    Judgement,
    RecordingRetriever,
    contains_normalized,
    gold_selected_pages,
    is_refusal,
    judge_case,
    load_nl_gold,
    normalize_answer,
    recall_at_k,
    route_label,
    run_route,
    select_di_pages,
    summarize,
    write_report,
)
from ragspine.retrieval.link.narrative_link import build_narrative_retriever
from ragspine.retrieval.vector.embedding_backends import DeterministicEmbeddingBackend
from ragspine.service.config import ServiceConfig, open_vector_channel
from ragspine.session import RAGSpine
from ragspine.storage.fact_store import SqliteFactStore

SHA = "a" * 64


def _anchor(
    page_index: int,
    *,
    quote: str | None = None,
    value: str | None = None,
    unit: str | None = None,
    kind: str = "quote",
) -> dict[str, object]:
    anchor: dict[str, object] = {
        "kind": kind,
        "page_index": page_index,
        "field_path": f"fragments.span-{page_index}",
    }
    if quote is not None:
        anchor["quote"] = quote
        anchor["text"] = quote
    if value is not None:
        anchor["value"] = value
    if unit is not None:
        anchor["unit"] = unit
    return anchor


def _case(
    case_id: str,
    case_class: str,
    question: dict[str, str],
    expected: dict[str, object],
    **extra: object,
) -> dict[str, object]:
    case: dict[str, object] = {
        "case_id": case_id,
        "case_class": case_class,
        "question": question,
        "document_sha256": SHA,
        "expected": expected,
        "rationale": "test case",
    }
    case.update(extra)
    return case


def _gold_payload() -> dict[str, object]:
    positive_any_of = _case(
        "p-roe",
        "positive",
        {"en": "What was the record ROE?"},
        {
            "status": "answered",
            "min_claims": 1,
            "required_claims": [
                {
                    "any_of": [
                        _anchor(0, quote="record ROE of 17.5%"),
                        _anchor(2, quote="17.5%", value="17.5", unit="%", kind="chart_value"),
                    ]
                }
            ],
        },
    )
    positive_two = _case(
        "p-mix-zh",
        "positive",
        {"zh": "Agency 和 Partnerships 的 VONB 占比"},
        {
            "status": "answered",
            "min_claims": 2,
            "required_claims": [
                _anchor(1, quote="72%", value="72", unit="%", kind="chart_value"),
                _anchor(1, quote="28%", value="28", unit="%", kind="chart_value"),
            ],
        },
    )
    grounded = _case(
        "p-grounded",
        "positive",
        {"en": "What was growth?"},
        {"status": "answered", "min_claims": 1, "grounded_only": True},
    )
    abstain = _case(
        "a-forecast",
        "abstain",
        {"en": "What is the 2030 forecast?"},
        {"status": "abstained", "abstain_reason": "model_declined", "forbidden_numbers": ["44"]},
    )
    gap = _case(
        "k-gap",
        "positive",
        {"en": "Thailand VONB"},
        {
            "status": "answered",
            "min_claims": 1,
            "required_claims": [_anchor(1, value="514", unit="$m", kind="chart_value")],
            "known_gap": True,
            "known_gap_detail": "column binding",
        },
    )
    adversarial = _case(
        "x-probe",
        "adversarial",
        {"en": "What was the record ROE?"},
        {"status": "abstained", "abstain_reason": "claim_not_in_evidence"},
        offline_only=True,
        model_output={
            "answer": "ROE was 18%",
            "claims": [
                {
                    "claim_id": "c1",
                    "kind": "quote",
                    "page_index": 0,
                    "field_path": "fragments.span-0",
                    "text": "18%",
                }
            ],
        },
    )
    return {
        "schema_version": "nl-answers-gold-v1",
        "corpus": "fictional test corpus",
        "review": ["test"],
        "pinned": {
            "document_sha256": SHA,
            "processing_id": "b" * 64,
            "snapshot_id": "c" * 64,
            "selected_physical_pages": [1, 2, 3],
            "member_count": 3,
            "embedding_fingerprint": "test",
        },
        "minimum_positive_cases": 1,
        "cases": [positive_any_of, positive_two, grounded, abstain, gap, adversarial],
    }


@pytest.fixture
def gold_path(tmp_path: Path) -> Path:
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(_gold_payload(), ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def cases(gold_path: Path) -> dict[str, GoldCase]:
    return {case.case_id: case for case in load_nl_gold(gold_path)}


# ---------------------------------------------------------------------------
# gold 解析
# ---------------------------------------------------------------------------


def test_load_classifies_cases(cases: dict[str, GoldCase]) -> None:
    assert {cid: c.case_class for cid, c in cases.items()} == {
        "p-roe": "positive",
        "p-mix-zh": "positive",
        "p-grounded": "positive",
        "a-forecast": "abstain",
        "k-gap": "known-gap",
        "x-probe": "adversarial",
    }
    assert cases["x-probe"].skip_reason and "adversarial" in cases["x-probe"].skip_reason
    assert cases["k-gap"].known_gap_detail == "column binding"
    assert cases["p-mix-zh"].questions == (("zh", "Agency 和 Partnerships 的 VONB 占比"),)
    assert len(cases["p-roe"].required_claims) == 1
    assert [a.page_index for a in cases["p-roe"].required_claims[0]] == [0, 2]
    assert cases["p-grounded"].grounded_only
    assert cases["a-forecast"].forbidden_numbers == ("44",)


def test_load_rejects_malformed_gold(tmp_path: Path) -> None:
    payload = _gold_payload()
    payload["cases"][0]["case_class"] = "bogus"  # type: ignore[index]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_nl_gold(path)


# ---------------------------------------------------------------------------
# 规范化与内容匹配
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        ("Revenue was 1,234 million", "1234"),
        ("Revenue was 1234 million", "1,234"),
        ("Share was 12 %", "12%"),
        ("Share was 12 per cent", "12%"),
        ("RECORD   operating\nROE of 17.5%", "record Operating ROE of 17.5%"),
        ("It said “Digitalised Agency”", '"Digitalised Agency"'),
        ("**Foundation** — 100% Digitalised Agency", "Foundation: 100% Digitalised Agency"),
        ("代理人渠道占７２％", "72%"),
    ],
)
def test_contains_normalized_tolerates_format(answer: str, needle: str) -> None:
    assert contains_normalized(normalize_answer(answer), needle)


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        ("Share was 172%", "72%"),
        ("Share was 72.5%", "72%"),
        ("ROE of 17.55%", "17.5%"),
        ("Growth: Data-Driven", "Foundation"),
    ],
)
def test_contains_normalized_respects_number_boundaries(answer: str, needle: str) -> None:
    assert not contains_normalized(normalize_answer(answer), needle)


def test_citation_locators_are_not_content(cases: dict[str, GoldCase]) -> None:
    # 答案里的来源回指 "@page=2#para28" 不能冒充 value 28 的内容命中。
    judgement = judge_case(
        cases["p-mix-zh"],
        answer="Agency 72%（来源：deck.md@page=2#para28）",
        locators=["deck.md@page=2#para1"],
    )
    assert [c.content_hit for c in judgement.claims] == [True, False]


# ---------------------------------------------------------------------------
# positive 判定
# ---------------------------------------------------------------------------


def test_positive_passes_when_content_and_page_both_hit(cases: dict[str, GoldCase]) -> None:
    judgement = judge_case(
        cases["p-mix-zh"], answer="代理人 72%，合作伙伴 28%。", locators=["d@page=2#para3"]
    )
    assert judgement.passed
    assert all(c.content_hit and c.page_hit and c.satisfied for c in judgement.claims)


def test_positive_content_without_page_fails(cases: dict[str, GoldCase]) -> None:
    judgement = judge_case(
        cases["p-mix-zh"], answer="72% and 28%", locators=["d@page=3#para1", "d@page=12#p1"]
    )
    assert not judgement.passed
    assert [(c.content_hit, c.page_hit) for c in judgement.claims] == [(True, False)] * 2


def test_positive_page_without_content_fails(cases: dict[str, GoldCase]) -> None:
    judgement = judge_case(cases["p-mix-zh"], answer="72% only", locators=["d@page=2#para1"])
    assert not judgement.passed
    assert [(c.content_hit, c.page_hit) for c in judgement.claims] == [(True, True), (False, True)]


def test_any_of_passes_through_either_anchor(cases: dict[str, GoldCase]) -> None:
    case = cases["p-roe"]
    # quote 锚点在第 1 页（整句）；图表锚点在第 3 页（17.5%）。
    via_chart = judge_case(case, answer="ROE was 17.5%", locators=["d@page=3#para1"])
    via_quote = judge_case(case, answer="A record ROE of 17.5%.", locators=["d@page=1#para2"])
    assert via_chart.passed and via_quote.passed
    # 内容只命中图表锚点、来源只有 quote 锚点的页 → 各自命中但不成对。
    crossed = judge_case(case, answer="ROE was 17.5%", locators=["d@page=1#para1"])
    claim = crossed.claims[0]
    assert (claim.content_hit, claim.page_hit, claim.satisfied) == (True, True, False)
    assert not crossed.passed


def test_value_anchor_matches_number_with_unit() -> None:
    case = GoldCase(
        case_id="c",
        case_class="positive",
        questions=(("en", "q"),),
        required_claims=(
            (ClaimAnchor(kind="chart_value", page_index=4, value="514", unit="$m"),),
            (ClaimAnchor(kind="chart_value", page_index=4, value="1234.5", unit="%"),),
        ),
    )
    judgement = judge_case(
        case, answer="Thailand VONB was US$514m, up 1,234.5 %", locators=["d@page=5#para1"]
    )
    assert judgement.passed
    unit_missing = judge_case(case, answer="514 and 1234.5", locators=["d@page=5#para1"])
    assert [c.content_hit for c in unit_missing.claims] == [True, False]


def test_grounded_only_needs_an_answer_with_sources(cases: dict[str, GoldCase]) -> None:
    case = cases["p-grounded"]
    assert judge_case(case, answer="Growth was 11%.", locators=["d@page=6#para1"]).passed
    assert not judge_case(case, answer="Growth was 11%.", locators=[]).passed
    assert not judge_case(
        case,
        answer="未检索到与该问题相关的资料，无法基于现有知识库作答。",
        locators=["d@page=1#p1"],
    ).passed


# ---------------------------------------------------------------------------
# abstain 判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "查不到：VONB / HK / FY2030（渠道 TOTAL）未在事实表中找到。",
        "未检索到与该问题相关的资料，无法基于现有知识库作答。",
        "检索片段中未提及 2030 年的预测。",
        "The provided excerpts do not contain a forecast for 2030.",
        "This information is not provided in the context.",
        "I can’t determine that from the excerpts.",
        "The snippets don't state which stage comes after Foundation.",
        "The excerpt doesn’t show the order.",
    ],
)
def test_refusal_phrasings_are_recognized(answer: str) -> None:
    assert is_refusal(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "The forecast VONB for 2030 is $6,000m.",
        "AI 服务暂时不可用，未能生成归因，请稍后再试。",
        "想查询哪个指标？目前支持：REVENUE、VONB。",
    ],
)
def test_answers_degrades_and_clarifications_are_not_refusals(answer: str) -> None:
    assert not is_refusal(answer)


def test_abstain_passes_on_refusal(cases: dict[str, GoldCase]) -> None:
    judgement = judge_case(cases["a-forecast"], answer="片段中没有提供该预测。", locators=[])
    assert judgement.passed and judgement.refusal


def test_abstain_fails_on_answer(cases: dict[str, GoldCase]) -> None:
    judgement = judge_case(cases["a-forecast"], answer="It will be $7bn.", locators=["d@page=1#p"])
    assert not judgement.passed


def test_abstain_fails_when_refusal_leaks_forbidden_number(cases: dict[str, GoldCase]) -> None:
    judgement = judge_case(
        cases["a-forecast"], answer="Not provided, though the gap is 44 points.", locators=[]
    )
    assert judgement.refusal and judgement.forbidden_hits == ("44",)
    assert not judgement.passed


# ---------------------------------------------------------------------------
# 路由标签 / 强制叙事解析器 / 包装件
# ---------------------------------------------------------------------------


def _result(route: str, **kwargs: object) -> AgentResult:
    return AgentResult(answer="x", route=route, **kwargs)  # type: ignore[arg-type]


def test_route_label_mapping() -> None:
    ask_first = ClarificationResult(mode=CLARIFY_ASK_FIRST, question="哪个指标？")
    assert route_label(_result(ROUTE_STRUCTURED, clarification=ask_first)) == "clarify"
    assert route_label(_result(ROUTE_STRUCTURED, tool_results=[{"status": "not_found"}])) == (
        "not_found"
    )
    assert route_label(_result(ROUTE_STRUCTURED, tool_results=[{"status": "found"}])) == (
        "structured"
    )
    assert route_label(_result(ROUTE_NARRATIVE, sources=[])) == "not_found"
    assert route_label(_result(ROUTE_NARRATIVE, sources=[{"doc": "d", "locator": "l"}])) == (
        "narrative"
    )
    assert route_label(_result(ROUTE_COMPOSITE, tool_results=[{"status": "not_found"}])) == (
        "composite"
    )
    fallback = _result(
        ROUTE_NARRATIVE,
        sources=[{"doc": "d", "locator": "l"}],
        tool_results=[{"status": "not_found"}],
        fallback="structured_no_hit",
    )
    assert route_label(fallback) == "fallback"
    ungrounded = AgentResult(
        answer="查不到：资料中没有能回答该问题的依据。想查询哪个指标？",
        route=ROUTE_STRUCTURED,
        clarification=ask_first,
    )
    assert route_label(ungrounded) == "not_found"


def test_forced_narrative_parser_keeps_slots_and_raw_question() -> None:
    parsed = ForcedNarrativeIntentParser().parse(
        "What was the REVENUE in FY2024?", reference_date=date(2026, 9, 23)
    )
    assert parsed.route == ROUTE_NARRATIVE
    assert parsed.raw_question == "What was the REVENUE in FY2024?"


def test_counting_provider_counts_calls() -> None:
    provider = CountingProvider(MockProvider())
    provider.chat([{"role": "user", "content": "hi"}])
    provider.chat([{"role": "user", "content": "hi"}])
    assert provider.calls == 2 and provider.seconds >= 0.0


def test_counting_provider_forwards_image_capability() -> None:
    from ragspine.agent.claude_cli_provider import ClaudeCliProvider
    from ragspine.agent.llm_provider import provider_supports_images

    assert provider_supports_images(CountingProvider(ClaudeCliProvider()))
    assert not provider_supports_images(CountingProvider(MockProvider()))


def test_recording_retriever_records_ranked_locators() -> None:
    class Fake:
        def retrieve(self, query, *, filters=None, top_k=50):  # noqa: ANN001, ANN202
            return [{"source_locator": "d@page=3#para1"}, {"locator": "d@page=1#para2"}]

    recorder = RecordingRetriever(Fake())
    recorder.retrieve("q", top_k=5)
    assert recorder.locators() == ["d@page=3#para1", "d@page=1#para2"]
    recorder.reset()
    assert recorder.locators() == []


def test_recall_reports_chunk_rank_and_distinct_page_rank() -> None:
    """同一页被多个块占位时，按块的名次 > 按不同页的名次；两种口径都报告。"""
    case = GoldCase(
        case_id="p",
        case_class="positive",
        questions=(("en", "q"),),
        required_claims=((ClaimAnchor(kind="quote", page_index=6, quote="x"),),),
    )
    run = CaseRun(
        route=ROUTE_FORCED_NARRATIVE,
        case_id="p",
        case_class="positive",
        language="en",
        question="q",
        answer="",
        answer_plain="",
        agent_route="narrative",
        route_label="narrative",
        sources=[],
        retrieved_locators=[],
        judgement=Judgement(passed=False, reason="", refusal=False),
        llm_calls=0,
        seconds=0.0,
        retrieved_pages=[5, 5, 5, 7],
    )
    recall = recall_at_k([run], [case], ks=(1, 3, 5))
    assert recall["recall"] == {"@1": 0.0, "@3": 0.0, "@5": 1.0}
    assert recall["page_recall"] == {"@1": 0.0, "@3": 1.0, "@5": 1.0}
    assert recall["per_case"][0]["gold_rank"] == 4
    assert recall["per_case"][0]["gold_page_rank"] == 2


# ---------------------------------------------------------------------------
# 端到端：小 DI markdown → 两路 → 汇总 / recall@k / 报告
# ---------------------------------------------------------------------------

_DECK = """# Fictional Deck

Welcome to the results.

<!-- PageBreak -->

# Distribution Mix

Agency share of VONB was 72%. Partnerships share of VONB was 28%.

<!-- PageBreak -->

# Returns

The record ROE of 17.5% was achieved.
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    deck = tmp_path / "deck.md"
    deck.write_text(_DECK, encoding="utf-8")
    RAGSpine.local(ws).ingest(deck)
    return ws


def test_persisted_ingest_fills_the_vector_channel_for_eval(tmp_path: Path) -> None:
    """评测走正式路径：入库即嵌入落盘（storage.persist_vectors），检索期 open_vector_channel 读回。"""
    pytest.importorskip("sqlite_vec")
    ws = tmp_path / "ws-vec"
    deck = tmp_path / "deck.md"
    deck.write_text(_DECK, encoding="utf-8")
    ingest = RAGSpine.local(
        ws, preset="balanced", config={"storage": {"persist_vectors": True}}
    ).ingest(deck)
    assert ingest.vector_report is not None and ingest.vector_report.total > 0
    count = ingest.vector_report.total

    config = ServiceConfig(
        db_path=str(ws / "knowledge.db"),
        chunk_db_path=str(ws / "knowledge.db"),
        persist_vectors=True,
    )
    backend, index = open_vector_channel(config, DeterministicEmbeddingBackend())
    assert backend is not None and index is not None
    try:
        hits = index.store.query(
            DeterministicEmbeddingBackend().embed_texts(["Agency"])[0], k=count
        )
    finally:
        index.close()
    assert len(hits) == count


def test_end_to_end_both_routes_and_report(
    workspace: Path, gold_path: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gold = load_nl_gold(gold_path)
    db = workspace / "knowledge.db"
    retriever, chunk_store = build_narrative_retriever(db)
    fact_store = SqliteFactStore(db)
    fact_store.init_schema()
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER_NAME)
    try:
        runs_b = run_route(
            gold,
            ROUTE_FORCED_NARRATIVE,
            store=fact_store,
            retriever=retriever,
            provider=MockProvider(),
            reference_date=date(2026, 9, 23),
        )
        runs_a = run_route(
            gold,
            ROUTE_ASK,
            store=fact_store,
            retriever=retriever,
            provider=MockProvider(),
            reference_date=date(2026, 9, 23),
        )
    finally:
        fact_store.close()
        chunk_store.close()

    # adversarial 不跑；known-gap 照跑；每条 case 每种语言一次。
    ran = {(r.case_id, r.language) for r in runs_b}
    assert ("x-probe", "en") not in ran
    assert ("k-gap", "en") in ran and ("p-mix-zh", "zh") in ran
    assert {r.route_label for r in runs_b} <= {"narrative", "not_found"}
    assert all(r.retrieved_locators for r in runs_b if r.route_label == "narrative")
    # MockProvider 回显检索片段 → 第 2 页 72%/28% 被命中且来源含第 2 页。
    mix = next(r for r in runs_b if r.case_id == "p-mix-zh")
    assert mix.judgement.passed, mix.answer
    assert sum(r.llm_calls for r in runs_b) >= 1

    summary = summarize(runs_b)
    assert summary["main"]["total"] == 4  # 3 positive + 1 abstain；known-gap 不计主分
    assert summary["known_gap"]["total"] == 1
    assert set(summary["by_class"]) == {"positive", "abstain"}
    assert 0.0 <= summary["claims"]["content_hit_rate"] <= 1.0
    assert set(summary["by_language"]) == {"en", "zh"}

    recall = recall_at_k(runs_b, gold, ks=(1, 3, 5, 10))
    assert set(recall["recall"]) == {"@1", "@3", "@5", "@10"}
    assert recall["recall"]["@10"] >= recall["recall"]["@1"]
    assert set(recall["page_recall"]) == {"@1", "@3", "@5", "@10"}
    assert all(recall["page_recall"][k] >= recall["recall"][k] for k in recall["recall"])

    out = write_report(
        tmp_path / "report",
        meta={"label": "test"},
        cases=gold,
        runs={ROUTE_ASK: runs_a, ROUTE_FORCED_NARRATIVE: runs_b},
    )
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert set(report["routes"]) == {ROUTE_ASK, ROUTE_FORCED_NARRATIVE}
    assert report["skipped"][0]["case_id"] == "x-probe"
    assert "route_distribution" in report["routes"][ROUTE_ASK]
    assert "retrieval" in report["routes"][ROUTE_FORCED_NARRATIVE]
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "p-mix-zh" in md and "x-probe" in md
    raw = sorted((out / "cases").rglob("*.json"))
    assert len(raw) == len(runs_a) + len(runs_b)
    assert "answer" in json.loads(raw[0].read_text(encoding="utf-8"))

    # 隐私：答案正文只进评测产物，绝不进 observability trace。
    traced = "\n".join(record.getMessage() for record in caplog.records)
    assert "72%" not in traced and "Agency share" not in traced


# ---------------------------------------------------------------------------
# 语料按 gold 的 pinned 页范围裁剪（页号保持物理序）
# ---------------------------------------------------------------------------


def test_gold_selected_pages(gold_path: Path) -> None:
    assert gold_selected_pages(gold_path) == (1, 2, 3)


def test_select_di_pages_blanks_other_pages_and_keeps_numbering(tmp_path: Path) -> None:
    sliced = select_di_pages(_DECK, [1, 3])
    assert sliced.count("<!-- PageBreak -->") == _DECK.count("<!-- PageBreak -->")
    assert "Distribution Mix" not in sliced and "72%" not in sliced
    assert "record ROE of 17.5%" in sliced and "Fictional Deck" in sliced

    ws = tmp_path / "ws-sliced"
    deck = tmp_path / "deck.md"
    deck.write_text(sliced, encoding="utf-8")
    RAGSpine.local(ws).ingest(deck)
    retriever, chunk_store = build_narrative_retriever(ws / "knowledge.db")
    try:
        snippets = retriever.retrieve("record ROE", top_k=10)
    finally:
        chunk_store.close()
    pages = {str(s["source_locator"]).split("@page=")[1].split("#")[0] for s in snippets}
    assert pages == {"3"}
