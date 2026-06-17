"""意图&范围解析 + 澄清网关。

从用户话语解析 metric/entity/period/channel 四个槽位（复用 glossary 归一化，
相对期间注入 reference_date 解析），并做三路分流：
    structured —— 纯查数（含"多少"等数字意图但指标缺失的情况，由澄清网关兜底）
    narrative  —— 归因/监管/人物等叙事问题
    composite  —— 复合问题（数字子任务 + 归因子任务）
澄清原则（docs/02 §2）：默认先答 + 显式暴露假设 + 一键收窄；
只有歧义会导致实质错误（如指标缺失）才前置单选反问。
"""

import re
from dataclasses import dataclass, field
from datetime import date
from itertools import product

from ragspine.common.company_profile import load_company_profile
from ragspine.common.glossary import (
    ENTITY_SYNONYMS,
    EXTERNAL_ENTITY_SYNONYMS,
    METRIC_SYNONYMS,
    resolve_relative_period,
)

ROUTE_STRUCTURED = "structured"
ROUTE_NARRATIVE = "narrative"
ROUTE_COMPOSITE = "composite"

CLARIFY_NONE = "none"
CLARIFY_ANSWER_WITH_ASSUMPTIONS = "answer_with_assumptions"
CLARIFY_ASK_FIRST = "ask_first"
# 外部/竞品实体越权：系统无该主体数据，命中即拒答并提议改查 home 等价口径。
CLARIFY_OUT_OF_SCOPE_ENTITY = "out_of_scope_entity"

# 数字意图线索：出现即认为用户在要一个数
_NUMERIC_CUES = ("多少", "几个", "what is", "what was", "how much", "how many")

# 叙事意图线索：归因/监管/评价/进展类
_NARRATIVE_CUES = (
    "为什么", "为何", "原因", "归因", "怎么看", "如何看", "怎么样",
    "监管", "政策", "动态", "进展", "影响", "评价", "趋势", "下降", "上升",
    "why", "reason", "driver", "regulat", "trend", "impact",
)

# 渠道同义词（glossary 暂无渠道维度词典，先在意图层维护）
_CHANNEL_SYNONYMS: dict[str, str] = {
    "代理": "AGENCY",
    "代理人": "AGENCY",
    "agency": "AGENCY",
    "银保": "BANCA",
    "bancassurance": "BANCA",
    "banca": "BANCA",
}

# 相对期间词（长词优先匹配，避免"去年上半年"被"去年"截胡）
_RELATIVE_PERIOD_TOKENS = (
    "去年上半年", "去年下半年", "今年上半年", "今年下半年", "前年上半年", "前年下半年",
    "上半年", "下半年", "上个季度", "这个季度", "上季度", "本季度",
    "去年", "今年", "本年", "上年", "前年", "last year", "this year",
)

# 绝对期间："FY2024" / "2024年" / "2024H1" / "2024年上半年" / "2025Q1"
_ABS_PERIOD_RE = re.compile(
    r"(?:FY\s*)?(\d{4})\s*年?\s*(?:(上半年)|(下半年)|H\s*([12])|Q\s*([1-4]))?",
    re.IGNORECASE,
)

# 支持的指标（澄清反问时列给用户选）
_SUPPORTED_METRICS = ("REVENUE", "NEWSALES", "PROFIT", "ROE")

# home 公司 profile（默认实体 + 拒答提议文案用的公司名，皆配置化，不硬编码）。
_PROFILE = load_company_profile()
# 默认假设：实体= home 集团口径
_DEFAULT_ENTITY = _PROFILE.home_entity_code


@dataclass
class ParsedIntent:
    """解析结果：路由 + 四槽位（缺失为 None，channel 默认 TOTAL）。

    metrics / entities / periods 为新增多值槽位（composite 多指标/多实体对比）：
    按文本出现顺序去重列出用户明确列举的全部值，未匹配到时为空列表；
    既有单值字段 metric/entity/period 的语义与取值逻辑保持不变。
    """

    route: str
    metric: str | None
    entity: str | None
    period: tuple[str, str] | None
    channel: str
    raw_question: str
    metrics: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    periods: list[tuple[str, str]] = field(default_factory=list)
    external_entity: str | None = None


@dataclass
class SubTask:
    """一次 query_metric 查询子任务（composite 展开后的最小执行单元）。"""

    metric: str | None
    entity: str | None
    period: tuple[str, str] | None
    channel: str = "TOTAL"


@dataclass
class ClarificationResult:
    """澄清网关输出：先答携带的假设说明，或前置单选问题。"""

    mode: str
    assumed_slots: dict = field(default_factory=dict)
    assumption_note: str | None = None
    narrowing_options: list[str] = field(default_factory=list)
    question: str | None = None


def _match_longest(text: str, synonyms: dict[str, str]) -> str | None:
    """在文本中按同义词长度降序做子串匹配，命中返回受控代码。

    text 已小写；同义词键大小写不一（中文条目可能混入大写公司前缀如 "ACME中国"），
    故按小写键匹配，保证大小写不敏感。
    """
    for key in sorted(synonyms, key=len, reverse=True):
        if key and key.lower() in text:
            return synonyms[key]
    return None


def _match_external(text: str) -> tuple[str | None, str]:
    """外部/竞品实体最长匹配 + 命中遮蔽。

    按外部别名长度降序在已归一文本中找首个命中，整体吃掉（如"中国竞安"长于"竞安"），
    返回 (展示名, 遮蔽后文本)。遮蔽= 把命中子串替换为等长空格，保持后续 home 实体
    匹配的位置语义，且让残留文本不再含可泄露成 home 实体的子串（防"中国"碰撞）。
    未命中返回 (None, 原文本)。
    """
    for alias in sorted(EXTERNAL_ENTITY_SYNONYMS, key=len, reverse=True):
        if not alias:
            continue
        pos = text.find(alias.lower())
        if pos >= 0:
            end = pos + len(alias)
            masked = text[:pos] + " " * len(alias) + text[end:]
            return EXTERNAL_ENTITY_SYNONYMS[alias], masked
    return None, text


def _match_all(text: str, synonyms: dict[str, str]) -> list[str]:
    """找出文本中全部同义词命中（长词优先、跨词条不重叠），按出现位置去重列出受控代码。"""
    spans: list[tuple[int, int, str]] = []
    for key in sorted(synonyms, key=len, reverse=True):
        if not key:
            continue
        needle = key.lower()
        start = 0
        while (pos := text.find(needle, start)) >= 0:
            end = pos + len(needle)
            if all(end <= s or pos >= e for s, e, _ in spans):
                spans.append((pos, end, synonyms[key]))
            start = pos + 1
    codes: list[str] = []
    for _, _, code in sorted(spans):
        if code not in codes:
            codes.append(code)
    return codes


def _extract_all_periods(
    text: str, reference_date: date | None
) -> list[tuple[str, str]]:
    """找出文本中全部期间（绝对优先占位，相对词不与其重叠），按出现位置去重列出。

    与 _extract_period 的单值语义解耦：这里只服务多值槽位 periods。
    """
    spans: list[tuple[int, int, tuple[str, str]]] = []
    for m in _ABS_PERIOD_RE.finditer(text):
        year, h1, h2, h_num, q_num = m.groups()
        if h1 or (h_num == "1"):
            resolved = ("HY", f"{year}H1")
        elif h2 or (h_num == "2"):
            resolved = ("HY", f"{year}H2")
        elif q_num:
            resolved = ("QUARTER", f"{year}Q{q_num}")
        else:
            resolved = ("FY", year)
        spans.append((m.start(), m.end(), resolved))

    for token in _RELATIVE_PERIOD_TOKENS:
        start = 0
        while (pos := text.find(token, start)) >= 0:
            end = pos + len(token)
            if all(end <= s or pos >= e for s, e, _ in spans):
                resolved = resolve_relative_period(token, reference_date)
                if resolved is not None:
                    spans.append((pos, end, resolved))
            start = pos + 1

    periods: list[tuple[str, str]] = []
    for _, _, period in sorted(spans):
        if period not in periods:
            periods.append(period)
    return periods


def expand_subtasks(
    intent: ParsedIntent,
    *,
    default_entity: str | None = None,
    default_period: tuple[str, str] | None = None,
) -> list[SubTask]:
    """把解析结果展开为 1..N 个 query_metric 子任务（docs/02 §2 Query Planner）。

    展开规则：笛卡尔积只在用户明确列举（多值槽位 len>1）的轴上进行；
    未列举的轴固定为单值（解析出的单槽位值，缺失时用注入的默认值），
    绝不向"全部支持的指标/实体"做全笛卡尔扩张。channel 整体共享。
    子任务顺序：实体外层 → 指标 → 期间（与用户列举顺序一致）。
    """
    metrics = intent.metrics or [intent.metric]
    entities = intent.entities or [intent.entity or default_entity]
    periods = intent.periods or [intent.period or default_period]
    return [
        SubTask(metric=m, entity=e, period=p, channel=intent.channel)
        for e, m, p in product(entities, metrics, periods)
    ]


def _extract_period(
    text: str, reference_date: date | None
) -> tuple[str, str] | None:
    """先试带年份的绝对期间（"2024年上半年"优先于裸"上半年"），再试相对期间词。"""
    for m in _ABS_PERIOD_RE.finditer(text):
        year, h1, h2, h_num, q_num = m.groups()
        if h1 or (h_num == "1"):
            return ("HY", f"{year}H1")
        if h2 or (h_num == "2"):
            return ("HY", f"{year}H2")
        if q_num:
            return ("QUARTER", f"{year}Q{q_num}")
        return ("FY", year)

    for token in _RELATIVE_PERIOD_TOKENS:
        if token in text:
            resolved = resolve_relative_period(token, reference_date)
            if resolved is not None:
                return resolved
    return None


def parse_intent(question: str, reference_date: date | None = None) -> ParsedIntent:
    """解析用户话语为 ParsedIntent。相对期间按注入的 reference_date 解析。"""
    text = re.sub(r"\s+", " ", question.strip().lower())

    # 先做外部/竞品实体最长匹配并遮蔽命中子串，再在遮蔽后文本上做 home 实体匹配：
    # 这样"中国竞安"命中外部"竞安"（实命中更长的"中国竞安"）遮蔽后不再有"中国"
    # 泄露成 ACME_CN；standalone"中国"无外部命中、遮蔽为空，照常解析为 home 实体。
    external_entity, entity_text = _match_external(text)

    metric = _match_longest(text, METRIC_SYNONYMS)
    entity = _match_longest(entity_text, ENTITY_SYNONYMS)
    period = _extract_period(text, reference_date)
    channel = _match_longest(text, _CHANNEL_SYNONYMS) or "TOTAL"

    has_numeric_cue = metric is not None or any(c in text for c in _NUMERIC_CUES)
    has_narrative_cue = any(c in text for c in _NARRATIVE_CUES)

    if metric is not None and has_narrative_cue:
        route = ROUTE_COMPOSITE
    elif has_numeric_cue:
        route = ROUTE_STRUCTURED
    else:
        route = ROUTE_NARRATIVE

    return ParsedIntent(
        route=route, metric=metric, entity=entity, period=period,
        channel=channel, raw_question=question,
        metrics=_match_all(text, METRIC_SYNONYMS),
        entities=_match_all(entity_text, ENTITY_SYNONYMS),
        periods=_extract_all_periods(text, reference_date),
        external_entity=external_entity,
    )


def clarify_scope(
    intent: ParsedIntent, reference_date: date | None = None
) -> ClarificationResult:
    """澄清网关：结构化/复合路线检查槽位完整性，按"默认先答"原则产出假设或反问。"""
    # 外部/竞品实体越权检查最前置（先于 narrative 早返回、先于 metric 缺失检查）：
    # 无论哪条路我们都没有外部数据，命中即拒答并提议改查 home 等价口径，
    # 绝不把 home 公司数字当成外部主体答案输出。
    if intent.external_entity is not None:
        home_name = _PROFILE.home_company_name
        metric_clause = f"的 {intent.metric}" if intent.metric else "的对应数字"
        message = (
            f"系统不掌握「{intent.external_entity}」的数据，无法回答该外部/竞品主体的"
            f"问题。可改查 {home_name}{metric_clause}。"
        )
        return ClarificationResult(
            mode=CLARIFY_OUT_OF_SCOPE_ENTITY,
            question=message,
            narrowing_options=[f"改查 {home_name}{metric_clause}"],
        )

    if intent.route == ROUTE_NARRATIVE:
        return ClarificationResult(mode=CLARIFY_NONE)

    # 指标缺失：猜错指标=实质错误 → 前置单选
    if intent.metric is None:
        options = "、".join(_SUPPORTED_METRICS)
        return ClarificationResult(
            mode=CLARIFY_ASK_FIRST,
            question=f"想查询哪个指标？目前支持：{options}。",
            narrowing_options=list(_SUPPORTED_METRICS),
        )

    assumed: dict = {}
    notes: list[str] = []
    options: list[str] = []

    if intent.entity is None:
        assumed["entity"] = _DEFAULT_ENTITY
        labels = _PROFILE.home_entity_labels
        default_label = labels.get(_DEFAULT_ENTITY, _DEFAULT_ENTITY)
        notes.append(f"实体默认按 {default_label} 口径")
        # 收窄项 = 改查 labels 中【非默认】的实体（顺序按 labels 插入序，稳定可测）。
        options.extend(
            f"改查 {label}"
            for code, label in labels.items()
            if code != _DEFAULT_ENTITY
        )

    if intent.period is None:
        ref = reference_date or date.today()
        latest_fy = str(ref.year - 1)
        assumed["period"] = ("FY", latest_fy)
        notes.append(f"期间默认取最近完整财年 FY{latest_fy}")
        options.extend([f"改查 FY{ref.year}（今年至今）", f"改查 {latest_fy}H1/H2"])

    if not assumed:
        return ClarificationResult(mode=CLARIFY_NONE)

    return ClarificationResult(
        mode=CLARIFY_ANSWER_WITH_ASSUMPTIONS,
        assumed_slots=assumed,
        assumption_note="；".join(notes),
        narrowing_options=options,
    )
