"""W6a 查询分解（opt-in，默认关）：把真多跳问题拆成多个子问题，分别回答再确定性合成。

现状（docs/prd-quality-depth.md W6a）：编排是单发、规则路由 retrieve-then-generate；分解只有
确定性笛卡尔（intent.expand_subtasks，仅在用户明确列举的轴上展开）。本模块沿既有 IntentParser/
QueryRewriter 缝（ADR 0010 已把"问什么"从安全判定解耦）加一个 **LLM 驱动** 的子问题分解：
"哪个区域增长最快、为什么"这类一句话夹多跳的问题，拆成 N 个独立子问题，各自走 answer_question 的
既有通路（结构化/叙事 + 全部 guard），再确定性合成。

硬约束（守 ADR 0001 确定性 + 反编造）：
- **默认关、字节不变**：answer_question 的 decomposer 参数默认 None，不注入即整条主流程逐位不变。
- **不绕过任何 guard**：每个子问题重新跑完整 answer_question——独立过安全门（越权/竞品拒答）、独立
  做 found/not-found 改写。分解只决定"问什么"，绝不在合成处夹带模型散文或编造数字。
- **确定性合成**：子答案以固定模板拼接（各子答案已是各通路 guard 后的结果），合成本身零 LLM。
- **有界**：子问题数量上限 max_subquestions，防发散；解析失败/provider 故障 → 退回原问句单元素表。

LLM 分解非确定，故仅作 opt-in 适配器（经 make_decomposer / RAGSPINE_QUERY_DECOMPOSE 选用，且必须
注入 provider 才生效）；默认仍是 RuleIntentParser 的确定性笛卡尔。
"""

import json
from datetime import date
from typing import Protocol, runtime_checkable

from ragspine.agent.llm_provider import LLMProvider, ProviderError

# 分解结果的路由标记：与 structured/narrative/composite 区分，仅在注入 decomposer 且真分解时出现。
ROUTE_DECOMPOSED = "decomposed"

# 分解选型读取的环境变量名（缺省 spec 时生效）。
QUERY_DECOMPOSE_ENV = "RAGSPINE_QUERY_DECOMPOSE"

# 子问题数量默认上限（有界，防发散）。
DEFAULT_MAX_SUBQUESTIONS = 4

# 分解提示：要求模型只输出一个 JSON 字符串数组（子问题），不可分解则原样返回单元素数组。
_DECOMPOSE_SYSTEM = (
    "你是查询分解器。把用户的复杂多跳问题拆成若干个相互独立、可分别检索回答的子问题，"
    "只输出一个 JSON 字符串数组，不要任何解释。若问题本就单一、无需分解，返回只含原问题的单元素数组。"
)


@runtime_checkable
class QueryDecomposer(Protocol):
    """查询分解协议：把一个问句拆成 1..N 个子问句。

    约定：返回 **非空** 列表；不可分解时返回 [原问句]（长度 1，调用方据此回退到正常单发路由）。
    实现可为非确定（LLM），故只作 opt-in 注入件——默认 None＝不分解，主流程字节不变。
    """

    def decompose(
        self, question: str, *, reference_date: date | None = None
    ) -> list[str]: ...


class LLMQueryDecomposer:
    """LLM 驱动的查询分解器（opt-in）。

    单轮调用 provider 让其产出 JSON 字符串数组；鲁棒解析 + 有界截断 + 确定性降级：
    - provider 抛 ProviderError（网络/API 故障）→ 返回 [question]（不分解、不崩）；
    - 回文非 JSON 数组 / 数组空 / 元素非字符串 → 返回 [question]；
    - 数组超过 max_subquestions → 截断到上限（防发散）。
    """

    def __init__(self, provider: LLMProvider, *, max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS):
        self.provider = provider
        self.max_subquestions = max(1, max_subquestions)

    def decompose(
        self, question: str, *, reference_date: date | None = None
    ) -> list[str]:
        try:
            resp = self.provider.chat([
                {"role": "system", "content": _DECOMPOSE_SYSTEM},
                {"role": "user", "content": question},
            ])
        except ProviderError:
            return [question]
        text = resp.choices[0].message.content or ""
        subs = _parse_subquestions(text)
        if not subs:
            return [question]
        return subs[: self.max_subquestions]


def _parse_subquestions(text: str) -> list[str]:
    """从模型回文鲁棒解析 JSON 字符串数组；任何不合规一律视为"无法分解"返回空表。"""
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    subs = [s.strip() for s in parsed if isinstance(s, str) and s.strip()]
    return subs


# 复杂度标签：simple=单跳（单一检索即可），complex=多跳（需拆成子问题分别检索再综合）。
COMPLEXITY_SIMPLE = "simple"
COMPLEXITY_COMPLEX = "complex"

# 启发式多跳信号（确定性、零 LLM）：并列/比较/归因复合等暗示需拆问。受控小集合，宁缺毋滥。
_COMPARISON_CUES = ("对比", "相比", "比较", "分别", "各自", "哪个最", "排名", "vs", "versus")
_CONJUNCTION_CUES = ("以及", "并且", "同时", "还有", "和为什么", "且", "并分析")
_CAUSAL_CUES = ("为什么", "原因", "为何", "驱动", "归因")

_ADAPTIVE_SYSTEM = (
    "你是查询复杂度分类器。判断用户问题是【单跳】（simple，单一事实/单一检索即可回答）还是【多跳】"
    "（complex，需拆成多个子问题分别检索再综合）。只输出一个词：simple 或 complex，不要任何解释。"
)


@runtime_checkable
class QueryComplexityClassifier(Protocol):
    """查询复杂度分类协议：问句 -> 'simple' | 'complex'。

    默认实现是确定性启发式（HeuristicComplexityClassifier）；LLM 分类作 opt-in（LLMComplexityClassifier，
    带启发式兜底）。Adaptive-RAG 用它在【单跳/多跳】间路由——本仓库反编造不变量禁止无依据的
    parametric/no-retrieval 路由，故绝无"不检索直接答"一档。
    """

    def classify(
        self, question: str, *, reference_date: date | None = None
    ) -> str: ...


class HeuristicComplexityClassifier:
    """确定性启发式复杂度分类（零 LLM、零网络）：命中比较/复合归因/多疑问信号即判 complex，否则 simple。"""

    def classify(
        self, question: str, *, reference_date: date | None = None
    ) -> str:
        q = question
        if any(cue in q for cue in _COMPARISON_CUES):
            return COMPLEXITY_COMPLEX
        if any(cue in q for cue in _CONJUNCTION_CUES):
            return COMPLEXITY_COMPLEX
        # 既问"是什么/多少"又问"为什么"——典型一句夹多跳。
        causal = any(cue in q for cue in _CAUSAL_CUES)
        factual = any(cue in q for cue in ("多少", "是多少", "是什么", "排名", "占比"))
        if causal and factual:
            return COMPLEXITY_COMPLEX
        return COMPLEXITY_SIMPLE


class LLMComplexityClassifier:
    """LLM 复杂度分类（opt-in）：单轮问 simple/complex；provider 故障 / 回文不合规 -> 启发式兜底。"""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        fallback: QueryComplexityClassifier | None = None,
    ):
        self.provider = provider
        self.fallback = fallback or HeuristicComplexityClassifier()

    def classify(
        self, question: str, *, reference_date: date | None = None
    ) -> str:
        try:
            resp = self.provider.chat([
                {"role": "system", "content": _ADAPTIVE_SYSTEM},
                {"role": "user", "content": question},
            ])
        except ProviderError:
            return self.fallback.classify(question, reference_date=reference_date)
        text = (resp.choices[0].message.content or "").strip().lower()
        if COMPLEXITY_COMPLEX in text:
            return COMPLEXITY_COMPLEX
        if COMPLEXITY_SIMPLE in text:
            return COMPLEXITY_SIMPLE
        return self.fallback.classify(question, reference_date=reference_date)


class AdaptiveDecomposer:
    """Adaptive-RAG（实现 QueryDecomposer 协议）：先分类复杂度，complex 才委托 base 分解器拆问。

    simple -> [原问题]（answer_question 据此回落正常单发路由）；complex -> base 分解器.decompose（W6a
    LLMQueryDecomposer）。把"是否值得拆"从无脑总拆，升级为按复杂度自适应路由——省调用、降误拆。
    """

    def __init__(
        self,
        decomposer: QueryDecomposer,
        classifier: QueryComplexityClassifier | None = None,
    ):
        self.decomposer = decomposer
        self.classifier = classifier or HeuristicComplexityClassifier()

    def decompose(
        self, question: str, *, reference_date: date | None = None
    ) -> list[str]:
        if self.classifier.classify(question, reference_date=reference_date) == COMPLEXITY_COMPLEX:
            return self.decomposer.decompose(question, reference_date=reference_date)
        return [question]


def make_decomposer(
    spec: str | None = None, *, provider: LLMProvider | None = None
) -> QueryDecomposer | None:
    """分解器选型工厂：把「是否 LLM 分解」从改代码降为一个 spec/env，默认 None＝不分解（行为不变）。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_QUERY_DECOMPOSE）：
        - None / 'none'  -> None（不分解；answer_question 走既有确定性笛卡尔单发路由，字节不变）
        - 'llm'          -> 注入了 provider 则 LLMQueryDecomposer；未注入 provider 则 None
                            （"注入 provider 才生效"——诚实降级为不分解，绝不空跑）
        - 'adaptive'     -> 注入了 provider 则 AdaptiveDecomposer(LLMQueryDecomposer, LLMComplexityClassifier)
                            （W9 Adaptive-RAG：按复杂度路由 单跳/多跳，多跳才拆）；未注入 provider 则 None
        - 其他           -> ValueError

    返回 QueryDecomposer 实例或 None（可直接喂给 answer_question 的 decomposer 参数）。
    """
    if spec is None:
        import os

        spec = os.environ.get(QUERY_DECOMPOSE_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "none":
        return None
    if normalized == "llm":
        if provider is None:
            return None
        return LLMQueryDecomposer(provider)
    if normalized == "adaptive":
        if provider is None:
            return None
        return AdaptiveDecomposer(
            LLMQueryDecomposer(provider), LLMComplexityClassifier(provider)
        )
    raise ValueError(
        f"未知 query-decompose spec：{normalized!r}（可选 none / llm / adaptive；llm/adaptive 需注入 provider）"
    )
