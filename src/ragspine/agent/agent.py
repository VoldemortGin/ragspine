"""Agent 编排层：意图解析 → 澄清网关 → 三路分流 → tool use 循环 → 合成回答。

硬约束（编排层兜底，不依赖模型自觉）：
- not_found / unrecognized：最终回答由编排层确定性生成，明确说查不到/无法识别，
  即使模型试图编造数字也会被拦截改写。
- found：回答必带数据血缘（source_doc_id + source_locator），模型漏写则补上。

叙事通路由另一条线并行开发：这里只定义 NarrativeRetriever 协议（duck-typed），
检索实现经参数注入，绝不 import 另一条线的模块。
"""

import json
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, cast, runtime_checkable

from corespine import Usage

from ragspine.agent.decompose import ROUTE_DECOMPOSED, QueryDecomposer
from ragspine.agent.intent import (
    CLARIFY_ANSWER_WITH_ASSUMPTIONS,
    CLARIFY_ASK_FIRST,
    CLARIFY_OUT_OF_SCOPE_ENTITY,
    ROUTE_COMPOSITE,
    ROUTE_NARRATIVE,
    ClarificationResult,
    IntentParser,
    ParsedIntent,
    RuleIntentParser,
    SubTask,
    clarify_scope,
    expand_subtasks,
)
from ragspine.agent.llm_provider import (
    NARRATIVE_PROMPT_PREFIX,
    LLMProvider,
    ProviderError,
)
from ragspine.agent.query_tools import QUERY_METRIC_TOOL_OPENAI, execute_query_metric
from ragspine.common.company_profile import load_company_profile
from ragspine.common.glossary import normalize_period, resolve_relative_period
from ragspine.common.observability import emit_trace, new_request_id
from ragspine.storage.fact_store import FactStore

MAX_TOOL_ITERATIONS = 5


def _usage_dict(usage: Usage | None) -> dict[str, int | None] | None:
    """corespine ChatCompletion.usage(prompt/completion)→ record_provider 期望的
    {input_tokens, output_tokens}（OpenAI prompt=input、completion=output）。"""
    if usage is None:
        return None
    return {"input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens}

# provider 失败时的诚实降级文案（结构化/叙事两路）：绝不含数字、绝不编造。
_DEGRADE_STRUCTURED = "AI 服务暂时不可用，未能完成本次查询，请稍后再试。"
_DEGRADE_NARRATIVE = "AI 服务暂时不可用，未能生成归因，请稍后再试。"

# home 公司 profile（系统 prompt 中的公司名由此派生，不硬编码 "ACME"）。
_PROFILE = load_company_profile()

_SYSTEM_PROMPT_TEMPLATE = (
    "你是 {company} 管理层经营洞察助手。回答财务/经营指标问题时必须调用 query_metric "
    "工具取确定值，绝不凭记忆报数字。今天是 {today}（相对期间据此换算，"
    "如去年={last_fy}）。工具返回 not_found 时必须明确告知查不到，绝不编造；"
    "返回 found 时回答必须附数据来源（文件+定位）。回答用中文，英文术语保留原文。"
)

# 叙事通路系统 prompt 模板（公司名同样由 profile 派生）。
_NARRATIVE_SYSTEM_PROMPT_TEMPLATE = (
    "你是 {company} 管理层经营洞察助手，只依据给定片段作答并标注来源。"
)


@runtime_checkable
class NarrativeRetriever(Protocol):
    """叙事检索协议（duck-typed）：另一条线的实现按此签名注入。"""

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]: ...


@dataclass
class AgentResult:
    """编排结果：最终回答 + 路由 + 澄清信息 + 工具结果 + 来源列表。"""

    answer: str
    route: str
    clarification: ClarificationResult | None = None
    tool_results: list[dict[str, object]] = field(default_factory=list)
    sources: list[dict[str, object]] = field(default_factory=list)


@dataclass
class _TraceCtx:
    """单次请求的可观测性采集器（仅承载非敏感元数据，绝不含答案正文/事实数值）。"""

    provider_error: bool = False
    provider_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    has_usage: bool = False
    chunk_ids: list[object] = field(default_factory=list)
    chunk_scores: list[object] = field(default_factory=list)

    def record_provider(
        self, seconds: float, usage: dict[str, int | None] | None
    ) -> None:
        self.provider_seconds += seconds
        if usage:
            self.has_usage = True
            self.input_tokens += usage.get("input_tokens") or 0
            self.output_tokens += usage.get("output_tokens") or 0


def _system_prompt(reference_date: date) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.format(
        company=_PROFILE.home_company_name,
        today=reference_date.isoformat(),
        last_fy=f"FY{reference_date.year - 1}",
    )


def _execute_tool(
    store: FactStore, tool_input: dict[str, object], reference_date: date
) -> dict[str, object]:
    """执行 query_metric：period 先试绝对归一，失败再按 reference_date 解析相对期间。"""
    period = str(tool_input.get("period", ""))
    if normalize_period(period) is None:
        resolved = resolve_relative_period(period, reference_date)
        if resolved is not None:
            period_type, value = resolved
            period = f"FY{value}" if period_type == "FY" else value
    return execute_query_metric(
        store,
        metric=str(tool_input.get("metric", "")),
        entity=str(tool_input.get("entity", "")),
        period=period,
        channel=str(tool_input.get("channel") or "TOTAL"),
    )


def _run_tool_loop(
    question: str,
    store: FactStore,
    provider: LLMProvider,
    reference_date: date,
    ctx: _TraceCtx,
) -> tuple[str, list[dict[str, object]]]:
    """标准 tool use 循环：user → (tool_use → tool_result)* → 最终文本。

    provider 网络/API 失败（ProviderError）→ 诚实降级：返回固定降级文案、空结果，
    绝不编造数字；其他异常（逻辑 bug）照常抛出，不被吞掉。
    """
    system = _system_prompt(reference_date)
    tools = [QUERY_METRIC_TOOL_OPENAI]
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]
    tool_results: list[dict[str, object]] = []

    final_text = ""
    for _ in range(MAX_TOOL_ITERATIONS):
        started = time.perf_counter()
        try:
            resp = provider.chat(messages, tools=tools)
        except ProviderError:
            ctx.provider_error = True
            ctx.record_provider(time.perf_counter() - started, None)
            return _DEGRADE_STRUCTURED, []
        ctx.record_provider(time.perf_counter() - started, _usage_dict(resp.usage))
        message = resp.choices[0].message
        final_text = message.content or ""
        if not message.tool_calls:
            break
        # 回传 assistant 的 tool_calls（OpenAI 形状），携工具结果继续下一轮。
        messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in message.tool_calls
            ],
        })
        for tc in message.tool_calls:
            args = cast("dict[str, object]", json.loads(tc.function.arguments))
            result = _execute_tool(store, args, reference_date)
            tool_results.append(result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    return final_text, tool_results


def _not_found_answer(result: dict[str, object]) -> str:
    raw_norm = result.get("normalized", {})
    norm = raw_norm if isinstance(raw_norm, dict) else {}
    return (
        f"查不到：{norm.get('metric_code')} / {norm.get('entity')} / "
        f"{norm.get('period')}（渠道 {norm.get('channel')}）未在事实表中找到。"
        "为避免误导，不提供任何推测数字；可尝试调整期间或实体后重问。"
    )


def _unrecognized_answer(result: dict[str, object]) -> str:
    return (
        f"无法识别参数 {result.get('param')}：'{result.get('raw')}'。"
        "请确认指标/实体/期间的说法后重试。"
    )


def _source_of(result: dict[str, object]) -> dict[str, object]:
    """取 found 结果的 source 子字典（缺失则 KeyError，与既有索引语义一致）。"""
    source = result["source"]
    return source if isinstance(source, dict) else {}


def _structured_answer(
    model_text: str, tool_results: list[dict[str, object]]
) -> tuple[str, list[dict[str, object]]]:
    """结构化回答的硬约束后处理：防编造 + 血缘补全。返回 (answer, sources)。"""
    found = [r for r in tool_results if r.get("status") == "found"]
    if found:
        # 确定性合成（反幻觉，审计 HIGH 修复）：found 路径的数字一律取自 fact 值，
        # 彻底弃用模型散文——否则 live-LLM 可在散文里夹带额外伪造数字（结构化通道命中
        # 时模型仍自由作答，MockProvider 测不到这种走私）。逐条 found 确定性列
        # 「实体 期间 指标（渠道）：值 单位（来源…）」，与 _multi_subtask_answer 同格式。
        sources = [_source_of(r) for r in found]
        lines: list[str] = []
        for r in found:
            source = _source_of(r)
            label = (
                f"{r['entity']} "
                f"{_period_label(str(r['period_type']), str(r['period']))} "
                f"{r['metric_code']}"
            )
            if r.get("channel") and r["channel"] != "TOTAL":
                label += f"（渠道 {r['channel']}）"
            # valid_as_of 存在时附「截至」业务时点。
            valid_as_of = r.get("valid_as_of")
            asof = f" · 截至 {valid_as_of}" if valid_as_of else ""
            lines.append(
                f"{label}：{r['value']:g} {r['unit']}"
                f"（来源：{source.get('doc')} · {source.get('locator')}{asof}）"
            )
        return "\n".join(lines), sources

    not_found = [r for r in tool_results if r.get("status") == "not_found"]
    if not_found:
        return _not_found_answer(not_found[0]), []

    unrecognized = [r for r in tool_results if r.get("status") == "unrecognized_param"]
    if unrecognized:
        return _unrecognized_answer(unrecognized[0]), []

    # 模型未调工具直接作答（如要求澄清）：原样返回，无来源
    return model_text, []


def _period_to_param(period: tuple[str, str]) -> str:
    """意图槽位 (period_type, value) → query_metric 的 period 字符串参数。"""
    period_type, value = period
    return f"FY{value}" if period_type == "FY" else value


def _period_label(period_type: str, period: str) -> str:
    """展示用期间标签：FY 加前缀，HY/QUARTER 的 period 自含年份直接用。"""
    return f"FY{period}" if period_type == "FY" else period


def _run_subtasks(
    subtasks: list[SubTask], store: FactStore
) -> list[dict[str, object]]:
    """确定性执行多个 query_metric 子任务（不经 LLM，参数已是受控代码）。"""
    return [
        execute_query_metric(
            store,
            metric=task.metric or "",
            entity=task.entity or "",
            period=_period_to_param(task.period) if task.period else "",
            channel=task.channel or "TOTAL",
        )
        for task in subtasks
    ]


def _multi_subtask_answer(
    tool_results: list[dict[str, object]],
) -> tuple[str, list[dict[str, object]]]:
    """多子任务对比式合成（确定性）：逐项列数+血缘；not_found 子项明确说查不到，
    绝不编造，也不拖垮其他子项。返回 (answer, sources)。"""
    lines: list[str] = []
    sources: list[dict[str, object]] = []
    found: list[dict[str, object]] = []
    for r in tool_results:
        if r.get("status") == "found":
            found.append(r)
            source = _source_of(r)
            sources.append(source)
            label = (
                f"{r['entity']} "
                f"{_period_label(str(r['period_type']), str(r['period']))} "
                f"{r['metric_code']}"
            )
            if r.get("channel") and r["channel"] != "TOTAL":
                label += f"（渠道 {r['channel']}）"
            lines.append(
                f"- {label}：{r['value']:g} {r['unit']}"
                f"（来源：{source.get('doc')} · {source.get('locator')}）"
            )
        elif r.get("status") == "not_found":
            raw_norm = r.get("normalized", {})
            norm = raw_norm if isinstance(raw_norm, dict) else {}
            lines.append(
                f"- {norm.get('entity')} {_period_label(str(norm.get('period_type', '')), str(norm.get('period', '')))} "
                f"{norm.get('metric_code')}：查不到（未在事实表中找到，不提供推测数字）"
            )
        else:  # unrecognized_param 或未知状态
            lines.append(
                f"- 无法识别参数 {r.get('param')}：'{r.get('raw')}'，该子项跳过。"
            )

    answer = "对比结果：\n" + "\n".join(lines)

    # 恰为两期可比（同指标/实体/渠道/单位、期间不同）时给出确定性差值
    if len(tool_results) == 2 and len(found) == 2:
        a, b = found
        same_scope = all(
            a[k] == b[k] for k in ("metric_code", "entity", "channel", "unit")
        )
        if same_scope and (a["period_type"], a["period"]) != (b["period_type"], b["period"]):
            a_value = cast(float, a["value"])
            b_value = cast(float, b["value"])
            diff = b_value - a_value
            delta = (
                f"对比：{_period_label(str(b['period_type']), str(b['period']))} 较 "
                f"{_period_label(str(a['period_type']), str(a['period']))} {diff:+g} {a['unit']}"
            )
            if a_value:
                delta += f"（{diff / a_value * 100:+.1f}%）"
            answer = f"{answer}\n{delta}"

    return answer, sources


def _snippet_text(snippet: dict[str, object]) -> str:
    return str(snippet.get("text") or snippet.get("content") or "")


def _snippet_source(snippet: dict[str, object]) -> dict[str, object]:
    doc = (
        snippet.get("doc_id") or snippet.get("source_doc_id")
        or snippet.get("doc") or ""
    )
    locator = snippet.get("locator") or snippet.get("source_locator") or ""
    return {"doc": doc, "locator": locator}


def _run_narrative(
    question: str,
    provider: LLMProvider,
    retriever: NarrativeRetriever | None,
    intent: ParsedIntent,
    ctx: _TraceCtx,
) -> tuple[str, list[dict[str, object]]]:
    """叙事通路：检索 → 合成 → 附来源。检索未接入/无结果时坦白降级；

    provider 失败（ProviderError）→ 诚实降级文案，不崩、不编造；其他异常照常抛出。
    """
    if retriever is None:
        return (
            "叙事检索通路尚未接入，暂时无法回答归因/监管/进展类问题；"
            "数字类问题可直接提问。", [],
        )

    filters: dict[str, str] = {}
    if intent.entity:
        filters["entity"] = intent.entity
    if intent.period:
        filters["period"] = intent.period[1]
    snippets = retriever.retrieve(question, filters=filters or None, top_k=50)
    if not snippets:
        return ("未检索到与该问题相关的资料，无法基于现有知识库作答。", [])

    # 仅采集非敏感元数据（chunk_id + 各通道分数），绝不采集 chunk 正文。
    ctx.chunk_ids = [s.get("chunk_id") for s in snippets if s.get("chunk_id")]
    ctx.chunk_scores = [s.get("scores") for s in snippets if s.get("scores")]

    sources = [_snippet_source(s) for s in snippets]
    body = "\n".join(
        f"[{i + 1}] {_snippet_text(s)}（来源：{src['doc']} {src['locator']}）"
        for i, (s, src) in enumerate(zip(snippets, sources, strict=True))
    )
    prompt = f"{NARRATIVE_PROMPT_PREFIX}。\n问题：{question}\n检索片段：\n{body}"
    started = time.perf_counter()
    try:
        resp = provider.chat([
            {"role": "system",
             "content": _NARRATIVE_SYSTEM_PROMPT_TEMPLATE.format(
                 company=_PROFILE.home_company_name)},
            {"role": "user", "content": prompt},
        ])
    except ProviderError:
        ctx.provider_error = True
        ctx.record_provider(time.perf_counter() - started, None)
        return _DEGRADE_NARRATIVE, []
    ctx.record_provider(time.perf_counter() - started, _usage_dict(resp.usage))
    answer = resp.choices[0].message.content or ""
    # 血缘兜底：来源文件名必须出现在回答里
    missing = [s for s in sources if s["doc"] and str(s["doc"]) not in answer]
    if missing:
        cite = "；".join(f"{s['doc']} {s['locator']}".strip() for s in missing)
        answer = f"{answer}\n（资料来源：{cite}）"
    return answer, sources


def _tool_status_counts(tool_results: list[dict[str, object]]) -> dict[str, int]:
    """统计 found / not_found / unrecognized 三态条数（trace 用，非数值）。"""
    counts = {"found": 0, "not_found": 0, "unrecognized": 0}
    for r in tool_results:
        status = r.get("status")
        if status == "found":
            counts["found"] += 1
        elif status == "not_found":
            counts["not_found"] += 1
        elif status == "unrecognized_param":
            counts["unrecognized"] += 1
    return counts


def _emit_request_trace(
    request_id: str,
    intent: ParsedIntent,
    clar: ClarificationResult,
    tool_results: list[dict[str, object]],
    ctx: _TraceCtx,
) -> None:
    """结束前发一条结构化 trace（仅非敏感元数据，日志按 Restricted 对待）。

    fabrication_guard_triggered：本次是否触发防编造强制改写——
    有工具结果但无任何 found（即落到 not_found/unrecognized 兜底改写）时为 True。
    """
    counts = _tool_status_counts(tool_results)
    fabrication_guard_triggered = bool(tool_results) and counts["found"] == 0
    fields: dict[str, object] = {
        "request_id": request_id,
        "route": intent.route,
        "metric": intent.metric,
        "entity": intent.entity,
        "period": intent.period,
        "channel": intent.channel,
        "external_entity": intent.external_entity,
        "clar_mode": clar.mode,
        "tool_status_counts": counts,
        "fabrication_guard_triggered": fabrication_guard_triggered,
        "provider_error": ctx.provider_error,
        "provider_seconds": round(ctx.provider_seconds, 6),
        "chunk_ids": ctx.chunk_ids,
        "chunk_scores": ctx.chunk_scores,
    }
    if ctx.has_usage:
        fields["token_usage"] = {
            "input_tokens": ctx.input_tokens,
            "output_tokens": ctx.output_tokens,
        }
    emit_trace(None, **fields)


def _answer_decomposed(
    subquestions: list[str],
    store: FactStore,
    provider: LLMProvider,
    *,
    reference_date: date,
    narrative_retriever: NarrativeRetriever | None,
    intent_parser: IntentParser | None,
) -> AgentResult:
    """W6a 多跳分解 fan-out：每个子问题独立走 answer_question（带全部 guard + 安全门），
    再确定性合成。

    关键不变量：子答案是各通路 guard 后的【最终文本】（found/not-found 改写、安全门越权拒答、
    血缘回指都已生效），合成只是确定性拼接 + 来源去重聚合——零 LLM、不夹带散文、不编造数字。
    每个子问题重新跑完整 answer_question（decomposer 缺省 None，绝不递归分解），故安全门从每个
    子问句独立复核：分解产出的竞品子问题照样被越权拒答，隔离/拒答不被绕过。
    """
    sub_results = [
        answer_question(
            sq, store, provider,
            reference_date=reference_date,
            narrative_retriever=narrative_retriever,
            intent_parser=intent_parser,
        )
        for sq in subquestions
    ]
    lines = [f"多跳分解（{len(subquestions)} 个子问题）："]
    sources: list[dict[str, object]] = []
    tool_results: list[dict[str, object]] = []
    for i, (sq, sub) in enumerate(zip(subquestions, sub_results, strict=True), start=1):
        lines.append(f"\n【子问题 {i}】{sq}\n{sub.answer}")
        tool_results.extend(sub.tool_results)
        for src in sub.sources:
            if src not in sources:
                sources.append(src)
    return AgentResult(
        answer="\n".join(lines),
        route=ROUTE_DECOMPOSED,
        clarification=None,
        tool_results=tool_results,
        sources=sources,
    )


def answer_question(
    question: str,
    store: FactStore,
    provider: LLMProvider,
    *,
    reference_date: date | None = None,
    narrative_retriever: NarrativeRetriever | None = None,
    intent_parser: IntentParser | None = None,
    decomposer: QueryDecomposer | None = None,
) -> AgentResult:
    """单条问题端到端编排入口。

    reference_date：相对期间（去年/上半年…）的换算基准，默认今天；
    narrative_retriever：叙事检索实现（duck-typed 注入），缺省时叙事路坦白降级；
    intent_parser：意图解析器（ADR 0010 可插拔），缺省用零-LLM 规则实现。
        无论用哪个解析器，澄清网关里的安全门都从 raw_question 独立复核越权/竞品。
    decomposer：W6a 查询分解器（opt-in，默认 None＝不分解，主流程逐位字节不变）。注入且把问题
        真拆成 >1 子问题时，各子问题独立走本函数（带全部 guard + 安全门）再确定性合成；返回单
        子问题（不可分解）时回落正常单发路由。
    """
    # W6a：注入了分解器且真分解（>1 子问题）时走 fan-out；否则（含 decomposer=None）落到下方
    # 既有主流程——此分支不命中时主流程逐位不变，默认 loop 字节等价。
    if decomposer is not None:
        ref0 = reference_date or date.today()
        subquestions = decomposer.decompose(question, reference_date=ref0)
        if len(subquestions) > 1:
            return _answer_decomposed(
                subquestions, store, provider,
                reference_date=ref0,
                narrative_retriever=narrative_retriever,
                intent_parser=intent_parser,
            )

    request_id = new_request_id()
    ctx = _TraceCtx()
    ref = reference_date or date.today()
    parser = intent_parser or RuleIntentParser()
    intent = parser.parse(question, reference_date=ref)
    clar = clarify_scope(intent, reference_date=ref)

    # 外部/竞品实体越权：最前置拒答，绝不调 tool/检索/LLM，绝不输出 home 公司数字
    if clar.mode == CLARIFY_OUT_OF_SCOPE_ENTITY:
        _emit_request_trace(request_id, intent, clar, [], ctx)
        return AgentResult(answer=clar.question or "", route=intent.route,
                           clarification=clar, tool_results=[], sources=[])

    # 前置单选：歧义会导致实质错误，直接反问，不调用 LLM
    if clar.mode == CLARIFY_ASK_FIRST:
        _emit_request_trace(request_id, intent, clar, [], ctx)
        return AgentResult(answer=clar.question or "", route=intent.route,
                           clarification=clar)

    # 默认先答：把假设槽位回填进问题（结构化通路按受控代码追加限定）
    effective_question = question
    if clar.mode == CLARIFY_ANSWER_WITH_ASSUMPTIONS:
        addenda = []
        if "entity" in clar.assumed_slots:
            addenda.append(f"实体={clar.assumed_slots['entity']}")
        if "period" in clar.assumed_slots:
            period_type, value = clar.assumed_slots["period"]
            addenda.append(
                f"期间={'FY' + value if period_type == 'FY' else value}"
            )
        effective_question = f"{question}（按默认口径：{'，'.join(addenda)}）"

    if intent.route == ROUTE_NARRATIVE:
        answer, sources = _run_narrative(
            question, provider, narrative_retriever, intent, ctx
        )
        _emit_request_trace(request_id, intent, clar, [], ctx)
        return AgentResult(answer=answer, route=intent.route,
                           clarification=clar, sources=sources)

    # structured / composite 都先跑数字子任务。
    # 多指标/多实体/多期间（用户明确列举的轴）→ 展开为多个子任务确定性执行；
    # 单子任务保持既有 tool use 循环行为不变。
    subtasks = expand_subtasks(
        intent,
        default_entity=clar.assumed_slots.get("entity"),
        default_period=clar.assumed_slots.get("period"),
    )
    if len(subtasks) > 1:
        tool_results = _run_subtasks(subtasks, store)
        answer, sources = _multi_subtask_answer(tool_results)
    else:
        model_text, tool_results = _run_tool_loop(
            effective_question, store, provider, ref, ctx
        )
        answer, sources = _structured_answer(model_text, tool_results)

    if intent.route == ROUTE_COMPOSITE:
        narrative_answer, narrative_sources = _run_narrative(
            question, provider, narrative_retriever, intent, ctx
        )
        answer = f"{answer}\n\n归因分析：\n{narrative_answer}"
        sources = sources + narrative_sources

    if clar.mode == CLARIFY_ANSWER_WITH_ASSUMPTIONS:
        options = "／".join(clar.narrowing_options)
        answer = (
            f"【假设】{clar.assumption_note}（如需收窄：{options}）\n{answer}"
        )

    _emit_request_trace(request_id, intent, clar, tool_results, ctx)
    return AgentResult(answer=answer, route=intent.route, clarification=clar,
                       tool_results=tool_results, sources=sources)
