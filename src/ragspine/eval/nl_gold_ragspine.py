"""ragspine 主链路的 nl-gold 评测：把 nl-answers-gold 集跑在 ``answer_question`` 上。

gold 文件（``nl-answers-gold-v1``，如 AIA 样本
``data/benchmarks/enterprise-pdf-rag/aia-2026-interim/nl-answers-gold-v1.json``）由本模块自带的
轻量读取器解析（只读评测用得到的字段；ragspine 按 ADR 0022 不 import ``enterprise_pdf_rag``）；
**判定**在此重写，只看 ragspine 答案能观察到的东西：

- positive：每条 required claim（``any_of`` 任一锚点）要求
  ① 内容：规范化后的答案含锚点的 quote / text，或含 value（数字边界 + ``%`` 单位容忍格式差异）；
  ② 页码：sources 里有 locator 命中 ``@page={page_index+1}#``；
  同一锚点同时满足 ①② 才算该 claim 满足。① ② 分开计数，便于定位失分。
  ``grounded_only`` 的 case 不冻结 claim：非拒答且带来源即可。
- abstain：答案须为拒答 / 未找到（agent 固定文案 + LLM 拒答措辞），且不含 forbidden number。
- known-gap（``expected.known_gap``）：照跑、单列，不计主分。
- adversarial / ``offline_only``：针对 enterprise 响应的变异探针，ragspine 无对应信封 → 跳过并记录原因。

两条路由：``A-ask``＝原样走 ``answer_question``（规则意图解析，含结构化 / 澄清 / 叙事分流）；
``B-narrative``＝注入 :class:`ForcedNarrativeIntentParser`，其余逐字不变，只把路由钉在叙事通道。
检索侧用 :class:`RecordingRetriever` 旁路记录排序后的 locator，算页级 recall@k，不多发检索请求。

隐私：答案正文只写进评测产物（report / 每条原始响应），本模块不向 observability trace 写任何东西；
``answer_question`` 自身的 trace 仍只含代码 / 计数 / 耗时。
"""

import json
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

from corespine import ChatCompletion

from ragspine.agent.agent import AgentResult, NarrativeRetriever, answer_question
from ragspine.agent.intent import (
    CLARIFY_ASK_FIRST,
    CLARIFY_OUT_OF_SCOPE_ENTITY,
    ROUTE_NARRATIVE,
    ROUTE_STRUCTURED,
    IntentParser,
    ParsedIntent,
    RuleIntentParser,
)
from ragspine.agent.llm_provider import LLMProvider, provider_supports_images
from ragspine.storage.fact_store import FactStore

ROUTE_ASK = "A-ask"
ROUTE_FORCED_NARRATIVE = "B-narrative"
ROUTES = (ROUTE_ASK, ROUTE_FORCED_NARRATIVE)
ROUTE_DESCRIPTIONS = {
    ROUTE_ASK: "真实 ask 链路（answer_question + 规则意图解析，路由原样）",
    ROUTE_FORCED_NARRATIVE: "强制叙事（注入 ForcedNarrativeIntentParser，其余不变）",
}
RECALL_KS = (1, 3, 5, 10)

ADVERSARIAL_SKIP_REASON = (
    "adversarial probe: it mutates an enterprise_pdf_rag answer envelope (scripted "
    "offline model output + claim verification); ragspine answers carry no such envelope"
)
OFFLINE_ONLY_SKIP_REASON = "offline_only: the case scripts a model output for the offline replay"

# ---------------------------------------------------------------------------
# gold
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimAnchor:
    """required claim 的一个可接受锚点（``any_of`` 里的一项）。page_index 0 起。"""

    kind: str
    page_index: int
    quote: str | None = None
    text: str | None = None
    value: str | None = None
    unit: str | None = None

    def describe(self) -> str:
        shown = self.quote or self.text or f"{self.value}{self.unit or ''}"
        return f"p{self.page_index + 1}:{shown}"


@dataclass(frozen=True)
class GoldCase:
    """评测视角的一条 gold case。case_class ∈ positive / abstain / known-gap / adversarial。"""

    case_id: str
    case_class: str
    questions: tuple[tuple[str, str], ...]
    required_claims: tuple[tuple[ClaimAnchor, ...], ...] = ()
    grounded_only: bool = False
    forbidden_numbers: tuple[str, ...] = ()
    known_gap_detail: str | None = None
    skip_reason: str | None = None
    expect_abstain: bool = False


GOLD_SCHEMA_VERSION = "nl-answers-gold-v1"
_CASE_CLASSES = ("positive", "abstain", "adversarial")
_STATUSES = ("answered", "abstained")


class GoldFormatError(ValueError):
    """gold 文件不符合 nl-answers-gold-v1 里本评测要读的那部分结构。"""


def _require(
    mapping: Mapping[str, Any], key: str, kind: type | tuple[type, ...], where: str
) -> Any:
    value = mapping.get(key)
    if not isinstance(value, kind):
        raise GoldFormatError(f"{where}: `{key}` 缺失或类型不对")
    return value


def _optional_str(mapping: Mapping[str, Any], key: str, where: str) -> str | None:
    value = mapping.get(key)
    if value is not None and not isinstance(value, str):
        raise GoldFormatError(f"{where}: `{key}` 须为字符串")
    return value


def _read_gold(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GoldFormatError(f"gold 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != GOLD_SCHEMA_VERSION:
        raise GoldFormatError(f"gold 的 schema_version 须为 {GOLD_SCHEMA_VERSION!r}")
    return payload


def _parse_anchor(raw: object, where: str) -> ClaimAnchor:
    if not isinstance(raw, dict):
        raise GoldFormatError(f"{where}: claim 须为对象")
    page_index = _require(raw, "page_index", int, where)
    if isinstance(page_index, bool) or page_index < 0:
        raise GoldFormatError(f"{where}: page_index 须为 0 起的整数")
    anchor = ClaimAnchor(
        kind=_require(raw, "kind", str, where),
        page_index=page_index,
        quote=_optional_str(raw, "quote", where),
        text=_optional_str(raw, "text", where),
        value=_optional_str(raw, "value", where),
        unit=_optional_str(raw, "unit", where),
    )
    if not (anchor.quote or anchor.text or anchor.value):
        raise GoldFormatError(f"{where}: claim 须带 quote / text / value 之一")
    return anchor


def _parse_case(raw: object, index: int) -> GoldCase:
    where = f"cases[{index}]"
    if not isinstance(raw, dict):
        raise GoldFormatError(f"{where}: case 须为对象")
    case_id = _require(raw, "case_id", str, where)
    where = f"case {case_id}"
    case_class = _require(raw, "case_class", str, where)
    if case_class not in _CASE_CLASSES:
        raise GoldFormatError(f"{where}: 未知 case_class {case_class!r}")
    question = _require(raw, "question", dict, where)
    questions = tuple(
        (lang, text.strip())
        for lang in ("en", "zh")
        if isinstance(text := question.get(lang), str) and text.strip()
    )
    if not questions:
        raise GoldFormatError(f"{where}: question 须至少有非空的 en 或 zh")
    expected = _require(raw, "expected", dict, where)
    status = _require(expected, "status", str, where)
    if status not in _STATUSES:
        raise GoldFormatError(f"{where}: 未知 expected.status {status!r}")
    required: list[tuple[ClaimAnchor, ...]] = []
    for spec in expected.get("required_claims", []) or []:
        options = spec.get("any_of") if isinstance(spec, dict) and "any_of" in spec else [spec]
        if not isinstance(options, list) or not options:
            raise GoldFormatError(f"{where}: any_of 须为非空数组")
        required.append(tuple(_parse_anchor(option, where) for option in options))
    forbidden = expected.get("forbidden_numbers", []) or []
    if not isinstance(forbidden, list) or not all(isinstance(n, str) for n in forbidden):
        raise GoldFormatError(f"{where}: forbidden_numbers 须为字符串数组")
    known_gap = bool(expected.get("known_gap", False))
    skip_reason = None
    if case_class == "adversarial":
        skip_reason = ADVERSARIAL_SKIP_REASON
    elif raw.get("offline_only"):
        skip_reason = OFFLINE_ONLY_SKIP_REASON
    return GoldCase(
        case_id=case_id,
        case_class="known-gap" if known_gap else case_class,
        questions=questions,
        required_claims=tuple(required),
        grounded_only=bool(expected.get("grounded_only", False)),
        forbidden_numbers=tuple(forbidden),
        known_gap_detail=_optional_str(expected, "known_gap_detail", where),
        skip_reason=skip_reason,
        expect_abstain=status == "abstained",
    )


def load_nl_gold(path: str | Path) -> tuple[GoldCase, ...]:
    """读 nl-answers-gold-v1 文件 → 评测视角的 case 列表；结构不对即 GoldFormatError（ValueError）。

    只读本评测用得到的字段（与 enterprise 侧严格 schema 同形）；ragspine 不 import
    ``enterprise_pdf_rag``（ADR 0022 conformance 门），故不复用那边的 pydantic 解析器。
    """
    payload = _read_gold(path)
    raw_cases = _require(payload, "cases", list, "gold")
    cases = tuple(_parse_case(raw, i) for i, raw in enumerate(raw_cases))
    ids = [case.case_id for case in cases]
    if len(set(ids)) != len(ids):
        raise GoldFormatError("gold 的 case_id 须唯一")
    return cases


def gold_selected_pages(path: str | Path) -> tuple[int, ...]:
    """gold 冻结时选定的物理页（1 起，``pinned.selected_physical_pages``）；缺省为空。"""
    pinned = _read_gold(path).get("pinned") or {}
    pages = pinned.get("selected_physical_pages", []) if isinstance(pinned, dict) else []
    if not isinstance(pages, list) or not all(
        isinstance(p, int) and not isinstance(p, bool) and p >= 1 for p in pages
    ):
        raise GoldFormatError("pinned.selected_physical_pages 须为 1 起的整数数组")
    return tuple(pages)


_DI_PAGE_BREAK_RE = re.compile(r"<!--\s*PageBreak\s*-->")


def select_di_pages(markdown: str, pages: Iterable[int]) -> str:
    """只保留 DI markdown 的指定物理页（1 起）；其余页清空但保留分页符，页号不变。

    gold 只覆盖文档的一部分页（如 1–20）时，用它把语料裁到同一范围，免得"范围外有答案"的页
    让 abstain 类 case 失去意义、或让等价页抢走来源。
    """
    keep = set(pages)
    parts = _DI_PAGE_BREAK_RE.split(markdown)
    return "\n<!-- PageBreak -->\n".join(
        part if index + 1 in keep else "\n" for index, part in enumerate(parts)
    )


# ---------------------------------------------------------------------------
# 规范化与匹配
# ---------------------------------------------------------------------------

_CHAR_FOLD = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "′": "'",
        "–": "-",
        "—": "-",
        "−": "-",
    }
)
# 答案里的来源回指（如 "deck.md@page=18#para3"）不是内容，匹配前剔除，免得页码/段号冒充数值。
_LOCATOR_RE = re.compile(r"[\w.\-]*@page=\d+(?:#[\w\-]+)?")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_PERCENT_WORD_RE = re.compile(r"(?<=\d)\s*(?:per\s*cent|percent)\b")
_SPACED_PERCENT_RE = re.compile(r"(?<=\d)\s+%")
# 除 \w、%、小数点外的一切标点折成空格（冒号/破折号/引号/markdown 强调符 都不影响匹配）。
_PUNCT_RE = re.compile(r"[^\w%.]+|_+")
_NON_DECIMAL_DOT_RE = re.compile(r"(?<!\d)\.|\.(?!\d)")
_SPACES_RE = re.compile(r"\s+")


def normalize_answer(text: str) -> str:
    """答案 / 期望文本的统一规范化：NFKC、弯直引号、大小写、千分位、百分号、标点、空白。"""
    folded = unicodedata.normalize("NFKC", text).translate(_CHAR_FOLD).casefold()
    folded = _LOCATOR_RE.sub(" ", folded)
    folded = _THOUSANDS_RE.sub("", folded)
    folded = _PERCENT_WORD_RE.sub("%", folded)
    folded = _SPACED_PERCENT_RE.sub("%", folded)
    folded = _PUNCT_RE.sub(" ", folded)
    folded = _NON_DECIMAL_DOT_RE.sub(" ", folded)
    return _SPACES_RE.sub(" ", folded).strip()


def contains_normalized(haystack_normalized: str, needle: str) -> bool:
    """已规范化的答案是否含 needle（needle 在此规范化）；数字两端不许粘连别的数字。"""
    target = normalize_answer(needle)
    if not target:
        return False
    lead = r"(?<![\d.])" if target[0].isdigit() else ""
    trail = r"(?![\d%]|\.\d)" if target[-1].isdigit() else ""
    return re.search(lead + re.escape(target) + trail, haystack_normalized) is not None


def _value_needle(anchor: ClaimAnchor) -> str | None:
    if not anchor.value:
        return None
    unit = (anchor.unit or "").strip()
    return f"{anchor.value}%" if unit == "%" else anchor.value


def anchor_content_hit(anchor: ClaimAnchor, answer_normalized: str) -> bool:
    needles = [n for n in (anchor.quote, anchor.text, _value_needle(anchor)) if n]
    return any(contains_normalized(answer_normalized, n) for n in needles)


def _page_re(page_index: int) -> re.Pattern[str]:
    return re.compile(rf"@page={page_index + 1}(?:#|$)")


def anchor_page_hit(anchor: ClaimAnchor, locators: Sequence[str]) -> bool:
    pattern = _page_re(anchor.page_index)
    return any(pattern.search(loc) for loc in locators)


# agent 编排层的确定性拒答 / 未找到文案（agent.py：_not_found_answer、_run_narrative 无结果、
# _unrecognized_answer）+ LLM 在叙事合成里常见的拒答措辞。provider 降级文案与澄清反问【不】算拒答。
REFUSAL_MARKERS = (
    "查不到",
    "未检索到与该问题相关的资料",
    "无法基于现有知识库作答",
    "无法识别参数",
    "未提及",
    "没有提及",
    "未提到",
    "没有提到",
    "未提供",
    "没有提供",
    "未包含",
    "不包含",
    "没有包含",
    "未披露",
    "没有披露",
    "未找到",
    "没有找到",
    "找不到",
    "无法确定",
    "无法回答",
    "无法给出",
    "无法从",
    "没有相关",
    "无相关",
    "not found",
    "not mentioned",
    "not provided",
    "not included",
    "not disclosed",
    "not stated",
    "not specified",
    "not available",
    "no information",
    "does not contain",
    "do not contain",
    "does not provide",
    "do not provide",
    "does not mention",
    "do not mention",
    "does not include",
    "do not include",
    "does not specify",
    "do not specify",
    "does not state",
    "do not state",
    "cannot be determined",
    "can't be determined",
    "cannot determine",
    "can't determine",
    "unable to",
    "cannot answer",
    "can't answer",
    "not possible to",
)
_REFUSAL_NORMALIZED = tuple(normalize_answer(marker) for marker in REFUSAL_MARKERS)
# 英文缩写否定（don't / doesn't / didn't + 动词；规范化后撇号变空格，如 "don t state"）。
_CONTRACTED_REFUSAL_RE = re.compile(
    r"\b(?:don|doesn|didn) t (?:contain|provide|mention|include|specify|state|show|list|give|say)"
)


def is_refusal(answer: str) -> bool:
    """答案是否为拒答 / 未找到。"""
    normalized = normalize_answer(answer)
    if any(marker in normalized for marker in _REFUSAL_NORMALIZED):
        return True
    return _CONTRACTED_REFUSAL_RE.search(normalized) is not None


@dataclass(frozen=True)
class ClaimJudgement:
    anchors: tuple[str, ...]
    content_hit: bool
    page_hit: bool
    satisfied: bool


@dataclass(frozen=True)
class Judgement:
    passed: bool
    reason: str
    refusal: bool
    claims: tuple[ClaimJudgement, ...] = ()
    forbidden_hits: tuple[str, ...] = ()


def judge_case(case: GoldCase, *, answer: str, locators: Sequence[str]) -> Judgement:
    """按本模块口径判一条答案。answer 用不含来源后缀的正文（answer_plain）。"""
    normalized = normalize_answer(answer)
    refusal = is_refusal(answer)
    forbidden = tuple(n for n in case.forbidden_numbers if contains_normalized(normalized, n))

    if case.expect_abstain or case.case_class == "abstain":
        if not refusal:
            return Judgement(False, "answered instead of abstaining", refusal, (), forbidden)
        if forbidden:
            return Judgement(False, "refusal leaks a forbidden number", refusal, (), forbidden)
        return Judgement(True, "abstained", refusal, (), forbidden)

    if not case.required_claims:
        if refusal:
            return Judgement(False, "refused a grounded-only question", refusal, (), forbidden)
        if not locators:
            return Judgement(False, "answer carries no source", refusal, (), forbidden)
        return Judgement(True, "grounded answer with sources", refusal, (), forbidden)

    claims: list[ClaimJudgement] = []
    for group in case.required_claims:
        content = [anchor_content_hit(a, normalized) for a in group]
        pages = [anchor_page_hit(a, locators) for a in group]
        claims.append(
            ClaimJudgement(
                anchors=tuple(a.describe() for a in group),
                content_hit=any(content),
                page_hit=any(pages),
                satisfied=any(c and p for c, p in zip(content, pages, strict=True)),
            )
        )
    passed = all(c.satisfied for c in claims)
    if passed:
        reason = "all claims satisfied"
    elif not all(c.content_hit for c in claims):
        reason = "claim content missing from answer"
    elif not all(c.page_hit for c in claims):
        reason = "claim page missing from sources"
    else:
        reason = "content and page hit different anchors"
    return Judgement(passed, reason, refusal, tuple(claims), forbidden)


# ---------------------------------------------------------------------------
# 路由 / 包装件
# ---------------------------------------------------------------------------


class ForcedNarrativeIntentParser:
    """IntentParser：先用内层解析器取槽位，再把路由钉成 narrative（B 路由）。

    raw_question 原样保留，安全门照常从原问句复核越权 / 竞品（契约见 IntentParser）。
    """

    def __init__(self, inner: IntentParser | None = None) -> None:
        self._inner = inner or RuleIntentParser()

    def parse(self, question: str, *, reference_date: date | None = None) -> ParsedIntent:
        parsed = self._inner.parse(question, reference_date=reference_date)
        return replace(parsed, route=ROUTE_NARRATIVE)


def route_label(result: AgentResult) -> str:
    """把一次 answer_question 结果归为 structured / narrative / composite / clarify /
    not_found / out_of_scope。"""
    clarification = result.clarification
    if clarification is not None and clarification.mode == CLARIFY_ASK_FIRST:
        return "clarify"
    if clarification is not None and clarification.mode == CLARIFY_OUT_OF_SCOPE_ENTITY:
        return "out_of_scope"
    if result.route == ROUTE_STRUCTURED:
        found = any(r.get("status") == "found" for r in result.tool_results)
        return ROUTE_STRUCTURED if found else "not_found"
    if result.route == ROUTE_NARRATIVE and not result.sources:
        return "not_found"
    return result.route


def _snippet_locator(snippet: Mapping[str, object]) -> str:
    return str(snippet.get("locator") or snippet.get("source_locator") or "")


class RecordingRetriever:
    """NarrativeRetriever 旁路包装：原样转发，并记下每次检索的排序后 locator（只记定位，不记正文）。"""

    def __init__(self, inner: NarrativeRetriever) -> None:
        self._inner = inner
        self._calls: list[list[str]] = []

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]:
        snippets = self._inner.retrieve(query, filters=filters, top_k=top_k)
        self._calls.append([_snippet_locator(s) for s in snippets])
        return snippets

    def locators(self) -> list[str]:
        """首次检索的排序后 locator（叙事通路每问只检索一次）。"""
        return list(self._calls[0]) if self._calls else []

    def reset(self) -> None:
        self._calls = []


class CountingProvider:
    """LLMProvider 旁路包装：统计 chat 调用次数与耗时，不看、不存消息内容。"""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.calls = 0
        self.seconds = 0.0
        # 透传读图能力：包装后图文混合上下文照常发给内层 provider。
        self.supports_image_input = provider_supports_images(inner)

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        self.calls += 1
        started = time.perf_counter()
        try:
            return self._inner.chat(messages, tools=tools)
        finally:
            self.seconds += time.perf_counter() - started


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


@dataclass
class CaseRun:
    """一条 case × 一种语言 × 一条路由的运行结果（含原始响应，供报告落盘）。"""

    route: str
    case_id: str
    case_class: str
    language: str
    question: str
    answer: str
    answer_plain: str
    agent_route: str
    route_label: str
    sources: list[dict[str, object]]
    retrieved_locators: list[str]
    judgement: Judgement
    llm_calls: int
    seconds: float
    error: str | None = None
    retrieved_pages: list[int] = field(default_factory=list)


_PAGE_NUM_RE = re.compile(r"@page=(\d+)(?:#|$)")


def _page_of(locator: str) -> int | None:
    match = _PAGE_NUM_RE.search(locator)
    return int(match.group(1)) if match else None


def run_route(
    cases: Iterable[GoldCase],
    route: str,
    *,
    store: FactStore,
    retriever: NarrativeRetriever | None,
    provider: LLMProvider,
    reference_date: date | None = None,
    languages: Sequence[str] | None = None,
    progress: Callable[[CaseRun], None] | None = None,
) -> list[CaseRun]:
    """跑一条路由：每条未跳过的 case、每种（被选中的）语言问一次并判定。"""
    if route not in ROUTES:
        raise ValueError(f"未知路由 {route!r}，可选 {ROUTES}")
    parser: IntentParser | None = (
        ForcedNarrativeIntentParser() if route == ROUTE_FORCED_NARRATIVE else None
    )
    counter = CountingProvider(provider)
    recorder = RecordingRetriever(retriever) if retriever is not None else None
    runs: list[CaseRun] = []
    for case in cases:
        if case.skip_reason:
            continue
        for language, question in case.questions:
            if languages is not None and language not in languages:
                continue
            if recorder is not None:
                recorder.reset()
            calls_before = counter.calls
            started = time.perf_counter()
            error: str | None = None
            try:
                result = answer_question(
                    question,
                    store,
                    counter,
                    reference_date=reference_date,
                    narrative_retriever=recorder,
                    intent_parser=parser,
                )
            except Exception as exc:  # noqa: BLE001 — 单条失败记为失败 case，整轮继续
                error = f"{type(exc).__name__}: {exc}"
                result = AgentResult(answer="", route="error")
            seconds = time.perf_counter() - started
            answer_plain = result.answer_plain or result.answer
            locators = [_snippet_locator(s) for s in result.sources]
            judgement = judge_case(case, answer=answer_plain, locators=locators)
            if error is not None:
                judgement = replace(judgement, passed=False, reason=f"error: {error}")
            retrieved = recorder.locators() if recorder is not None else []
            run = CaseRun(
                route=route,
                case_id=case.case_id,
                case_class=case.case_class,
                language=language,
                question=question,
                answer=result.answer,
                answer_plain=answer_plain,
                agent_route=result.route,
                route_label="error" if error else route_label(result),
                sources=list(result.sources),
                retrieved_locators=retrieved,
                judgement=judgement,
                llm_calls=counter.calls - calls_before,
                seconds=round(seconds, 3),
                error=error,
                retrieved_pages=[p for p in (_page_of(loc) for loc in retrieved) if p is not None],
            )
            runs.append(run)
            if progress is not None:
                progress(run)
    return runs


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _score(runs: Sequence[CaseRun]) -> dict[str, object]:
    passed = sum(r.judgement.passed for r in runs)
    return {"passed": passed, "total": len(runs), "rate": _rate(passed, len(runs))}


def summarize(runs: Sequence[CaseRun]) -> dict[str, Any]:
    """一条路由的分数：主分（positive+abstain）、分类分、分语言、claim 内容/页码命中、路由分布。"""
    scored = [r for r in runs if r.case_class in ("positive", "abstain")]
    known_gap = [r for r in runs if r.case_class == "known-gap"]
    positive = [r for r in scored if r.case_class == "positive"]
    claims = [c for r in positive for c in r.judgement.claims]
    with_claims = [r for r in positive if r.judgement.claims]
    by_class = {
        name: _score([r for r in scored if r.case_class == name])
        for name in ("positive", "abstain")
        if any(r.case_class == name for r in scored)
    }
    by_language = {
        lang: _score([r for r in scored if r.language == lang])
        for lang in sorted({r.language for r in scored})
    }
    return {
        "main": _score(scored),
        "by_class": by_class,
        "by_language": by_language,
        "known_gap": _score(known_gap),
        "claims": {
            "total": len(claims),
            "content_hits": sum(c.content_hit for c in claims),
            "page_hits": sum(c.page_hit for c in claims),
            "satisfied": sum(c.satisfied for c in claims),
            "content_hit_rate": _rate(sum(c.content_hit for c in claims), len(claims)),
            "page_hit_rate": _rate(sum(c.page_hit for c in claims), len(claims)),
            "cases_with_claims": len(with_claims),
            "cases_all_satisfied": sum(
                all(c.satisfied for c in r.judgement.claims) for r in with_claims
            ),
        },
        "route_distribution": dict(Counter(r.route_label for r in runs).most_common()),
        "errors": sum(r.error is not None for r in runs),
        "llm_calls": sum(r.llm_calls for r in runs),
        "seconds": round(sum(r.seconds for r in runs), 1),
    }


def _gold_rank(run: CaseRun, case: GoldCase, *, distinct_pages: bool = False) -> int | None:
    """所有 required claim 都已在前 k 个 chunk 内出现（任一锚点页）的最小 k；到底都凑不齐则 None。

    distinct_pages=True 时按不同页计名次（同一页的多个块只算第一次出现）。
    """
    pages = list(dict.fromkeys(run.retrieved_pages)) if distinct_pages else run.retrieved_pages
    worst = 0
    for group in case.required_claims:
        wanted = {a.page_index + 1 for a in group}
        rank = next((i + 1 for i, p in enumerate(pages) if p in wanted), None)
        if rank is None:
            return None
        worst = max(worst, rank)
    return worst


def recall_at_k(
    runs: Sequence[CaseRun],
    cases: Iterable[GoldCase],
    *,
    ks: Sequence[int] = RECALL_KS,
) -> dict[str, Any]:
    """页级 recall@k：冻结了 claim 的 positive / known-gap case，所有 claim 的 gold 页都进前 k。

    两种口径：``recall`` 的 k 按检索结果条数（chunk）计；``page_recall`` 的 k 按不同页计。
    """
    by_id = {c.case_id: c for c in cases}
    eligible: list[tuple[CaseRun, int | None, int | None]] = []
    for run in runs:
        case = by_id.get(run.case_id)
        if case is None or case.expect_abstain or not case.required_claims:
            continue
        eligible.append((run, _gold_rank(run, case), _gold_rank(run, case, distinct_pages=True)))

    def at_k(ranks: list[int | None]) -> dict[str, float]:
        return {
            f"@{k}": _rate(sum(1 for r in ranks if r is not None and r <= k), len(ranks))
            for k in ks
        }

    return {
        "cases": len(eligible),
        "recall": at_k([rank for _, rank, _ in eligible]),
        "page_recall": at_k([page_rank for _, _, page_rank in eligible]),
        "per_case": [
            {
                "case_id": run.case_id,
                "language": run.language,
                "gold_rank": rank,
                "gold_page_rank": page_rank,
            }
            for run, rank, page_rank in eligible
        ],
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int = 240) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _expected_text(case: GoldCase) -> str:
    if case.case_class == "abstain":
        return "abstain"
    if not case.required_claims:
        return "grounded answer with sources"
    return " AND ".join(" | ".join(a.describe() for a in group) for group in case.required_claims)


def _run_record(run: CaseRun) -> dict[str, Any]:
    record = asdict(run)
    record["judgement"] = asdict(run.judgement)
    return record


def write_report(
    out_dir: str | Path,
    *,
    meta: Mapping[str, object],
    cases: Sequence[GoldCase],
    runs: Mapping[str, Sequence[CaseRun]],
) -> Path:
    """落盘 report.json / report.md / cases/<route>/<case>-<lang>.json，返回目录。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    by_id = {c.case_id: c for c in cases}
    routes: dict[str, Any] = {}
    for route, route_runs in runs.items():
        summary = summarize(route_runs)
        summary["description"] = ROUTE_DESCRIPTIONS.get(route, route)
        summary["retrieval"] = recall_at_k(route_runs, cases)
        routes[route] = summary
        case_dir = out / "cases" / route
        case_dir.mkdir(parents=True, exist_ok=True)
        for run in route_runs:
            (case_dir / f"{run.case_id}-{run.language}.json").write_text(
                json.dumps(_run_record(run), ensure_ascii=False, indent=2), encoding="utf-8"
            )
    skipped = [
        {"case_id": c.case_id, "case_class": c.case_class, "reason": c.skip_reason}
        for c in cases
        if c.skip_reason
    ]
    known_gaps = [
        {"case_id": c.case_id, "detail": c.known_gap_detail}
        for c in cases
        if c.case_class == "known-gap"
    ]
    report = {
        "meta": dict(meta),
        "routes": routes,
        "skipped": skipped,
        "known_gaps": known_gaps,
        "cases": {route: [_run_record(r) for r in rs] for route, rs in runs.items()},
    }
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "report.md").write_text(
        _render_markdown(meta, routes, runs, by_id, skipped, known_gaps), encoding="utf-8"
    )
    return out


def _render_markdown(
    meta: Mapping[str, object],
    routes: Mapping[str, Any],
    runs: Mapping[str, Sequence[CaseRun]],
    cases: Mapping[str, GoldCase],
    skipped: Sequence[Mapping[str, object]],
    known_gaps: Sequence[Mapping[str, object]],
) -> str:
    lines = [f"# ragspine nl-gold report — {meta.get('label', '')}", ""]
    lines += [f"- {key}: `{value}`" for key, value in meta.items()]
    lines += ["", "## Scores", ""]
    lines += [
        "| route | main | positive | abstain | known-gap | claim content hit | claim page hit | "
        "cases all satisfied | LLM calls | seconds |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for route, s in routes.items():
        cls = s["by_class"]
        claims = s["claims"]

        def fmt(score: Mapping[str, Any] | None) -> str:
            if not score or not score["total"]:
                return "-"
            return f"{score['passed']}/{score['total']} ({score['rate']:.0%})"

        lines.append(
            f"| {route} | {fmt(s['main'])} | {fmt(cls.get('positive'))} | {fmt(cls.get('abstain'))}"
            f" | {fmt(s['known_gap'])} | {claims['content_hits']}/{claims['total']}"
            f" ({claims['content_hit_rate']:.0%}) | {claims['page_hits']}/{claims['total']}"
            f" ({claims['page_hit_rate']:.0%}) | {claims['cases_all_satisfied']}/"
            f"{claims['cases_with_claims']} | {s['llm_calls']} | {s['seconds']} |"
        )
    lines += ["", "### By language", ""]
    for route, s in routes.items():
        parts = ", ".join(
            f"{lang}: {v['passed']}/{v['total']}" for lang, v in s["by_language"].items()
        )
        lines.append(f"- {route}: {parts}")
    lines += ["", "### Route distribution", ""]
    for route, s in routes.items():
        dist = ", ".join(f"{k}={v}" for k, v in s["route_distribution"].items())
        lines.append(f"- {route}: {dist}")
    lines += ["", "### Retrieval page recall@k (gold page among top-k retrieved chunks)", ""]
    for route, s in routes.items():
        r = s["retrieval"]
        parts = ", ".join(f"{k}={v:.0%}" for k, v in r["recall"].items())
        lines.append(f"- {route} ({r['cases']} cases): {parts}")
    lines += ["", "### Retrieval page recall@k (gold page among top-k distinct pages)", ""]
    for route, s in routes.items():
        r = s["retrieval"]
        parts = ", ".join(f"{k}={v:.0%}" for k, v in r["page_recall"].items())
        lines.append(f"- {route} ({r['cases']} cases): {parts}")
    lines += ["", "## Case matrix", ""]
    route_names = list(runs)
    lines.append("| case | class | lang | " + " | ".join(route_names) + " |")
    lines.append("|---|---|---|" + "---|" * len(route_names))
    keys = list(dict.fromkeys((r.case_id, r.language) for rs in runs.values() for r in rs))
    index = {(r.route, r.case_id, r.language): r for rs in runs.values() for r in rs}
    for case_id, lang in keys:
        cells = []
        for route in route_names:
            run = index.get((route, case_id, lang))
            cells.append(
                "-"
                if run is None
                else f"{'PASS' if run.judgement.passed else 'FAIL'} ({run.route_label})"
            )
        lines.append(
            f"| {case_id} | {cases[case_id].case_class} | {lang} | " + " | ".join(cells) + " |"
        )
    lines += ["", "## Failures", ""]
    for route, rs in runs.items():
        failed = [r for r in rs if not r.judgement.passed and r.case_class != "known-gap"]
        lines += [f"### {route} ({len(failed)})", ""]
        for run in failed:
            lines += _failure_lines(run, cases[run.case_id])
    lines += ["", "## Known gaps (reported, not scored)", ""]
    if known_gaps:
        lines += [f"- `{g['case_id']}` — {g['detail']}" for g in known_gaps]
        for route, rs in runs.items():
            for run in rs:
                if run.case_class == "known-gap":
                    status = "PASS" if run.judgement.passed else "FAIL"
                    lines.append(f"  - {route} `{run.case_id}` ({run.language}): {status}")
    else:
        lines.append("- none in this gold version")
    lines += ["", "## Skipped", ""]
    lines += [f"- `{s['case_id']}` ({s['case_class']}): {s['reason']}" for s in skipped] or [
        "- none"
    ]
    return "\n".join(lines) + "\n"


def _failure_lines(run: CaseRun, case: GoldCase) -> list[str]:
    source_pages = sorted({p for p in (_page_of(_snippet_locator(s)) for s in run.sources) if p})
    lines = [
        f"- **{run.case_id}** ({run.case_class}, {run.language}, route={run.route_label}) — "
        f"{run.judgement.reason}",
        f"  - Q: {run.question}",
        f"  - expected: {_expected_text(case)}",
        f"  - answer: {_clip(run.answer_plain)}",
        f"  - source pages: {source_pages or '-'}; retrieved pages (ranked): "
        f"{run.retrieved_pages[:10] or '-'}",
    ]
    for claim in run.judgement.claims:
        lines.append(
            f"  - claim {' | '.join(claim.anchors)}: content={'Y' if claim.content_hit else 'N'} "
            f"page={'Y' if claim.page_hit else 'N'}"
        )
    if run.judgement.forbidden_hits:
        lines.append(f"  - forbidden numbers in answer: {list(run.judgement.forbidden_hits)}")
    return lines
