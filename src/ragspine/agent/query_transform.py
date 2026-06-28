"""W9 查询变换（opt-in，默认关）：HyDE / RAG-Fusion / step-back 在检索前重写/扩展查询。

现状（docs/prd-quality-depth.md W9）：query 侧变换仅确定性——RuleIntentParser 受控同义词多查询
（GlossaryQueryRewriter）+ W6a opt-in LLM 分解。无 HyDE / RAG-Fusion / step-back。本模块沿 QueryRewriter
/ IntentParser 缝（ADR 0010 已把查询改写解耦）加一族 opt-in、LLM 驱动的查询变换，经 make_query_transform
/ RAGSPINE_QUERY_TRANSFORM 选用（mirror make_decomposer），默认 off 字节不变。

统一抽象：QueryTransform.transform(query) -> 检索查询列表（1..N，空=不检索）。QueryTransformRetriever
包裹任一 base NarrativeRetriever：对每个变换查询走 base.retrieve，多查询按 RRF（复用 W1 rrf_fuse）融合、
截断 top_k；单查询即 identity。
- HyDE：LLM 写一段【假设性答案文档】，以其（+原问题）做检索查询——欠规格问题的 dense 召回更好。
- RAG-Fusion：LLM 生成 N 个查询变体，各自检索、RRF 融合（RRF 我们 W1 已自有）。
- step-back：LLM 抽象出更一般性的背景问题，原问题 + step-back 一并检索、融合。

硬约束（守 ADR 0001 确定性 + 反编造 + 安全）：
- **默认关、字节不变**：make_query_transform_retriever('none') 返回 base 本身；未注入即整条主流程不变。
- **隔离继承**：只对 base.retrieve(...) 的输出融合取舍，绝不造片段、绝不直读块库——base
  （NarrativeIndexRetriever）已在出口剔除 RESTRICTED，故输出恒为 base 输出的子集，RESTRICTED 永不出域。
- **HyDE 假设文档是检索探针，绝不可引为事实**：以其做检索查询，召回的是带【真实血缘】的真实块；假设
  文档文本本身绝不进入答案/citation。
- **反编造 + 安全在答案层继承**：变换只决定"用什么查询去检索"，最终答案仍走 answer_question 对【原问题】
  的安全门 + found/not-found 改写——竞品/越权问题照常拒答，home 数字不泄漏。
- **有界 + 确定降级**：变体数上限；provider 故障 / 解析失败 -> 回落 [原查询]（退化普通检索，不崩）。

Adaptive-RAG 见 decompose.py 的 AdaptiveDecomposer（落在 decomposer 缝，门控 W6a 分解）：本仓库反编造
不变量禁止无依据的 parametric/no-retrieval 路由，故 adaptive 只在【单跳/多跳】间路由，绝不答无依据。

LLM 变换非确定，故仅 opt-in 适配器（make_query_transform 且必须注入 provider 才生效）。
"""

import json
import os
from collections.abc import Callable
from datetime import date
from typing import Any, Protocol, runtime_checkable

from ragspine.agent.llm_provider import LLMProvider, ProviderError
from ragspine.retrieval.lexical.retrieval import DEFAULT_RRF_K, rrf_fuse

# 变换选型读取的环境变量名（缺省 spec 时生效）。
QUERY_TRANSFORM_ENV = "RAGSPINE_QUERY_TRANSFORM"

# RAG-Fusion 变体数默认上限（有界，防发散；不含原问题）。
DEFAULT_FUSION_VARIANTS = 4

_HYDE_SYSTEM = (
    "你是检索辅助器。针对用户问题，写一段【假设性的】、像是摘自资料的简短答案段落（2-4 句，"
    "尽量含可能出现在真实文档里的关键词），仅用于检索匹配。只输出该段落，不要解释、不要前后缀。"
)
_RAGFUSION_SYSTEM = (
    "你是查询扩展器。针对用户问题，生成若干个语义相近但措辞不同、有助于召回不同相关文档的检索查询。"
    "只输出一个 JSON 字符串数组，不要任何解释。"
)
_STEPBACK_SYSTEM = (
    "你是 step-back 提问器。把用户的具体问题抽象成一个更一般性的背景问题（便于召回背景资料）。"
    "只输出该抽象问题一行，不要解释、不要前后缀。"
)


@runtime_checkable
class QueryTransform(Protocol):
    """查询变换协议：一个问句 -> 1..N 个检索查询（空表=不检索）。

    实现可为非确定（LLM），故只作 opt-in 注入件——默认 None=不变换，主流程字节不变。
    约定：失败时返回 [原查询]（退化普通检索），绝不抛死、绝不返回会让召回崩塌的空表（空表仅
    用于"明确不检索"的语义，当前三个变换都不产空表）。
    """

    def transform(
        self, query: str, *, reference_date: date | None = None
    ) -> list[str]: ...


@runtime_checkable
class NarrativeRetriever(Protocol):
    """叙事检索协议（duck-typed，结构等同 agent.NarrativeRetriever）：本层既包裹它、也实现它。"""

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]: ...


def _snippet_text(snippet: dict[str, Any]) -> str:
    """片段文本访问器（镜像 agent._snippet_text）：text 优先、content 兜底、缺失为空串。"""
    return str(snippet.get("text") or snippet.get("content") or "")


def _snippet_key(snippet: dict[str, Any]) -> str:
    """片段融合主键：chunk_id 优先、doc_id 兜底、再退文本——确定、稳定（同一片段恒同键）。"""
    return str(snippet.get("chunk_id") or snippet.get("doc_id") or _snippet_text(snippet))


def _dedup_preserve_order(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _parse_str_array(text: str) -> list[str]:
    """从模型回文鲁棒解析 JSON 字符串数组；不合规一律返回空表（调用方据此降级）。"""
    try:
        parsed = json.loads(text.strip())
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [s.strip() for s in parsed if isinstance(s, str) and s.strip()]


class HyDETransform:
    """HyDE（Hypothetical Document Embeddings）：LLM 写假设答案文档，以其做检索查询。

    include_original=True（默认）：返回 [原问题, 假设文档]（两者一并检索、RRF 融合，稳健抗坏假设）；
    False：纯 HyDE，仅 [假设文档]。provider 故障 / 空回文 -> [原问题]（退化普通检索）。
    """

    def __init__(self, provider: LLMProvider, *, include_original: bool = True):
        self.provider = provider
        self.include_original = include_original

    def transform(
        self, query: str, *, reference_date: date | None = None
    ) -> list[str]:
        try:
            resp = self.provider.chat([
                {"role": "system", "content": _HYDE_SYSTEM},
                {"role": "user", "content": query},
            ])
        except ProviderError:
            return [query]
        doc = (resp.choices[0].message.content or "").strip()
        if not doc:
            return [query]
        return _dedup_preserve_order([query, doc] if self.include_original else [doc])


class RAGFusionTransform:
    """RAG-Fusion：LLM 生成 N 个查询变体，原问题 + 变体一并检索、RRF 融合。

    n_variants 上限（有界）；provider 故障 / 解析失败 -> [原问题]（退化普通检索）。
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        n_variants: int = DEFAULT_FUSION_VARIANTS,
        include_original: bool = True,
    ):
        self.provider = provider
        self.n_variants = max(1, n_variants)
        self.include_original = include_original

    def transform(
        self, query: str, *, reference_date: date | None = None
    ) -> list[str]:
        try:
            resp = self.provider.chat([
                {"role": "system", "content": _RAGFUSION_SYSTEM},
                {"role": "user", "content": query},
            ])
        except ProviderError:
            return [query]
        variants = _parse_str_array(resp.choices[0].message.content or "")[: self.n_variants]
        base = [query] if self.include_original else []
        out = _dedup_preserve_order([*base, *variants])
        return out or [query]


class StepBackTransform:
    """step-back prompting：LLM 抽象出更一般性的背景问题，原问题 + step-back 一并检索、融合。

    provider 故障 / 空回文 -> [原问题]（退化普通检索）。
    """

    def __init__(self, provider: LLMProvider, *, include_original: bool = True):
        self.provider = provider
        self.include_original = include_original

    def transform(
        self, query: str, *, reference_date: date | None = None
    ) -> list[str]:
        try:
            resp = self.provider.chat([
                {"role": "system", "content": _STEPBACK_SYSTEM},
                {"role": "user", "content": query},
            ])
        except ProviderError:
            return [query]
        text = (resp.choices[0].message.content or "").strip()
        stepback = text.splitlines()[0].strip() if text else ""
        if not stepback:
            return [query]
        return _dedup_preserve_order(
            [query, stepback] if self.include_original else [stepback]
        )


class QueryTransformRetriever:
    """对变换后的查询集做 base 检索 + RRF 融合（实现 NarrativeRetriever 协议）。

    包裹任一 base：transform(query) -> 查询表；单查询 identity 透传；多查询逐个 base.retrieve 后按
    RRF（复用 W1 rrf_fuse）融合、截断 top_k。本层绝不造片段（隔离继承，见模块 docstring）。

    确定性：base 确定 + transform 确定 => 可复现（LLM transform 非确定，故整链默认关、opt-in）。
    """

    def __init__(
        self,
        base: NarrativeRetriever,
        transform: QueryTransform,
        *,
        rrf_k: float = DEFAULT_RRF_K,
    ):
        self.base = base
        self.transform = transform
        self.rrf_k = rrf_k

    def retrieve(
        self,
        query: str,
        *,
        filters: dict[str, str] | None = None,
        top_k: int = 50,
    ) -> list[dict[str, object]]:
        queries = self.transform.transform(query)
        if not queries:
            return []
        if len(queries) == 1:
            return self.base.retrieve(queries[0], filters=filters, top_k=top_k)

        rankings: list[list[str]] = []
        by_key: dict[str, dict[str, object]] = {}
        first_seen: dict[str, int] = {}
        order = 0
        for q in queries:
            snippets = self.base.retrieve(q, filters=filters, top_k=top_k)
            ranking: list[str] = []
            for s in snippets:
                key = _snippet_key(s)
                ranking.append(key)
                if key not in by_key:
                    by_key[key] = s
                    first_seen[key] = order
                    order += 1
            rankings.append(ranking)

        fused = rrf_fuse(rankings, k=self.rrf_k)
        # 平分按【首次出现序】稳定兜底——确定、可复现。
        ordered = sorted(
            by_key, key=lambda k: (-fused.get(k, 0.0), first_seen[k])
        )
        return [by_key[k] for k in ordered[:top_k]]


# spec -> (变换工厂)；所有变换都需 provider（LLM 驱动），无 provider 即诚实降级为 None（不变换）。
_TRANSFORM_BUILDERS: dict[str, Callable[[LLMProvider], QueryTransform]] = {
    "hyde": HyDETransform,
    "rag_fusion": RAGFusionTransform,
    "ragfusion": RAGFusionTransform,
    "fusion": RAGFusionTransform,
    "step_back": StepBackTransform,
    "stepback": StepBackTransform,
}


def make_query_transform(
    spec: str | None = None, *, provider: LLMProvider | None = None
) -> QueryTransform | None:
    """变换选型工厂：把「用哪个查询变换」降为一个 spec/env，默认 None=不变换（行为不变）。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_QUERY_TRANSFORM）：
        - None / 'none'                       -> None（不变换；检索走原查询，字节不变）
        - 'hyde'                              -> HyDETransform（需注入 provider，否则 None）
        - 'rag_fusion' / 'fusion'             -> RAGFusionTransform（需 provider）
        - 'step_back' / 'stepback'            -> StepBackTransform（需 provider）
        - 其他                                -> ValueError

    所有变换都 LLM 驱动：未注入 provider 时返回 None（诚实降级为不变换，绝不空跑）。
    """
    if spec is None:
        spec = os.environ.get(QUERY_TRANSFORM_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "none":
        return None
    builder = _TRANSFORM_BUILDERS.get(normalized)
    if builder is None:
        raise ValueError(
            f"未知 query-transform spec：{normalized!r}"
            "（可选 none / hyde / rag_fusion / step_back；均需注入 provider）"
        )
    if provider is None:
        return None
    return builder(provider)


def make_query_transform_retriever(
    base: NarrativeRetriever,
    spec: str | None = None,
    *,
    provider: LLMProvider | None = None,
    **kwargs: Any,
) -> NarrativeRetriever:
    """检索器变换包裹工厂：默认 'none'（或未注入 provider）返回 base 本身（字节不变），变换 opt-in。

    范式同 make_corrective_retriever：transform=None => base 原样返回；否则包成 QueryTransformRetriever。
    隔离继承自 base（其输出已剔除 RESTRICTED）。kwargs（如 rrf_k）透传给 QueryTransformRetriever。
    """
    transform = make_query_transform(spec, provider=provider)
    if transform is None:
        return base
    return QueryTransformRetriever(base, transform, **kwargs)
