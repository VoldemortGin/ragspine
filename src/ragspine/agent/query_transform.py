"""W9 查询变换（opt-in，默认关，字节不变）：HyDE + RAG-Fusion + step-back + Adaptive-RAG。

现状（docs/prd-quality-depth.md W9）：查询侧变换只有确定性两件——RuleIntentParser 的受控词表同义词
多查询（intent.expand_subtasks 笛卡尔）与 W6a 的 opt-in LLM 分解（agent/decompose.py）。本模块沿既有
QueryRewriter/IntentParser 缝（ADR 0010 已把"问什么"从安全判定解耦）补齐四个主流竞品都有、ragspine
之前缺的 LLM 查询变换，全部 opt-in、默认关、未选用时逐位字节不变：

1. **HyDE**（假设文档嵌入）：LLM 先写一个假想答案文档，用【它】去检索（对齐 query 向量与答案形状的
   passage）。假想文档只是【检索探针】，绝不进答案/引用——返回的仍是真 chunk（隔离 + 血缘不变）。
   对标 LlamaIndex HyDEQueryTransform / LangChain HypotheticalDocumentEmbedder。
2. **RAG-Fusion**：LLM 生成 N 个 query 变体 → 各自检索 → 复用 retrieval 已有的 rrf_fuse 融合。与
   确定性同义词多查询（GlossaryQueryRewriter）是不同层：那个是规则同义词，这个是 LLM 生成变体。
   对标 LlamaIndex QueryFusionRetriever / LangChain MultiQueryRetriever。
3. **step-back prompting**：LLM 先生成一个更抽象的"退一步"问题检索更广背景，再与原问题结果合并
   （RRF）。对标 Zheng et al. 2023 step-back prompting。
4. **Adaptive-RAG**（复杂度路由）：按查询复杂度路由——simple→直接结构化/不检索、single→单跳、
   multi→多跳（交给 W6a 分解 fan-out）。默认是【确定性启发式分类器】（按 slot 数/cue 词，零 LLM），
   opt-in LLM 分类器（同 W5 EntailmentJudge 的"确定性默认 + opt-in 模型"范式）。对标 LangGraph
   adaptive-rag。Adaptive 复用既有 answer_question(decomposer=) 缝：AdaptiveDecomposer 实现
   QueryDecomposer 协议，multi 才委托内层 LLM 分解、否则返回单元素表（正常单发路由，字节不变）。

硬约束（守 ADR 0001 确定性 + 反编造 + ADR 0010 安全）：
- **默认关、字节不变**：HyDE/RAG-Fusion/step-back 是 NarrativeRetriever 包装件（缝同 W6b
  CorrectiveRetriever）——make_query_transform('none') 返回 base 本身；answer_question 完全不改，故
  agent 既有测试与 byte-identity golden 逐位不变。Adaptive 复用 decomposer 缝，默认 None＝不路由。
- **假想文档绝不引为 fact**：HyDE 只把假想文档当【检索 query 文本】喂给 base.retrieve，返回的片段全是
  真 chunk（带真血缘）；假想文档本身绝不进片段/答案/引用（冻结于 test_query_transform.py）。
- **每个生成变体/退一步问题都过确定性安全门**：RAG-Fusion 变体与 step-back 问题在检索前逐个过
  SecurityGate（security_gate.py）——竞品/越权变体被剔除、绝不检索（原始问句在 answer_question 入口
  已过门；此处拦的是 LLM 变换新引入的竞品 query）。数字仍归结构化通路，本层只做叙事检索。
- **隔离继承**：三个 wrapper 只对 base.retrieve(...) 的输出取舍/融合，绝不自行造片段、绝不直接读块库
  ——base（NarrativeIndexRetriever）已在出口剔除 sensitivity==RESTRICTED，故输出恒为 base 输出子集，
  RESTRICTED 永不出域（隔离 conformance 见 tests/agent/test_query_transform.py）。
- **诚实降级**：provider 故障/无 provider/回文不合规 → 回退确定性行为（HyDE 用原 query、RAG-Fusion/
  step-back 只用原 query），绝不编造。

LLM 变换非确定，故一律 opt-in（经 make_query_transform / RAGSPINE_QUERY_TRANSFORM、
make_adaptive_decomposer / RAGSPINE_ADAPTIVE 选用，且需注入 provider 才生效）。

*Follow-up（诚实标注，见 PRD W9）*：HyDE 的最大收益在 dense 通道，需 W1 的 dense-on（auto）；
step-back 的【确定性变体】（沿受控词表维度层级泛化，零 LLM）；一个在 W5 harness 上度量各变换召回增益
的 A/B。
"""

import json
import os
import re
from datetime import date
from typing import Protocol, runtime_checkable

from ragspine.agent.decompose import LLMQueryDecomposer, QueryDecomposer
from ragspine.agent.intent import ROUTE_STRUCTURED, parse_intent
from ragspine.agent.llm_provider import LLMProvider, ProviderError
from ragspine.agent.security_gate import SECURITY_REFUSE_OUT_OF_SCOPE, SecurityGate
from ragspine.common.company_profile import load_company_profile
from ragspine.common.glossary import EXTERNAL_ENTITY_SYNONYMS
from ragspine.retrieval.lexical.retrieval import rrf_fuse

# 查询变换选型读取的环境变量名（缺省 spec 时生效）。
QUERY_TRANSFORM_ENV = "RAGSPINE_QUERY_TRANSFORM"
# Adaptive-RAG 选型读取的环境变量名（缺省 spec 时生效）。
ADAPTIVE_ENV = "RAGSPINE_ADAPTIVE"

# RAG-Fusion 变体数量默认上限（有界，防发散）。
DEFAULT_MAX_VARIANTS = 4

# 复杂度标签：simple＝可直接结构化/不检索；single＝单跳；multi＝多跳（交给分解 fan-out）。
COMPLEXITY_SIMPLE = "simple"
COMPLEXITY_SINGLE = "single"
COMPLEXITY_MULTI = "multi"
_COMPLEXITY_LABELS = frozenset({COMPLEXITY_SIMPLE, COMPLEXITY_SINGLE, COMPLEXITY_MULTI})

# 显式比较/对比线索：出现即倾向多跳（与 intent 的多值槽位互补）。
_COMPARISON_CUES = (
    "对比", "相比", "比较", "各自", "分别", "谁更", "哪个更", "哪个最", "哪些",
    "vs", "versus", "compare", "comparison",
)

# 各 LLM 变换的系统提示（编排层与 provider 约定；MockProvider 不解析这些、走确定性脚本）。
_HYDE_SYSTEM = (
    "你是资料撰写助手。针对用户的问题，写一段【假设性的答案文档】——就当你知道答案那样把它写出来，"
    "只输出这段文档正文，不要解释、不要标注这是假设。它只用于检索对齐，不会作为最终答案。"
)
_FUSION_SYSTEM = (
    "你是检索查询扩展助手。为用户问题生成若干个语义等价但措辞/角度不同的检索查询变体，"
    "只输出一个 JSON 字符串数组，不要任何解释。"
)
_STEPBACK_SYSTEM = (
    "你是问题抽象助手。把用户的具体问题抽象成一个更宽泛的『退一步』问题，用于检索更广的背景，"
    "只输出这一个问题（一行），不要解释。"
)
_COMPLEXITY_SYSTEM = (
    "你是查询复杂度分类器。判断用户问题属于以下哪一类，只输出一个词，不要解释："
    "simple（单一事实查数，无需检索）/ single（单跳，一次检索即可）/ multi（多跳，需拆成多个子问题）。"
)

__all__ = [
    "NarrativeRetriever",
    "HyDERetriever",
    "RAGFusionRetriever",
    "StepBackRetriever",
    "ComplexityClassifier",
    "HeuristicComplexityClassifier",
    "LLMComplexityClassifier",
    "AdaptiveDecomposer",
    "make_query_transform",
    "make_adaptive_decomposer",
    "COMPLEXITY_SIMPLE",
    "COMPLEXITY_SINGLE",
    "COMPLEXITY_MULTI",
    "QUERY_TRANSFORM_ENV",
    "ADAPTIVE_ENV",
]


@runtime_checkable
class NarrativeRetriever(Protocol):
    """叙事检索协议（duck-typed，结构等同 agent.NarrativeRetriever）：本层既包裹它、也实现它。

    本地声明而非 import 编排层的同名协议——只为结构一致，避免 agent.query_transform 反向耦合
    agent.agent（后者也只把它当注入协议）。
    """

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]: ...


def _default_security_gate() -> SecurityGate:
    """构建确定性安全门（口径同 intent._security_gate）：外部/竞品清单 + home 公司名。

    用于筛检 LLM 变换【新生成】的 query——竞品变体被拒即剔除、绝不检索。安全决策永远走这里，
    确定性、零 LLM（ADR 0010）。
    """
    return SecurityGate(EXTERNAL_ENTITY_SYNONYMS, load_company_profile().home_company_name)


def _is_out_of_scope(gate: SecurityGate, query: str) -> bool:
    """该 query 是否命中外部/竞品越权（命中即应剔除，不得检索）。"""
    return gate.screen(raw_question=query, metric=None).decision == SECURITY_REFUSE_OUT_OF_SCOPE


def _snippet_key(snippet: dict[str, object]) -> str:
    """片段去重/融合键：chunk_id 优先，缺失回落 doc_id+locator，再回落文本。"""
    cid = snippet.get("chunk_id")
    if cid:
        return str(cid)
    doc = snippet.get("doc_id") or snippet.get("source_doc_id") or ""
    loc = snippet.get("source_locator") or snippet.get("locator") or ""
    if doc or loc:
        return f"{doc}::{loc}"
    return str(snippet.get("text") or snippet.get("content") or "")


def _fuse_snippets(
    query_snippets: list[list[dict[str, object]]], *, top_k: int
) -> list[dict[str, object]]:
    """把多份检索片段列表按 RRF 融合成一份 top_k（复用 retrieval.rrf_fuse，确定性平分破除）。

    每份列表视为一个 ranked id 序（按 _snippet_key）；rrf_fuse 融合成 id→分，再按（-分, key）排序。
    片段实体取【首次出现】的那份（各变体返回同一 chunk 时血缘一致，取谁都等价）。
    """
    by_key: dict[str, dict[str, object]] = {}
    rankings: list[list[str]] = []
    for snippets in query_snippets:
        ranking: list[str] = []
        for s in snippets:
            key = _snippet_key(s)
            by_key.setdefault(key, s)
            ranking.append(key)
        rankings.append(ranking)
    fused = rrf_fuse(rankings)
    ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
    return [by_key[key] for key, _ in ordered[:top_k] if key in by_key]


def _parse_json_string_array(text: str) -> list[str]:
    """从模型回文鲁棒解析 JSON 字符串数组；任何不合规一律返回空表（视为"无变体"）。"""
    try:
        parsed = json.loads(text.strip())
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [s.strip() for s in parsed if isinstance(s, str) and s.strip()]


# ---------------------------------------------------------------------------
# HyDE：假设文档嵌入
# ---------------------------------------------------------------------------


class HyDERetriever:
    """HyDE 检索包装件（实现 NarrativeRetriever）：LLM 先写假想答案文档，用它作检索探针。

    retrieve(query)：单轮调 provider 让其写一段假想答案文档 → 用【该文档文本】作 query 检索
    （对齐 dense 向量与答案形状的 passage；BM25 通道也吃它、扩词面覆盖）。返回的仍是 base 的真
    chunk（隔离 + 血缘不变）——假想文档只是探针，绝不进片段/答案/引用。

    诚实降级：provider 抛 ProviderError / 回文空 → 用原 query 检索（不崩、不编造）。
    """

    def __init__(self, base: NarrativeRetriever, provider: LLMProvider):
        self.base = base
        self.provider = provider

    def _hypothetical_document(self, query: str) -> str | None:
        try:
            resp = self.provider.chat([
                {"role": "system", "content": _HYDE_SYSTEM},
                {"role": "user", "content": query},
            ])
        except ProviderError:
            return None
        text = (resp.choices[0].message.content or "").strip()
        return text or None

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]:
        probe = self._hypothetical_document(query) or query
        return self.base.retrieve(probe, filters=filters, top_k=top_k)


# ---------------------------------------------------------------------------
# RAG-Fusion：LLM 多 query 变体 → 各自检索 → RRF 融合
# ---------------------------------------------------------------------------


class RAGFusionRetriever:
    """RAG-Fusion 检索包装件（实现 NarrativeRetriever）：LLM 生成 N 变体 → 各检索 → RRF 融合。

    retrieve(query)：单轮调 provider 生成变体（JSON 数组，有界截断到 max_variants）；原 query 恒
    参与，每个【生成的】变体先过确定性安全门——竞品/越权变体剔除、绝不检索。各安全 query 分别检索，
    结果按 _snippet_key 复用 retrieval.rrf_fuse 融合成 top_k。

    与确定性同义词多查询（GlossaryQueryRewriter）不同层：那是规则同义词，这是 LLM 生成变体。
    诚实降级：provider 故障/无变体 → 退化为对原 query 单次检索（与不挂 fusion 等价）。
    """

    def __init__(
        self,
        base: NarrativeRetriever,
        provider: LLMProvider,
        *,
        max_variants: int = DEFAULT_MAX_VARIANTS,
        security_gate: SecurityGate | None = None,
    ):
        self.base = base
        self.provider = provider
        self.max_variants = max(1, max_variants)
        self.gate = security_gate or _default_security_gate()

    def _variants(self, query: str) -> list[str]:
        try:
            resp = self.provider.chat([
                {"role": "system", "content": _FUSION_SYSTEM},
                {"role": "user", "content": query},
            ])
        except ProviderError:
            return []
        return _parse_json_string_array(resp.choices[0].message.content or "")[: self.max_variants]

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]:
        # 原 query 恒参与（入口已过安全门）；生成的变体逐个过门，竞品剔除、去重、去掉与原相同者。
        queries = [query]
        for v in self._variants(query):
            if v in queries or _is_out_of_scope(self.gate, v):
                continue
            queries.append(v)
        if len(queries) == 1:
            return self.base.retrieve(query, filters=filters, top_k=top_k)
        per_query = [self.base.retrieve(q, filters=filters, top_k=top_k) for q in queries]
        return _fuse_snippets(per_query, top_k=top_k)


# ---------------------------------------------------------------------------
# step-back prompting：抽象出更宽泛的退一步问题，原 + 退一步都检索再合并
# ---------------------------------------------------------------------------


class StepBackRetriever:
    """step-back 检索包装件（实现 NarrativeRetriever）：LLM 抽象出更宽的退一步问题，两路合并。

    retrieve(query)：单轮调 provider 抽象出一个更宽泛的退一步问题；退一步问题过确定性安全门（越权则
    弃用），与原问题分别检索，按 RRF 合并成 top_k（原问题的具体命中 + 退一步的更广背景）。

    诚实降级：provider 故障 / 退一步为空或与原相同 / 退一步越权 → 只用原 query 检索。
    """

    def __init__(
        self,
        base: NarrativeRetriever,
        provider: LLMProvider,
        *,
        security_gate: SecurityGate | None = None,
    ):
        self.base = base
        self.provider = provider
        self.gate = security_gate or _default_security_gate()

    def _step_back_question(self, query: str) -> str | None:
        try:
            resp = self.provider.chat([
                {"role": "system", "content": _STEPBACK_SYSTEM},
                {"role": "user", "content": query},
            ])
        except ProviderError:
            return None
        text = (resp.choices[0].message.content or "").strip().splitlines()
        first = text[0].strip() if text else ""
        return first or None

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]:
        stepback = self._step_back_question(query)
        if stepback is None or stepback == query or _is_out_of_scope(self.gate, stepback):
            return self.base.retrieve(query, filters=filters, top_k=top_k)
        original = self.base.retrieve(query, filters=filters, top_k=top_k)
        broader = self.base.retrieve(stepback, filters=filters, top_k=top_k)
        return _fuse_snippets([original, broader], top_k=top_k)


# ---------------------------------------------------------------------------
# Adaptive-RAG：复杂度分类（确定性启发式默认 + opt-in LLM）→ 复用 decomposer 缝路由
# ---------------------------------------------------------------------------


@runtime_checkable
class ComplexityClassifier(Protocol):
    """查询复杂度分类协议：classify(question) -> simple / single / multi。"""

    def classify(self, question: str, *, reference_date: date | None = None) -> str: ...


class HeuristicComplexityClassifier:
    """确定性启发式复杂度分类器（W9 离线默认，零 LLM）。

    规则（复用 intent.parse_intent 的确定性解析，不新增词表）：
    - multi：用户在某个轴上明确列举了多值（metrics/entities/periods 任一 len>1），或出现显式对比线索
      （对比/哪个最/vs…）——这些天然多部分、宜拆多跳；
    - simple：structured 路由且单一指标、无对比线索——纯查数事实，可直接结构化、无需检索；
    - single：其余（单一叙事 / 单一 composite 归因问题）——单跳即可（composite 归因由 agent 的复合路由
      原生处理，无需分解）。
    """

    def classify(self, question: str, *, reference_date: date | None = None) -> str:
        intent = parse_intent(question, reference_date=reference_date)
        multi_axis = any(
            len(lst) > 1 for lst in (intent.metrics, intent.entities, intent.periods)
        )
        lowered = question.lower()
        has_comparison = any(cue in lowered for cue in _COMPARISON_CUES)
        if multi_axis or has_comparison:
            return COMPLEXITY_MULTI
        if intent.route == ROUTE_STRUCTURED and intent.metric is not None:
            return COMPLEXITY_SIMPLE
        return COMPLEXITY_SINGLE


class LLMComplexityClassifier:
    """opt-in LLM 复杂度分类器（同 W5 EntailmentJudge 的"确定性默认 + opt-in 模型"范式）。

    单轮调 provider 让其只输出一个标签词；鲁棒解析（含子串匹配）+ 确定性降级：provider 故障 / 回文
    非法标签 → 回退确定性 HeuristicComplexityClassifier（绝不崩、绝不臆断）。
    """

    def __init__(self, provider: LLMProvider):
        self.provider = provider
        self._fallback = HeuristicComplexityClassifier()

    def classify(self, question: str, *, reference_date: date | None = None) -> str:
        try:
            resp = self.provider.chat([
                {"role": "system", "content": _COMPLEXITY_SYSTEM},
                {"role": "user", "content": question},
            ])
        except ProviderError:
            return self._fallback.classify(question, reference_date=reference_date)
        text = (resp.choices[0].message.content or "").strip().lower()
        # 精确标签优先，其次子串匹配（模型可能多说一两个字）。
        if text in _COMPLEXITY_LABELS:
            return text
        for label in (COMPLEXITY_MULTI, COMPLEXITY_SINGLE, COMPLEXITY_SIMPLE):
            if re.search(rf"\b{label}\b", text):
                return label
        return self._fallback.classify(question, reference_date=reference_date)


class AdaptiveDecomposer:
    """Adaptive-RAG 路由（实现 QueryDecomposer 协议，复用 answer_question(decomposer=) 缝）。

    decompose(question)：先分类复杂度——multi 且注入了内层分解器时，委托内层 LLM 分解 fan-out（→ W6a
    多跳通路，各子问题独立过安全门 + guard）；simple/single（或 multi 但无内层分解器）则返回 [question]
    单元素表——answer_question 据此走【正常单发路由】，逐位字节不变。

    即：Adaptive 只在"确判为多跳"时启用分解，其余一律不改变默认路由行为（opt-in 增强，非替换）。
    """

    def __init__(
        self,
        classifier: ComplexityClassifier,
        inner: QueryDecomposer | None = None,
    ):
        self.classifier = classifier
        self.inner = inner

    def decompose(self, question: str, *, reference_date: date | None = None) -> list[str]:
        label = self.classifier.classify(question, reference_date=reference_date)
        if label == COMPLEXITY_MULTI and self.inner is not None:
            return self.inner.decompose(question, reference_date=reference_date)
        return [question]


# ---------------------------------------------------------------------------
# 工厂：make_query_transform（HyDE/RAG-Fusion/step-back）+ make_adaptive_decomposer（Adaptive）
# ---------------------------------------------------------------------------


def make_query_transform(
    base: NarrativeRetriever,
    spec: str | None = None,
    *,
    provider: LLMProvider | None = None,
    **kwargs: object,
) -> NarrativeRetriever:
    """查询变换选型工厂：默认 'none' 返回 base 本身（字节不变），LLM 变换 opt-in（缝同 make_corrective_retriever）。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_QUERY_TRANSFORM）：
        - None / 'none'                   -> base 本身（opt-out，字节不变，默认；未接线即无影响）
        - 'hyde'                          -> HyDERetriever（假设文档嵌入）
        - 'rag_fusion' / 'fusion'         -> RAGFusionRetriever（LLM 多变体 → RRF 融合）
        - 'step_back' / 'stepback'        -> StepBackRetriever（退一步 + 原问题合并）
        - 其他                            -> ValueError（列清可用 spec）

    provider：LLM 变换必需；未注入（None）时诚实降级为 base 本身（不空跑，同 make_decomposer 'llm'）。
    其余 kwargs（如 max_variants）透传给对应 wrapper。
    返回 NarrativeRetriever（可直接喂给 answer_question 的 narrative_retriever，或再被 corrective 包裹）。
    """
    if spec is None:
        spec = os.environ.get(QUERY_TRANSFORM_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "none":
        return base
    if normalized not in {"hyde", "rag_fusion", "fusion", "step_back", "stepback"}:
        raise ValueError(
            f"未知 query-transform spec {spec!r}；可用：none / hyde / rag_fusion / step_back"
        )
    if provider is None:
        # 诚实降级：选了 LLM 变换但没 provider → 回退 base，绝不空跑。
        return base
    if normalized == "hyde":
        return HyDERetriever(base, provider)
    if normalized in {"rag_fusion", "fusion"}:
        return RAGFusionRetriever(base, provider, **kwargs)  # type: ignore[arg-type]
    return StepBackRetriever(base, provider, **kwargs)  # type: ignore[arg-type]


def make_adaptive_decomposer(
    spec: str | None = None, *, provider: LLMProvider | None = None
) -> QueryDecomposer | None:
    """Adaptive-RAG 选型工厂：默认 None＝不路由（answer_question 走既有路由，字节不变），路由 opt-in。

    返回一个实现 QueryDecomposer 的 AdaptiveDecomposer，喂给 answer_question 的 decomposer 参数即启用
    "按复杂度路由"（multi 才分解 fan-out，simple/single 单发路由不变）。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_ADAPTIVE）：
        - None / 'none'          -> None（不路由，默认字节不变）
        - 'heuristic' / 'on'     -> 确定性启发式分类 + （注入 provider 时）内层 LLM 分解
                                    （无 provider 时 multi 也只回退单发——诚实降级）
        - 'llm'                  -> LLM 分类 + LLM 分解；未注入 provider 则 None（诚实降级）
        - 其他                   -> ValueError
    """
    if spec is None:
        spec = os.environ.get(ADAPTIVE_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "none":
        return None
    inner = LLMQueryDecomposer(provider) if provider is not None else None
    if normalized in {"heuristic", "on"}:
        return AdaptiveDecomposer(HeuristicComplexityClassifier(), inner)
    if normalized == "llm":
        if provider is None:
            return None
        return AdaptiveDecomposer(LLMComplexityClassifier(provider), inner)
    raise ValueError(
        f"未知 adaptive spec {spec!r}；可用：none / heuristic / llm（llm 需注入 provider）"
    )
