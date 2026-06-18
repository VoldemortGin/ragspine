"""Q&A 评测闭环 harness：四命门指标 + 基线门禁（docs/adr/0006-quality-bar-invariants-and-benchmark.md）。

四个命门指标分别报告，绝不合并成笼统 pass rate：
    ①数字准确率（NUMERIC_ACCURACY）：exact match——数值+单位与期望完全一致（结构化通路）。
    ②citation validity（CITATION_VALIDITY）：血缘/来源与期望一致；"答案对+来源错"判 fail。
        结构化：tool 返回的 source 与期望逐字段相等，且来源文件名出现在回答文本里；
        叙事：期望来源文档出现在 sources 列表且文件名出现在回答文本里。
    ③refusal appropriateness（REFUSAL_APPROPRIATENESS）：双向——该拒答时拒答（KB 无数据
        → not_found），不该拒答时不拒答（有数据却答"查不到"同样 fail）。
    ④clarification appropriateness（CLARIFICATION_APPROPRIATENESS）：双向——该澄清时
        澄清（实际 mode == 期望 mode），完整问题不许反问（期望 none 实际 ask_first 即 fail）。

编造数字检测（fabrication，单列报告，目标 0）：拒答类 case 的回答中，剥离期间类
数字（FY2024 / 2025H1 / 2025Q1 / 2030年 等——合法拒答文本必然回显期间）后，
出现任何其余数字即判 fabrication。

双模式（全链路零网络、零真实 LLM）：
    tool-direct —— 绕过 LLM：intent 解析 → 澄清网关 → query_metric / 叙事检索直接执行，
        完全确定性，度量"管道本身"的上限；
    agent —— answer_question + MockProvider 注入，连编排层硬约束一起测。

golden set 约定（data/golden/qa_golden_set.jsonl，git 版本化，置于 data/golden/ 并经 .gitignore !data/golden/ 强制跟踪）：
    每行一个 case：id / question / case_type / expected / tags / reference_date。
    expected 统一含 clarification（none|ask_first|answer_with_assumptions）与 refuse（bool）；
    numeric、composite 另含 value+unit+source；narrative、composite 另含 narrative_doc。

基线门禁仿 src/ragspine/eval/extraction_eval 模式：基线存 data/golden/qa_baseline.json（按 mode 分键），
compare_to_baseline 以「任一命门指标 pass_rate 低于基线，或 fabrication_count 高于基线」
为 gate fail；首次运行由 CLI 生成基线。
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from ragspine.agent.agent import NarrativeRetriever, answer_question
from ragspine.agent.intent import (
    CLARIFY_ANSWER_WITH_ASSUMPTIONS,
    CLARIFY_ASK_FIRST,
    CLARIFY_NONE,
    CLARIFY_OUT_OF_SCOPE_ENTITY,
    ROUTE_COMPOSITE,
    ROUTE_NARRATIVE,
    ROUTE_STRUCTURED,
    clarify_scope,
    parse_intent,
)
from ragspine.agent.llm_provider import MockProvider
from ragspine.agent.query_tools import execute_query_metric
from ragspine.common.company_profile import load_company_profile
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta, chunk_document
from ragspine.retrieval.link.narrative_link import (
    build_narrative_retriever,
)
from ragspine.storage.fact_store import Fact, FactStore

# 四命门指标名（基线/报告的键，一处定义）。
NUMERIC_ACCURACY = "numeric_accuracy"
CITATION_VALIDITY = "citation_validity"
REFUSAL_APPROPRIATENESS = "refusal_appropriateness"
CLARIFICATION_APPROPRIATENESS = "clarification_appropriateness"
GATE_METRICS = (
    NUMERIC_ACCURACY,
    CITATION_VALIDITY,
    REFUSAL_APPROPRIATENESS,
    CLARIFICATION_APPROPRIATENESS,
)
# 编造数字（单列，不在四命门 dict 里合并报告）。
FABRICATION = "fabrication"

EVAL_MODES = ("tool", "agent")

_CASE_TYPES = ("numeric", "clarification", "refusal", "narrative", "composite")
_CLARIFICATION_MODES = (
    CLARIFY_NONE, CLARIFY_ASK_FIRST, CLARIFY_ANSWER_WITH_ASSUMPTIONS
)

# 期间类数字（剥离后再查编造）：FY2024 / 2025H1 / 2025Q1 / 2030年 / 2024 年 上半年 等。
# 年份限定 19xx/20xx：9999、1234 这类四位数不是合法期间，必须按编造数字上报。
# 保留为 byte-pin 锚 + 向后兼容：detect_fabricated_numbers 不再直接用它（改读
# _PROFILE 的 temporal 维 fabrication_whitelist_regex），但其 pattern 必须与该字面逐字节相同。
_PERIOD_TOKEN_RE = re.compile(
    r"(?:FY\s*)?(?:19|20)\d{2}\s*年?\s*(?:H\s*[12]|Q\s*[1-4]|上半年|下半年)?",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

# 反编造检查的活跃 profile（call-time 读取：monkeypatch 本模块 _PROFILE 即生效，
# 随 _activate_acme/lab profile 切换而切换期间白名单）。
_PROFILE = load_company_profile()


def _fabrication_whitelist_re() -> re.Pattern[str] | None:
    """活跃 profile 的反编造白名单正则：取第一个 whitelist_in_fabrication_check 且
    fabrication_whitelist_regex 非空的维（仅 temporal 维设此显式字面）。都不满足 → None
    （无 temporal 维则不剥离任何数字，最严格）。call-time 读模块全局 _PROFILE。"""
    for dim in _PROFILE.dimensions:
        if dim.whitelist_in_fabrication_check and dim.fabrication_whitelist_regex:
            return re.compile(dim.fabrication_whitelist_regex, re.IGNORECASE)
    return None


# ---------------------------------------------------------------------------
# golden set：数据结构 + 加载校验
# ---------------------------------------------------------------------------

@dataclass
class GoldenCase:
    """golden set 中的一条评测用例。

    字段：
        id:             全集唯一标识。
        question:       用户问法（中英/混合）。
        case_type:      'numeric'|'clarification'|'refusal'|'narrative'|'composite'。
        expected:       期望行为（见模块 docstring 的约定）。
        tags:           分层标签 {topic, scope, qtype}（指标按此分层细分）。
        reference_date: 相对期间换算的固定基准日（保证"去年"等可复现）。
    """

    id: str
    question: str
    case_type: str
    expected: dict[str, object]
    tags: dict[str, object]
    reference_date: date


def _validate_case(record: dict[str, object], lineno: int) -> GoldenCase:
    """单条记录的结构校验；不合法抛 ValueError（带行号与 id 便于定位）。"""
    where = f"golden set 第 {lineno} 行（id={record.get('id')!r}）"
    for key in ("id", "question", "case_type", "expected", "tags", "reference_date"):
        if key not in record:
            raise ValueError(f"{where}：缺少必填字段 {key!r}")
    if not str(record["question"]).strip():
        raise ValueError(f"{where}：question 不能为空")
    case_type = record["case_type"]
    if case_type not in _CASE_TYPES:
        raise ValueError(f"{where}：未知 case_type {case_type!r}")

    tags = record["tags"]
    if not isinstance(tags, dict) or not {"topic", "scope", "qtype"} <= set(tags):
        raise ValueError(f"{where}：tags 必须含 topic/scope/qtype")

    expected = record["expected"]
    if not isinstance(expected, dict):
        raise ValueError(f"{where}：expected 必须是对象")
    if expected.get("clarification") not in _CLARIFICATION_MODES:
        raise ValueError(f"{where}：expected.clarification 必须是 {_CLARIFICATION_MODES}")
    if not isinstance(expected.get("refuse"), bool):
        raise ValueError(f"{where}：expected.refuse 必须是 bool")

    if case_type in ("numeric", "composite"):
        if not isinstance(expected.get("value"), (int, float)):
            raise ValueError(f"{where}：{case_type} 类必须给 expected.value（数值）")
        if not expected.get("unit"):
            raise ValueError(f"{where}：{case_type} 类必须给 expected.unit")
        source = expected.get("source")
        if not isinstance(source, dict) or not {"doc", "locator"} <= set(source):
            raise ValueError(f"{where}：{case_type} 类必须给 expected.source{{doc,locator}}")
    if case_type in ("narrative", "composite") and not expected.get("narrative_doc"):
        raise ValueError(f"{where}：{case_type} 类必须给 expected.narrative_doc")
    if case_type == "refusal" and expected["refuse"] is not True:
        raise ValueError(f"{where}：refusal 类的 expected.refuse 必须为 true")
    if case_type == "clarification" and expected["clarification"] == CLARIFY_NONE:
        raise ValueError(f"{where}：clarification 类的期望模式不能是 none")
    if "value" in expected and ("unit" not in expected or "source" not in expected):
        raise ValueError(f"{where}：给了 value 就必须同时给 unit 与 source")

    try:
        ref = date.fromisoformat(str(record["reference_date"]))
    except ValueError as exc:
        raise ValueError(f"{where}：reference_date 不是合法 ISO 日期") from exc

    return GoldenCase(
        id=str(record["id"]), question=str(record["question"]),
        case_type=case_type, expected=expected, tags=tags, reference_date=ref,
    )


def load_golden_set(path: str | Path) -> list[GoldenCase]:
    """加载并校验 golden set（JSONL）；任何一条不合法即整体拒绝（ValueError）。"""
    cases: list[GoldenCase] = []
    seen_ids: set[str] = set()
    for lineno, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"golden set 第 {lineno} 行不是合法 JSON") from exc
        case = _validate_case(record, lineno)
        if case.id in seen_ids:
            raise ValueError(f"golden set 第 {lineno} 行：id {case.id!r} 重复")
        seen_ids.add(case.id)
        cases.append(case)
    return cases


# ---------------------------------------------------------------------------
# 配套合成 KB：与 golden set 严格对齐的事实表 + 叙事块库（确定性、可幂等重建）
# ---------------------------------------------------------------------------

_DOC_R25 = "ACME_FY2025_Results.pptx"
_DOC_R24 = "ACME_FY2024_Results.pptx"
_DOC_I25 = "ACME_2025_Interim_Results.pptx"

# (metric, entity, geography, channel, period_type, period, value, unit, doc, locator)
_EVAL_FACT_ROWS: tuple[
    tuple[str, str, str, str, str, str, float, str, str, str], ...
] = (
    ("REVENUE", "ACME_GROUP", "ASIA", "TOTAL", "FY", "2025", 4500.0, "USD_M",
     _DOC_R25, "slide=3,table=1,row=2,col=3"),
    ("NEWSALES", "ACME_GROUP", "ASIA", "TOTAL", "FY", "2025", 8180.0, "USD_M",
     _DOC_R25, "slide=3,table=1,row=3,col=3"),  # 8.18b 量级陷阱
    ("PROFIT", "ACME_GROUP", "ASIA", "TOTAL", "FY", "2025", 6605.0, "USD_M",
     _DOC_R25, "slide=4,table=1,row=2,col=3"),
    ("ROE", "ACME_GROUP", "ASIA", "TOTAL", "FY", "2025", 14.8, "PCT",
     _DOC_R25, "slide=4,table=1,row=5,col=3"),
    ("REVENUE", "ACME_HK", "HK", "TOTAL", "FY", "2025", 1702.0, "USD_M",
     _DOC_R25, "slide=5,table=1,row=2,col=3"),
    ("REVENUE", "ACME_HK", "HK", "AGENCY", "FY", "2025", 1200.0, "USD_M",
     _DOC_R25, "slide=5,table=2,row=2,col=2"),
    ("NEWSALES", "ACME_HK", "HK", "TOTAL", "FY", "2025", 3104.0, "USD_M",
     _DOC_R25, "slide=5,table=1,row=3,col=3"),  # 与 2025H1 构成 FY/HY 陷阱对
    ("REVENUE", "ACME_CN", "CN", "TOTAL", "FY", "2025", 1320.0, "USD_M",
     _DOC_R25, "slide=6,table=1,row=2,col=3"),
    ("PROFIT", "ACME_CN", "CN", "TOTAL", "FY", "2025", 990.0, "USD_M",
     _DOC_R25, "slide=6,table=1,row=4,col=3"),
    ("REVENUE", "ACME_GROUP", "ASIA", "TOTAL", "FY", "2024", 4012.0, "USD_M",
     _DOC_R24, "slide=3,table=1,row=2,col=3"),
    ("REVENUE", "ACME_HK", "HK", "TOTAL", "FY", "2024", 1538.0, "USD_M",
     _DOC_R24, "slide=5,table=1,row=2,col=3"),
    ("REVENUE", "ACME_HK", "HK", "TOTAL", "HY", "2025H1", 818.0, "USD_M",
     _DOC_I25, "slide=4,table=1,row=2,col=2"),  # 818m vs 8.18b 陷阱值
    ("NEWSALES", "ACME_HK", "HK", "TOTAL", "HY", "2025H1", 1520.0, "USD_M",
     _DOC_I25, "slide=4,table=1,row=3,col=2"),
)

# (DocumentMeta, 正文)：叙事块库。CN QBR 含双语句以支撑英文叙事问法的 BM25 召回。
_EVAL_NARRATIVE_DOCS: tuple[tuple[DocumentMeta, str], ...] = (
    (
        DocumentMeta(doc_id="HK_QBR_2025Q4.pptx", title="HK QBR 2025Q4",
                     topic="FIN", entity="ACME_HK", geography="HK",
                     period="2025", language="zh"),
        "香港 REVENUE 下降主因是 MCV 客群收缩与银保渠道调整，趋势上短期仍有压力。",
    ),
    (
        DocumentMeta(doc_id="CN_QBR_2025.pptx", title="CN QBR 2025",
                     topic="FIN", entity="ACME_CN", geography="CN",
                     period="2025", language="zh"),
        "中国 REVENUE 增长由代理人产能提升与银保渠道扩张驱动"
        "（ACME China growth driver: agency productivity and bancassurance expansion）。",
    ),
    (
        DocumentMeta(doc_id="GROUP_QBR_2025.pptx", title="Group QBR 2025",
                     topic="FIN", entity="ACME_GROUP", geography="ASIA",
                     period="2025", language="zh"),
        "集团 NEWSALES 增长主因是香港与中国双引擎发力，银保渠道扩张带来新单增长。",
    ),
    (
        DocumentMeta(doc_id="REG_WATCH_HK.pptx", title="HK Regulatory Watch",
                     topic="REG", entity="ACME_HK", geography="HK", language="zh"),
        "香港监管动态：MPFA 强积金新规要求披露管理费，IA 加强销售流程审查。",
    ),
    (
        DocumentMeta(doc_id="REG_WATCH_CN.pptx", title="CN Regulatory Watch",
                     topic="REG", entity="ACME_CN", geography="CN", language="zh"),
        "中国监管动态：金融监管总局发布分红险新规，强化销售行为管理。",
    ),
)


def build_eval_kb(kb_dir: str | Path) -> tuple[Path, Path]:
    """在 kb_dir 下确定性构建评测 KB，返回 (fact_db, chunk_db) 路径。

    幂等：facts 走 upsert（唯一键覆盖），chunks 走同 doc 版本替换，
    重复调用后活跃数据集与首次构建完全一致。
    """
    kb_dir = Path(kb_dir)
    kb_dir.mkdir(parents=True, exist_ok=True)
    fact_db = kb_dir / "qa_eval_facts.db"
    chunk_db = kb_dir / "qa_eval_chunks.db"

    store = FactStore(fact_db)
    store.init_schema()
    store.upsert_facts([Fact(*row) for row in _EVAL_FACT_ROWS])
    store.close()

    chunk_store = ChunkStore(chunk_db)
    chunk_store.init_schema()
    for meta, text in _EVAL_NARRATIVE_DOCS:
        chunk_store.replace_doc_chunks(meta.doc_id, chunk_document(text, meta))
    chunk_store.close()

    return fact_db, chunk_db


# ---------------------------------------------------------------------------
# 单 case 执行：tool-direct / agent 两种 runner，归一为 CaseOutcome
# ---------------------------------------------------------------------------

@dataclass
class CaseOutcome:
    """一条 case 的实际行为（两种模式归一后的观测面，供四命门指标判定）。

    字段：
        clarification_mode: 实际澄清模式（none/ask_first/answer_with_assumptions）。
        answer:             最终回答文本（citation 呈现与编造检测的对象）。
        found_value/unit/source: 结构化通路命中的确定值与血缘（未命中为 None）。
        refused:            拒答信号（not_found/unrecognized 且无命中，或叙事零检索）。
        sources:            回答附带的全部来源 [{doc, locator}]。
        tool_statuses:      结构化工具调用的状态序列（诊断用）。
        route:              意图路由（诊断用）。
    """

    case_id: str
    clarification_mode: str = CLARIFY_NONE
    answer: str = ""
    found_value: float | None = None
    found_unit: str | None = None
    found_source: dict[str, object] | None = None
    refused: bool = False
    sources: list[dict[str, object]] = field(default_factory=list)
    tool_statuses: list[str] = field(default_factory=list)
    route: str = ""


def _period_param(period: tuple[str, str] | None) -> str:
    """意图槽位 (period_type, value) → query_metric 的 period 字符串。"""
    if period is None:
        return ""
    period_type, value = period
    return f"FY{value}" if period_type == "FY" else value


def _snippet_source(snippet: dict[str, object]) -> dict[str, object]:
    """检索 snippet → {doc, locator}（与 agent 层的字段兼容规则一致）。"""
    doc = (
        snippet.get("doc_id") or snippet.get("source_doc_id")
        or snippet.get("doc") or ""
    )
    locator = snippet.get("locator") or snippet.get("source_locator") or ""
    return {"doc": doc, "locator": locator}


def run_case_tool_direct(
    case: GoldenCase, store: FactStore, retriever: NarrativeRetriever
) -> CaseOutcome:
    """tool-direct 模式：intent 解析 → 澄清网关 → 工具/检索直接执行（零 LLM）。"""
    ref = case.reference_date
    intent = parse_intent(case.question, reference_date=ref)
    clar = clarify_scope(intent, reference_date=ref)
    out = CaseOutcome(case_id=case.id, route=intent.route,
                      clarification_mode=clar.mode)
    # 外部/竞品实体越权：最前置拒答，不查 tool/检索（与 answer_question 保持一致）。
    if clar.mode == CLARIFY_OUT_OF_SCOPE_ENTITY:
        out.answer = clar.question or ""
        out.refused = True
        return out
    if clar.mode == CLARIFY_ASK_FIRST:
        out.answer = clar.question or ""
        return out

    parts: list[str] = []
    if intent.route in (ROUTE_STRUCTURED, ROUTE_COMPOSITE):
        entity = intent.entity or clar.assumed_slots.get("entity") or ""
        period = intent.period or clar.assumed_slots.get("period")
        result = execute_query_metric(
            store, metric=intent.metric or "", entity=entity,
            period=_period_param(period), channel=intent.channel,
        )
        out.tool_statuses.append(cast("str", result["status"]))
        if result["status"] == "found":
            source = dict(cast("dict[str, object]", result["source"]))
            out.found_value = cast("float", result["value"])
            out.found_unit = cast("str", result["unit"])
            out.found_source = source
            out.sources.append(source)
            parts.append(
                f"{result['entity']} {result['period_type']}{result['period']} "
                f"{result['metric_code']} 为 {result['value']:g} {result['unit']}"
                f"（来源：{source['doc']} · {source['locator']}）"
            )
        elif result["status"] == "not_found":
            out.refused = True
            norm = cast("dict[str, object]", result["normalized"])
            parts.append(
                f"查不到：{norm['metric_code']} / {norm['entity']} / "
                f"{norm['period']}（渠道 {norm['channel']}）未在事实表中找到，"
                "不提供任何推测数字。"
            )
        else:
            out.refused = True
            parts.append(
                f"无法识别参数 {result.get('param')}：'{result.get('raw')}'。"
            )

    if intent.route in (ROUTE_NARRATIVE, ROUTE_COMPOSITE):
        filters: dict[str, str] = {}
        if intent.entity:
            filters["entity"] = intent.entity
        if intent.period:
            filters["period"] = intent.period[1]
        snippets = retriever.retrieve(
            case.question, filters=filters or None, top_k=50
        )
        if snippets:
            for snippet in snippets:
                source = _snippet_source(snippet)
                out.sources.append(source)
                parts.append(
                    f"{snippet.get('text', '')}"
                    f"（来源：{source['doc']} {source['locator']}）"
                )
        elif intent.route == ROUTE_NARRATIVE:
            out.refused = True
            parts.append("未检索到与该问题相关的资料，无法基于现有知识库作答。")
        else:
            parts.append("归因部分未检索到相关资料。")

    out.answer = "\n".join(parts)
    return out


def run_case_agent(
    case: GoldenCase, store: FactStore, retriever: NarrativeRetriever
) -> CaseOutcome:
    """agent 模式：answer_question + MockProvider（确定性脚本化 tool use 循环）。"""
    provider = MockProvider(reference_date=case.reference_date)
    result = answer_question(
        case.question, store, provider,
        reference_date=case.reference_date, narrative_retriever=retriever,
    )
    clar_mode = result.clarification.mode if result.clarification else CLARIFY_NONE
    out = CaseOutcome(
        case_id=case.id, route=result.route, clarification_mode=clar_mode,
        answer=result.answer, sources=[dict(s) for s in result.sources],
        tool_statuses=[cast("str", r.get("status", "")) for r in result.tool_results],
    )
    found = [r for r in result.tool_results if r.get("status") == "found"]
    if found:
        out.found_value = cast("float", found[0]["value"])
        out.found_unit = cast("str", found[0]["unit"])
        out.found_source = dict(cast("dict[str, object]", found[0]["source"]))

    # 外部/竞品实体越权无 tool 调用也无检索，refused 不会被下方推导命中——显式置位，
    # 保证与 tool-direct 模式一致（answer_question 已最前置拒答，不输出 home 数字）。
    if clar_mode == CLARIFY_OUT_OF_SCOPE_ENTITY:
        out.refused = True
    elif clar_mode != CLARIFY_ASK_FIRST:
        if result.route in (ROUTE_STRUCTURED, ROUTE_COMPOSITE):
            if not found and any(
                s in ("not_found", "unrecognized_param") for s in out.tool_statuses
            ):
                out.refused = True
        if result.route == ROUTE_NARRATIVE and not result.sources:
            out.refused = True
    return out


# ---------------------------------------------------------------------------
# 四命门指标计算 + 编造检测
# ---------------------------------------------------------------------------

def detect_fabricated_numbers(answer: str) -> list[str]:
    """拒答回答中的编造数字：剥离期间类数字后，余下的任何数字均视为编造。

    期间白名单正则来自活跃 profile 的 temporal 维（call-time 读 _PROFILE）；无 temporal
    维（如 lab 域）则不剥离任何数字——拒答答案里每个数字都被标记（最严格）。"""
    whitelist = _fabrication_whitelist_re()
    residual = whitelist.sub(" ", answer) if whitelist is not None else answer
    return _NUMBER_RE.findall(residual)


@dataclass
class GateMetric:
    """单命门指标结果（结构仿 extraction_eval.ChannelMetric，增加分层细分）。

    字段：
        name:      指标名（GATE_METRICS 之一或 FABRICATION）。
        total:     应评样本数。
        passed:    通过数。
        pass_rate: passed / total（total=0 时约定为 1.0）。
        failures:  失败明细 [{id, expected, actual}]。
        by_tag:    分层细分 {tag键: {tag值: {total, passed}}}。
    """

    name: str
    total: int = 0
    passed: int = 0
    pass_rate: float = 0.0
    failures: list[dict[str, object]] = field(default_factory=list)
    by_tag: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)

    def tally(
        self, case: GoldenCase, ok: bool, expected: object, actual: object
    ) -> None:
        """记一个样本：总数/通过/失败明细/按 tags 分层。"""
        self.total += 1
        if ok:
            self.passed += 1
        else:
            self.failures.append(
                {"id": case.id, "expected": expected, "actual": actual}
            )
        for tag_key, tag_val in case.tags.items():
            bucket = self.by_tag.setdefault(tag_key, {}).setdefault(
                str(tag_val), {"total": 0, "passed": 0}
            )
            bucket["total"] += 1
            bucket["passed"] += int(ok)

    def finalize(self) -> None:
        self.pass_rate = 1.0 if self.total == 0 else self.passed / self.total

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "total": self.total,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "failures": self.failures,
            "by_tag": self.by_tag,
        }


@dataclass
class QAEvalReport:
    """一次评测的全量结果：四命门指标 + 编造单列（JSON 可序列化）。"""

    mode: str
    n_cases: int
    metrics: dict[str, GateMetric] = field(default_factory=dict)
    fabrication: GateMetric = field(
        default_factory=lambda: GateMetric(name=FABRICATION)
    )

    @property
    def fabrication_count(self) -> int:
        """出现编造数字的拒答 case 数（目标恒为 0）。"""
        return self.fabrication.total - self.fabrication.passed

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "n_cases": self.n_cases,
            "metrics": {name: m.to_dict() for name, m in self.metrics.items()},
            "fabrication": self.fabrication.to_dict(),
            "fabrication_count": self.fabrication_count,
        }


def evaluate(
    cases: list[GoldenCase],
    outcomes: dict[str, CaseOutcome],
    mode: str = "tool",
) -> QAEvalReport:
    """按四命门规则比对期望与实际，产出分指标报告（绝不合并成单一 pass rate）。"""
    report = QAEvalReport(mode=mode, n_cases=len(cases))
    report.metrics = {name: GateMetric(name=name) for name in GATE_METRICS}

    for case in cases:
        out = outcomes[case.id]
        exp = case.expected

        # ① 数字准确率：数值 + 单位 exact match。
        if "value" in exp:
            ok = out.found_value == exp["value"] and out.found_unit == exp["unit"]
            report.metrics[NUMERIC_ACCURACY].tally(
                case, ok,
                expected=f"{exp['value']} {exp['unit']}",
                actual=f"{out.found_value} {out.found_unit}",
            )

        # ② citation validity："答案对+来源错"判 fail。
        if "source" in exp or "narrative_doc" in exp:
            ok = True
            actual_bits: list[str] = []
            if "source" in exp:
                exp_source = cast("dict[str, str]", exp["source"])
                ok = (
                    out.found_source == exp_source
                    and exp_source["doc"] in out.answer
                )
                actual_bits.append(f"structured_source={out.found_source}")
            if "narrative_doc" in exp:
                doc = cast("str", exp["narrative_doc"])
                ok = ok and any(s.get("doc") == doc for s in out.sources) \
                    and doc in out.answer
                actual_bits.append(
                    f"sources={[s.get('doc') for s in out.sources]}"
                )
            report.metrics[CITATION_VALIDITY].tally(
                case, ok,
                expected={k: exp[k] for k in ("source", "narrative_doc") if k in exp},
                actual="; ".join(actual_bits),
            )

        # ③ refusal appropriateness：双向。
        if exp["refuse"]:
            ok = out.refused and out.found_value is None
        else:
            ok = not out.refused
        report.metrics[REFUSAL_APPROPRIATENESS].tally(
            case, ok,
            expected="拒答" if exp["refuse"] else "正常作答",
            actual="拒答" if out.refused else "作答",
        )

        # ④ clarification appropriateness：双向（完整问题不许反问）。
        ok = out.clarification_mode == exp["clarification"]
        report.metrics[CLARIFICATION_APPROPRIATENESS].tally(
            case, ok, expected=exp["clarification"], actual=out.clarification_mode
        )

        # 编造数字（仅拒答类，单列报告）。
        if exp["refuse"]:
            fabricated = detect_fabricated_numbers(out.answer)
            report.fabrication.tally(
                case, not fabricated,
                expected="拒答回答不含任何数字（期间除外）",
                actual=f"出现数字 {fabricated}" if fabricated else "无",
            )

    for metric in report.metrics.values():
        metric.finalize()
    report.fabrication.finalize()
    return report


# ---------------------------------------------------------------------------
# 基线门禁（仿 extraction_eval.compare_to_baseline：任一退化即 fail）
# ---------------------------------------------------------------------------

@dataclass
class BaselineComparison:
    """与基线比对结果：任一命门指标退化（或编造增加）即 passed=False。"""

    passed: bool = False
    regressions: list[dict[str, object]] = field(default_factory=list)


def make_baseline_entry(report: QAEvalReport) -> dict[str, object]:
    """从报告生成基线条目（按 mode 存入 data/golden/qa_baseline.json）。"""
    return {
        "metrics": {name: m.pass_rate for name, m in report.metrics.items()},
        "fabrication_count": report.fabrication_count,
        "n_cases": report.n_cases,
    }


def compare_to_baseline(
    report: QAEvalReport, baseline: dict[str, object]
) -> BaselineComparison:
    """任一命门指标 pass_rate 低于基线、或 fabrication_count 高于基线 → gate fail。

    只检查 baseline.metrics 中列出的指标；恰好等于基线视为通过。
    """
    regressions: list[dict[str, object]] = []
    baseline_metrics = cast("dict[str, float]", baseline.get("metrics", {}))
    for name, threshold in baseline_metrics.items():
        metric = report.metrics.get(name)
        if metric is None:
            continue
        if metric.pass_rate < threshold:
            regressions.append({
                "metric": name,
                "baseline": threshold,
                "current": metric.pass_rate,
                "delta": metric.pass_rate - threshold,
            })
    baseline_fab = cast("int", baseline.get("fabrication_count", 0))
    if report.fabrication_count > baseline_fab:
        regressions.append({
            "metric": FABRICATION,
            "baseline": baseline_fab,
            "current": report.fabrication_count,
            "delta": report.fabrication_count - baseline_fab,
        })
    return BaselineComparison(passed=not regressions, regressions=regressions)


# ---------------------------------------------------------------------------
# 全集执行入口
# ---------------------------------------------------------------------------

def run_qa_eval(
    golden_path: str | Path,
    mode: str = "tool",
    kb_dir: str | Path | None = None,
) -> QAEvalReport:
    """加载 golden set → 构建合成 KB → 双模式之一逐条执行 → 四命门报告。

    kb_dir 缺省时用临时目录（跑完即清）；传入则在其下建库（可复查中间产物）。
    """
    if mode not in EVAL_MODES:
        raise ValueError(f"未知评测模式 {mode!r}，可选：{EVAL_MODES}")
    cases = load_golden_set(golden_path)
    runner = run_case_tool_direct if mode == "tool" else run_case_agent

    with TemporaryDirectory(prefix="qa_eval_kb_") as tmp:
        fact_db, chunk_db = build_eval_kb(kb_dir if kb_dir is not None else tmp)
        store = FactStore(fact_db)
        retriever, chunk_store = build_narrative_retriever(chunk_db)
        try:
            outcomes = {c.id: runner(c, store, retriever) for c in cases}
        finally:
            store.close()
            chunk_store.close()
    return evaluate(cases, outcomes, mode=mode)
