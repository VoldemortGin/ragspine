"""A/B 两线集成适配层：B 线检索（NarrativeIndex）接入 A 线编排（NarrativeRetriever 协议）。

只做弥合，不改两线任何既有模块：
    - NarrativeIndexRetriever：把 A 线协议 retrieve(query, *, filters, top_k) 的
      filters dict 转成 NarrativeIndex.retrieve 的元数据 kwargs，把 RetrievalResult
      转成 agent 合成与 citation 所需的 dict（text / doc_id / title / source_locator /
      元数据 / 各通道得分）。
    - ProviderListwiseJudge：用 A 线 LLMProvider 实现 B 线 ListwiseJudge 协议
      （build_listwise_prompt 构造、parse_listwise_response 解析）。provider 异常
      原样上抛，由 listwise_rerank 的 B 线退化语义兜底（返回原序，绝不抛死）。

embedding 现实处理：真实向量后端选型未拍板，本期不接。NarrativeIndex /
HybridRetriever 在 embedding_backend=None 时原生即纯 BM25+RRF 模式，集成默认
走该模式，不伪装任何向量通道。

Restricted 出口拦截：B 线只保证 Restricted 文本不进 judge prompt，但 retrieve
结果中 Restricted 块原位保留；而 A 线会把 snippet 文本送入 LLM 合成 prompt。
为守住「Restricted 不出域」硬约束，适配器在出口处剔除 sensitivity==RESTRICTED
（大小写不敏感）的块。
"""

from pathlib import Path
from typing import Any

from ragspine.agent.llm_provider import LLMProvider
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.lexical.retrieval import (
    EmbeddingBackend,
    GlossaryQueryRewriter,
    NarrativeIndex,
    RetrievableIndex,
    RetrievalResult,
)
from ragspine.retrieval.rerank.listwise_rerank import (
    RESTRICTED_SENSITIVITY,
    build_listwise_prompt,
    parse_listwise_response,
)

# A 线 filters dict 中允许透传给 NarrativeIndex.retrieve 的元数据键。
_FILTER_KEYS = ("topic", "entity", "geography", "period", "language")

_JUDGE_SYSTEM = "你是检索精排员，严格按用户指令输出，不输出任何额外内容。"


class ProviderListwiseJudge:
    """ListwiseJudge 的 LLMProvider 实现（适配 Anthropic/OpenAI/Mock 任一 provider）。

    judge() 不自行吞异常：provider 抛错时原样上抛，listwise_rerank 会按 B 线
    退化语义回到原（RRF）序；乱回文由 parse_listwise_response 鲁棒解析兜底。
    """

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        """构造 listwise prompt → provider 单轮调用 → 鲁棒解析为下标排列。"""
        prompt = build_listwise_prompt(query, candidates)
        resp = self.provider.create_message(
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        return parse_listwise_response(resp.text, len(candidates))


class NarrativeIndexRetriever:
    """把 NarrativeIndex 适配成 A 线 NarrativeRetriever 协议的薄转换层。

    retry_without_filters：元数据过滤把候选全滤光时是否去过滤重试一次
    （默认开，召回优先——agent 给出的 entity/period 过滤是收窄信号而非硬约束，
    叙事文档元数据覆盖不全时不应直接答"未检索到"）。
    """

    def __init__(self, index: RetrievableIndex, *, retry_without_filters: bool = True):
        self.index = index
        self.retry_without_filters = retry_without_filters

    def retrieve(
        self, query: str, *, filters: dict[str, Any] | None = None, top_k: int = 50
    ) -> list[dict[str, Any]]:
        """A 线协议入口：filters 映射为元数据 kwargs，结果转 snippet dict。

        - 只透传 _FILTER_KEYS 中的非空键，未知键/空值丢弃；
        - RESTRICTED 块在出口处剔除（不出域，见模块 docstring）。
        """
        kwargs = {
            k: v for k, v in (filters or {}).items() if k in _FILTER_KEYS and v
        }
        results = self.index.retrieve(query, top_k=top_k, **kwargs)
        if not results and kwargs and self.retry_without_filters:
            results = self.index.retrieve(query, top_k=top_k)
        return [
            _to_snippet(r)
            for r in results
            if str(r.chunk.sensitivity).upper() != RESTRICTED_SENSITIVITY
        ]


def _to_snippet(result: RetrievalResult) -> dict[str, Any]:
    """RetrievalResult → A 线 snippet dict（agent 取 text / doc_id / source_locator）。"""
    c = result.chunk
    return {
        "text": c.text,
        "doc_id": c.doc_id,
        "title": c.title,
        "source_locator": c.source_locator,
        "chunk_id": c.chunk_id,
        "topic": c.topic,
        "entity": c.entity,
        "geography": c.geography,
        "period": c.period,
        "language": c.language,
        "sensitivity": c.sensitivity,
        "scores": {
            "bm25": result.bm25_score,
            "vector": result.vector_score,
            "fused": result.fused_score,
        },
    }


def build_narrative_retriever(
    chunk_db: str | Path,
    provider: LLMProvider | None = None,
    *,
    embedding_backend: EmbeddingBackend | None = None,
) -> tuple[NarrativeIndexRetriever, ChunkStore]:
    """开块库并组装默认叙事检索链（CLI/服务接线入口）。

    默认链：纯 BM25+RRF（无向量后端）+ glossary 规则 multi-query 改写 +
    （给了 provider 时）Claude listwise 二审。返回 (retriever, store)，
    store 由调用方负责 close。
    embedding_backend：可选向量通道后端（如 ragspine.retrieval.vector.embedding_backends 的
    OpenAIEmbeddingBackend），默认 None＝纯 BM25 现状，既有调用零影响。
    """
    store = ChunkStore(chunk_db)
    store.init_schema()
    index = NarrativeIndex(
        store,
        embedding_backend=embedding_backend,
        query_rewriter=GlossaryQueryRewriter(),
        judge=ProviderListwiseJudge(provider) if provider is not None else None,
    )
    return NarrativeIndexRetriever(index), store
