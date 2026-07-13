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
from ragspine.retrieval.postprocess import NodePostprocessor
from ragspine.retrieval.rerank.listwise_rerank import (
    RESTRICTED_SENSITIVITY,
    ListwiseJudge,
    build_listwise_prompt,
    parse_listwise_response,
)
from ragspine.retrieval.vector.persistence_policy import PersistencePolicy
from ragspine.retrieval.vector.store import VectorStore

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
        resp = self.provider.chat([
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": prompt},
        ])
        return parse_listwise_response(resp.choices[0].message.content or "", len(candidates))


class NarrativeIndexRetriever:
    """把 NarrativeIndex 适配成 A 线 NarrativeRetriever 协议的薄转换层。

    retry_without_filters：元数据过滤把候选全滤光时是否去过滤重试一次
    （默认开，召回优先——agent 给出的 entity/period 过滤是收窄信号而非硬约束，
    叙事文档元数据覆盖不全时不应直接答"未检索到"）。

    postprocessor：可选后检索 postprocessor 链（W8，任一 NodePostprocessor，如
    make_postprocessor('mmr,lost_in_middle')）；给了即在【RESTRICTED 出口剔除之后】对已剥离的 snippet
    子集做重排/去冗余/压缩，故隔离继承自出口（RESTRICTED 永不进入 postprocessor）。默认 None＝不挂链，
    retrieve 输出字节不变。
    """

    def __init__(
        self,
        index: RetrievableIndex,
        *,
        retry_without_filters: bool = True,
        postprocessor: NodePostprocessor | None = None,
    ):
        self.index = index
        self.retry_without_filters = retry_without_filters
        self.postprocessor = postprocessor

    def retrieve(
        self, query: str, *, filters: dict[str, Any] | None = None, top_k: int = 50
    ) -> list[dict[str, Any]]:
        """A 线协议入口：filters 映射为元数据 kwargs，结果转 snippet dict。

        - 只透传 _FILTER_KEYS 中的非空键，未知键/空值丢弃；
        - RESTRICTED 块在出口处剔除（不出域，见模块 docstring）；
        - postprocessor（W8，opt-in）在【剔除之后】对已剥离子集做重排/去冗余/压缩（隔离继承）。
        """
        kwargs = {
            k: v for k, v in (filters or {}).items() if k in _FILTER_KEYS and v
        }
        results = self.index.retrieve(query, top_k=top_k, **kwargs)
        if not results and kwargs and self.retry_without_filters:
            results = self.index.retrieve(query, top_k=top_k)
        snippets = [
            _to_snippet(r)
            for r in results
            if str(r.chunk.sensitivity).upper() != RESTRICTED_SENSITIVITY
        ]
        if self.postprocessor is not None:
            snippets = list(self.postprocessor.postprocess(query, snippets))
        return snippets


def _to_snippet(result: RetrievalResult) -> dict[str, Any]:
    """RetrievalResult → A 线 snippet dict（agent 取 text / doc_id / source_locator）。

    父子（small-to-big）展开：块带 window_text（父小节富上下文）时，把它写进【独立的】prompt_text
    键——agent._snippet_text 优先读之作【生成上下文】，而 text / source_locator / chunk_id 仍是命中的
    细 child（citation 诚实、命中证据不被窗口伪装）。window 扩展只影响送 prompt 的上下文，绝不产生新
    检索命中。带 parent_locator（父小节真实段落跨度）时附带之，作 provenance 的父级回指（非命中证据）。
    默认切块器 window_text/parent_locator 均为空 → 两键都不加 → snippet 逐位等价旧行为（字节不变）。

    隔离：本函数只对【已过 RESTRICTED 双出口剔除】的块调用（见 NarrativeIndexRetriever.retrieve 的
    出口过滤）——window_text 随其 child 块受同一出口门控，RESTRICTED 块整段被拒（连同其父窗口），
    父上下文绝不经 child 泄漏。
    """
    c = result.chunk
    snippet: dict[str, Any] = {
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
    window_text = getattr(c, "window_text", "") or ""
    if window_text:
        snippet["prompt_text"] = window_text
    parent_locator = getattr(c, "parent_locator", "") or ""
    if parent_locator:
        snippet["parent_locator"] = parent_locator
    return snippet


def build_narrative_retriever(
    chunk_db: str | Path,
    provider: LLMProvider | None = None,
    *,
    embedding_backend: EmbeddingBackend | None = None,
    vector_store: VectorStore | None = None,
    persistence_policy: PersistencePolicy | None = None,
    reranker: ListwiseJudge | None = None,
    postprocessor: NodePostprocessor | None = None,
) -> tuple[NarrativeIndexRetriever, ChunkStore]:
    """开块库并组装默认叙事检索链（CLI/服务接线入口）。

    默认链：纯 BM25+RRF（无向量后端）+ glossary 规则 multi-query 改写 +
    （给了 provider 时）Claude listwise 二审。返回 (retriever, store)，
    store 由调用方负责 close。
    embedding_backend：可选向量通道后端（如 ragspine.retrieval.vector.embedding_backends 的
    OpenAIEmbeddingBackend），默认 None＝纯 BM25 现状，既有调用零影响。
    vector_store：可选向量打分缝（make_vector_store 选型）；默认 None＝有 embedding 后端时
    NarrativeIndex 自建零依赖内存默认，未来 sqlite-vec / Qdrant adapter 即由此注入。
    persistence_policy：可选持久化门控（make_persistence_policy 选型）；默认 None＝隔离优先
    （IsolationFirstPolicy，绝不落盘 RESTRICTED 块向量）。
    reranker：可选离线重排大脑（任一 ListwiseJudge，如 make_reranker('cross_encoder') 给出的本地
    cross-encoder，W2）。给了即作为 NarrativeIndex 的 judge，**优先于** provider 的 LLM listwise；
    默认 None＝退回原行为（给了 provider 用 ProviderListwiseJudge、否则不二审）——默认 loop 字节不变，
    cross-encoder 是 opt-in。无论哪种 judge 都走 listwise_rerank 编排，RESTRICTED 不出域一致守住。
    postprocessor：可选后检索 postprocessor 链（W8，make_postprocessor 选型）；给了即在精排出口之后、
    prompt 组装之前对已 RESTRICTED-剥离的输出做重排/去冗余/压缩。默认 None＝不挂链、retrieve 输出字节不变。
    """
    store = ChunkStore(chunk_db)
    store.init_schema()
    judge: ListwiseJudge | None = reranker
    if judge is None and provider is not None:
        judge = ProviderListwiseJudge(provider)
    index = NarrativeIndex(
        store,
        embedding_backend=embedding_backend,
        query_rewriter=GlossaryQueryRewriter(),
        judge=judge,
        vector_store=vector_store,
        persistence_policy=persistence_policy,
    )
    return NarrativeIndexRetriever(index, postprocessor=postprocessor), store
