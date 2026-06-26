"""叙事通路混合检索：BM25（纯 Python）+ 向量（注入）+ RRF 融合 + 元数据预过滤 + multi-query。

检索配置拍板（docs/architecture.md，2026-06-12）：召回 top-50；覆盖率靠三件套——混合召回
（BM25+向量+RRF）、元数据预过滤（过滤在打分之前）、multi-query 改写；精排走 Claude
listwise 二审（ragspine.retrieval.rerank.listwise_rerank，Restricted 不出域）。

依赖注入（范式同 src/ragspine/extraction/extractors/pdf_scanned_extractor 的 OcrBackend）：
    - EmbeddingBackend：embed_texts(list[str]) -> list[list[float]]，真实现（GenAI Hub
      网关等）由集成线提供，本模块零 SDK；测试用确定性 fake。
    - QueryRewriter：rewrite(query) -> list[str]（含原 query）；默认提供基于
      ragspine.common.glossary 同义词的确定性规则改写器（只读复用 glossary）。

BM25 为标准 Okapi（k1=1.5、b=0.75，idf = ln(1 + (N-df+0.5)/(df+0.5))），不引入
rank-bm25 等新依赖。分词处理中英混排：ASCII 连续串按词、CJK 按字符 unigram+bigram、
大小写归一。RRF 融合 k=60（标准值，可参数化），rank 从 1 起：score += 1/(k+rank)。
"""

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ragspine.common.glossary import ENTITY_SYNONYMS, METRIC_SYNONYMS
from ragspine.pipeline.graph import PipelineGraph
from ragspine.retrieval.chunking.chunk_store import ChunkStore, StoredChunk
from ragspine.retrieval.chunking.chunking import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    Chunk,
    DocumentMeta,
    chunk_document,
)
from ragspine.retrieval.contextual import IndexTextFn
from ragspine.retrieval.rerank.listwise_rerank import DEFAULT_TOP_N, ListwiseJudge, listwise_rerank
from ragspine.retrieval.vector.persistence_policy import IsolationFirstPolicy, PersistencePolicy
from ragspine.retrieval.vector.store import InProcessVectorStore, VectorRecord, VectorStore

DEFAULT_TOP_K = 50      # 拍板召回深度（docs/architecture.md）。
DEFAULT_RRF_K = 60      # RRF 标准值。
DEFAULT_BM25_K1 = 1.5   # Okapi 标准参数。
DEFAULT_BM25_B = 0.75

# CJK 统一表意文字（基本区 + 扩展 A + 兼容区），分词用。
_CJK_RANGE = "㐀-䶿一-鿿豈-﫿"
_TOKEN_RE = re.compile(rf"[a-z0-9]+|[{_CJK_RANGE}]+")


@runtime_checkable
class EmbeddingBackend(Protocol):
    """向量通道后端协议（依赖注入点，零 SDK）。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量文本 -> 向量（顺序对齐）。"""
        ...


@runtime_checkable
class QueryRewriter(Protocol):
    """multi-query 改写协议：rewrite(query) -> list[str]，须含原 query。"""

    def rewrite(self, query: str) -> list[str]:
        ...


def tokenize(text: str) -> list[str]:
    """中英混排分词：小写归一；ASCII 字母数字连续串按词；CJK 串出 unigram + 相邻 bigram。"""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        run = match.group(0)
        if run[0].isascii():
            tokens.append(run)
        else:
            tokens.extend(run)  # unigram
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))  # bigram
    return tokens


def bm25_scores(
    query_tokens: list[str],
    docs_tokens: list[list[str]],
    k1: float = DEFAULT_BM25_K1,
    b: float = DEFAULT_BM25_B,
) -> list[float]:
    """标准 Okapi BM25：对每篇文档算 query 的得分（idf = ln(1+(N-df+0.5)/(df+0.5))）。"""
    n_docs = len(docs_tokens)
    if n_docs == 0 or not query_tokens:
        return [0.0] * n_docs
    doc_counters = [Counter(tokens) for tokens in docs_tokens]
    avgdl = sum(len(tokens) for tokens in docs_tokens) / n_docs

    scores = [0.0] * n_docs
    for term, query_tf in Counter(query_tokens).items():
        df = sum(1 for counter in doc_counters if term in counter)
        if df == 0:
            continue
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        for d, counter in enumerate(doc_counters):
            tf = counter.get(term, 0)
            if tf == 0:
                continue
            dl = len(docs_tokens[d])
            denom = tf + k1 * (1 - b + b * dl / avgdl)
            scores[d] += query_tf * idf * tf * (k1 + 1) / denom
    return scores


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度；零向量一律 0.0。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def rrf_fuse(rankings: list[list[str]], k: float = DEFAULT_RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion：多份 ranked id 列表 -> id 融合得分（rank 从 1 起）。"""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + rank)
    return fused


class GlossaryQueryRewriter:
    """基于 ragspine.common.glossary 同义词的确定性规则改写器（只读复用，不修改 glossary）。

    在归一化后的 query 中查找指标/实体同义词词条（ASCII 词条要求词边界，避免
    'cn' 命中 'concern'），每个命中词条生成「替换为受控代码 / 其他同义词」的变体，
    原 query 恒在首位，整体去重并按 max_queries 截断。纯规则，零 LLM。
    """

    def __init__(self, max_queries: int = 5):
        self.max_queries = max_queries

    def rewrite(self, query: str) -> list[str]:
        """query -> [原 query, 变体...]（确定性，长度 <= max_queries）。"""
        queries = [query]
        lowered = query.lower()

        # 命中词条：(start, end, term, code, 所属词典)，按词典定义顺序确定性扫描。
        matches: list[tuple[int, int, str, str, dict[str, str]]] = []
        for vocab in (METRIC_SYNONYMS, ENTITY_SYNONYMS):
            for term, code in vocab.items():
                span = self._find_term(lowered, term)
                if span is not None:
                    matches.append((*span, term, code, vocab))

        def add(candidate: str) -> None:
            if candidate not in queries and len(queries) < self.max_queries:
                queries.append(candidate)

        # 第一轮：替换为受控代码（最强归一信号优先占名额）。
        for start, end, _term, code, _vocab in matches:
            add(query[:start] + code + query[end:])
        # 第二轮：替换为同 code 的其他同义词。
        for start, end, term, code, vocab in matches:
            for synonym, syn_code in vocab.items():
                if syn_code == code and synonym != term:
                    add(query[:start] + synonym + query[end:])
        return queries

    @staticmethod
    def _find_term(lowered_query: str, term: str) -> tuple[int, int] | None:
        """在小写化 query 中找词条：ASCII 词条要求词边界，CJK 词条子串匹配。"""
        if term.isascii():
            match = re.search(rf"\b{re.escape(term)}\b", lowered_query)
            return (match.start(), match.end()) if match else None
        pos = lowered_query.find(term)
        return (pos, pos + len(term)) if pos >= 0 else None


@dataclass
class RetrievalResult:
    """一条检索结果：块全量元数据 + 各通道得分 + 融合得分（可解释性）。

    bm25_score / vector_score 取该块在各改写 query 下对应通道的最高分；
    fused_score 为 RRF 融合得分（排序依据）。
    """

    chunk: StoredChunk | Chunk
    bm25_score: float
    vector_score: float
    fused_score: float


# 入 VectorStore 的过滤维度（与 search 的元数据预过滤、where 下推同一组键）。
_FILTER_DIMS = ("topic", "entity", "geography", "period", "language")


def _record_metadata(chunk: StoredChunk | Chunk) -> dict[str, str]:
    """构造 VectorRecord.metadata：5 个过滤维度 + doc_id，str 归一（None -> ''），口径同预过滤。

    5 维恒含（含空串）+ str 强制，确保 store 的 where 精确匹配 python 预过滤；额外带 doc_id
    供【doc 粒度失效】（NarrativeIndex 重入时 delete(where={doc_id})，不全清）。doc_id 不进
    检索 where（检索按 5 维过滤），故不影响打分/排序的逐位等价。None 防御性归一为 ''。
    """
    md = {
        name: ("" if (val := getattr(chunk, name, "")) is None else str(val))
        for name in _FILTER_DIMS
    }
    md["doc_id"] = str(getattr(chunk, "doc_id", "") or "")
    return md


def _index_text(chunk: StoredChunk | Chunk, index_text_fn: IndexTextFn | None) -> str:
    """块的【索引/嵌入文本】：注入了 index_text_fn 则用之（W4a contextual 确定性情境头进索引），
    否则用原文 chunk.text（默认，逐位等价旧行为）。citation 用的 chunk.text 始终不被改动。
    """
    return index_text_fn(chunk) if index_text_fn is not None else chunk.text


class HybridRetriever:
    """混合检索器：元数据预过滤 -> (BM25 + 向量) x multi-query -> RRF -> top_k。

    embedding_backend 缺省 None 时为纯 BM25 模式（向量通道关闭）；块向量按 chunk_id 惰性
    计算并缓存（_embedding_cache，预过滤严格先于任何 embedding），向量【打分】则委托给
    可插拔的 VectorStore 缝（默认零依赖内存实现 InProcessVectorStore，行为等价于原内联
    cosine 暴力扫；注入则复用调用方持有的 store——未来 sqlite-vec / Qdrant adapter 即由此接入）。

    前置约定：候选 chunk_id 唯一（与 search 内 by_id={c.chunk_id:c} 的既有假设一致）。
    """

    def __init__(
        self,
        chunks: Sequence[StoredChunk | Chunk],
        *,
        embedding_backend: EmbeddingBackend | None = None,
        query_rewriter: QueryRewriter | None = None,
        k1: float = DEFAULT_BM25_K1,
        b: float = DEFAULT_BM25_B,
        rrf_k: float = DEFAULT_RRF_K,
        top_k: int = DEFAULT_TOP_K,
        embedding_cache: dict[str, list[float]] | None = None,
        vector_store: VectorStore | None = None,
        manage_vectors: bool = True,
        index_text_fn: IndexTextFn | None = None,
    ):
        self.chunks = list(chunks)
        self.embedding_backend = embedding_backend
        self.query_rewriter = query_rewriter
        self.k1 = k1
        self.b = b
        self.rrf_k = rrf_k
        self.top_k = top_k
        # 索引/嵌入文本缝（W4a contextual，opt-in）：None=用 chunk.text（默认，逐位等价旧行为）；
        # 注入 contextual_index_text 则给 BM25 分词与块向量嵌入拼确定性情境头，citation 原文不变。
        self.index_text_fn = index_text_fn
        # 块向量归谁管：True（默认）= 检索器惰性 embed 候选并 upsert（直连用法，逐位等价原内联）；
        # False = store-managed，块向量已由上层（NarrativeIndex 入库即嵌入落盘）灌好，检索只嵌 query
        # 并查 store，绝不重嵌块——持久化（sqlite-vec）由此「重启不重算」真正生效。
        self.manage_vectors = manage_vectors
        # 块向量缓存（chunk_id -> 向量），可由外层（NarrativeIndex）注入共享。
        self._embedding_cache: dict[str, list[float]] = (
            embedding_cache if embedding_cache is not None else {}
        )
        # 向量打分缝：有 embedding 后端时解析有效 store（注入优先，否则自建零依赖内存默认）；
        # 纯 BM25 时保持 None（无向量通道，拓扑亦据此判定）。
        if embedding_backend is not None:
            self.vector_store: VectorStore | None = (
                vector_store if vector_store is not None else InProcessVectorStore()
            )
        else:
            self.vector_store = vector_store

    def search(
        self,
        query: str,
        *,
        topic: str | None = None,
        entity: str | None = None,
        geography: str | None = None,
        period: str | None = None,
        language: str | None = None,
        top_k: int | None = None,
    ) -> list[RetrievalResult]:
        """检索：先按元数据过滤候选，再对候选打分融合，返回 top_k（默认 50）。

        排序：fused_score 降序，平分按 chunk_id 升序（确定性）。
        纯 BM25 模式下全 query 零命中的块不进结果。
        """
        limit = self.top_k if top_k is None else top_k

        # 1) 元数据预过滤：严格发生在任何打分/embedding 之前。
        filters = {
            "topic": topic,
            "entity": entity,
            "geography": geography,
            "period": period,
            "language": language,
        }
        candidates = [
            c
            for c in self.chunks
            if all(getattr(c, name) == val for name, val in filters.items() if val is not None)
        ]
        if not candidates:
            return []
        by_id = {c.chunk_id: c for c in candidates}

        # 2) multi-query 改写（含原 query），去重保序。
        raw_queries = self.query_rewriter.rewrite(query) if self.query_rewriter else [query]
        queries = list(dict.fromkeys(raw_queries))

        # 3) 各通道打分与排名（仅对通过预过滤的候选）。
        #    索引文本经 index_text_fn（W4a contextual，默认恒等于 c.text）——BM25 分词与块向量嵌入
        #    都吃它，让情境头进索引；citation 用的 c.text 不受影响。
        docs_tokens = [tokenize(_index_text(c, self.index_text_fn)) for c in candidates]
        use_vector = self.embedding_backend is not None and self.vector_store is not None
        if use_vector:
            assert self.embedding_backend is not None and self.vector_store is not None
            if self.manage_vectors:
                # 检索器自管块向量：惰性入缓存（embed 仅对未缓存候选——严格守「预过滤先于打分」），
                # 再灌入 VectorStore 缝；打分/排名由 store.query 负责（替代原内联 cosine 暴力扫）。
                missing = [c for c in candidates if c.chunk_id not in self._embedding_cache]
                if missing:
                    vectors = self.embedding_backend.embed_texts(
                        [_index_text(c, self.index_text_fn) for c in missing]
                    )
                    for c, vec in zip(missing, vectors, strict=False):
                        self._embedding_cache[c.chunk_id] = vec
                self.vector_store.upsert(
                    [
                        VectorRecord(
                            id=c.chunk_id,
                            vector=tuple(self._embedding_cache[c.chunk_id]),
                            metadata=_record_metadata(c),
                        )
                        for c in candidates
                    ]
                )
            # store-managed（manage_vectors=False）：块向量已由上层落盘，跳过 embed/upsert，
            # 只嵌 query 查 store；未落盘的候选（如 RESTRICTED）store 不返回 -> 向量分 0（仍走 BM25）。
            # where 下推与 python 预过滤同口径（`if val is not None`，故 '' 是真实过滤值）；
            # 值 str 归一与 _record_metadata 对称——两侧绝不因类型不一致而错配（今值皆 str，为防御）。
            # 候选已预过滤，where 对每检索器私有默认 store 冗余但无害，对共享/超集 store 则保正确。
            vector_where = {
                name: str(val) for name, val in filters.items() if val is not None
            }

        rankings: list[list[str]] = []
        best_bm25: dict[str, float] = {}
        best_vector: dict[str, float] = {}
        for q in queries:
            scores = bm25_scores(tokenize(q), docs_tokens, self.k1, self.b)
            hit = sorted(
                ((s, c.chunk_id) for s, c in zip(scores, candidates, strict=False) if s > 0.0),
                key=lambda pair: (-pair[0], pair[1]),
            )
            rankings.append([cid for _, cid in hit])
            for s, cid in hit:
                best_bm25[cid] = max(best_bm25.get(cid, 0.0), s)

            if use_vector:
                assert self.embedding_backend is not None and self.vector_store is not None
                query_vec = self.embedding_backend.embed_texts([q])[0]
                # k=len(candidates)（非 store 默认 50）：拿回全部候选的向量排名，含 cosine=0 者，
                # 故 best_vector 对每个候选都有显式分（与原内联 zip(sims,candidates) 逐位等价）。
                vector_hits = [
                    h
                    for h in self.vector_store.query(query_vec, k=len(candidates), where=vector_where)
                    if h.id in by_id  # 兜底：只取当前候选（共享/超集 store 下安全）
                ]
                rankings.append([h.id for h in vector_hits])
                for h in vector_hits:
                    best_vector[h.id] = max(best_vector.get(h.id, 0.0), h.score)

        # 4) RRF 融合 -> top_k（确定性平分破除）。
        fused = rrf_fuse(rankings, self.rrf_k)
        ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
        return [
            RetrievalResult(
                chunk=by_id[cid],
                bm25_score=best_bm25.get(cid, 0.0),
                vector_score=best_vector.get(cid, 0.0),
                fused_score=score,
            )
            for cid, score in ordered[:limit]
        ]

    def topology(self) -> PipelineGraph:
        """返回本检索器子管线的静态拓扑（反映向量/multi-query 是否接入）。

        薄委托：构建逻辑在 ragspine.pipeline.retriever_topology（duck-typed，不反向依赖）；
        惰性 import 该构建器（其经 pipeline 包 __init__ 拉入 topology 模块）避免 import 环——
        值类型 PipelineGraph 走叶子模块 ragspine.pipeline.graph 顶层 import（无环）。
        """
        from ragspine.pipeline import retriever_topology

        return retriever_topology(self)


@runtime_checkable
class RetrievableIndex(Protocol):
    """检索接口（结构化）：任何带 retrieve(query, …) -> list[RetrievalResult] 的索引。

    NarrativeIndexRetriever 适配器只依赖这个结构（面向接口），不绑死具体 NarrativeIndex
    ——这正是测试能用轻量替身的原因，也让运行时 beartype 检查放行任何 retrieve-兼容实现。
    """

    def retrieve(
        self,
        query: str,
        *,
        topic: str | None = None,
        entity: str | None = None,
        geography: str | None = None,
        period: str | None = None,
        language: str | None = None,
        top_k: int | None = None,
        rerank: bool = True,
        top_n: int | None = None,
    ) -> list[RetrievalResult]: ...


class NarrativeIndex:
    """叙事通路端到端入口：建库（切块+入库）-> 混合检索 -> 可选 Claude listwise 二审。

    所有 LLM/embedding 依赖只经注入协议表达（EmbeddingBackend / QueryRewriter /
    ListwiseJudge），不 import 任何 provider/SDK，集成由主控完成。
    """

    def __init__(
        self,
        store: ChunkStore,
        *,
        embedding_backend: EmbeddingBackend | None = None,
        query_rewriter: QueryRewriter | None = None,
        judge: ListwiseJudge | None = None,
        max_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
        top_k: int = DEFAULT_TOP_K,
        rerank_top_n: int = DEFAULT_TOP_N,
        vector_store: VectorStore | None = None,
        persistence_policy: PersistencePolicy | None = None,
        index_text_fn: IndexTextFn | None = None,
    ):
        self.store = store
        self.embedding_backend = embedding_backend
        self.query_rewriter = query_rewriter
        self.judge = judge
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars
        self.top_k = top_k
        self.rerank_top_n = rerank_top_n
        # 索引/嵌入文本缝（W4a contextual，opt-in）：入库即嵌入与检索期 BM25 都吃它；
        # None=用 chunk.text（默认，逐位等价旧行为）。citation 原文 chunk.text 始终不变。
        self.index_text_fn = index_text_fn
        # 跨 retrieve 共享的向量打分缝（注入优先，否则有 embedding 后端时自建内存默认）；
        # 块向量在【入库时】嵌入并写进此 store（store-managed），retrieve 复用、不重嵌。
        if embedding_backend is not None:
            self.vector_store: VectorStore | None = (
                vector_store if vector_store is not None else InProcessVectorStore()
            )
        else:
            self.vector_store = vector_store
        # 持久化门控（可插拔缝）：决定哪些块的向量可写盘。默认隔离优先——绝不落盘 RESTRICTED。
        self.persistence_policy = persistence_policy or IsolationFirstPolicy()

    def ingest(self, text: str, meta: DocumentMeta, valid_as_of: str = "") -> int:
        """切块 + 幂等入库 + 入库即嵌入落盘（policy 门控、doc 粒度失效），返回入库块数。"""
        chunks = chunk_document(
            text, meta, max_chars=self.max_chars, overlap_chars=self.overlap_chars
        )
        self.store.replace_doc_chunks(meta.doc_id, chunks, valid_as_of=valid_as_of)
        if self.vector_store is not None:
            # doc 粒度失效：只撤下本 doc 的旧向量，其它 doc 的持久向量存活（非 delete-all）。
            self.vector_store.delete(where={"doc_id": meta.doc_id})
            if self.embedding_backend is not None:
                # 入库即嵌入落盘（policy 门控）：可落盘块的向量写进持久库，重启/换实例不重算。
                # RESTRICTED 块默认被 IsolationFirstPolicy 挡下——其衍生向量绝不写盘。
                persistable = [c for c in chunks if self.persistence_policy.persistable(c)]
                if persistable:
                    vectors = self.embedding_backend.embed_texts(
                        [_index_text(c, self.index_text_fn) for c in persistable]
                    )
                    self.vector_store.upsert(
                        [
                            VectorRecord(id=c.chunk_id, vector=tuple(vec), metadata=_record_metadata(c))
                            for c, vec in zip(persistable, vectors, strict=False)
                        ]
                    )
        return len(chunks)

    def retrieve(
        self,
        query: str,
        *,
        topic: str | None = None,
        entity: str | None = None,
        geography: str | None = None,
        period: str | None = None,
        language: str | None = None,
        top_k: int | None = None,
        rerank: bool = True,
        top_n: int | None = None,
    ) -> list[RetrievalResult]:
        """混合检索 +（给了 judge 且 rerank=True 时）listwise 二审。

        二审遵守 ragspine.retrieval.rerank.listwise_rerank 的 Restricted 不出域策略；judge 缺省或
        rerank=False 时直接返回 RRF 序 top_k。
        """
        # 预过滤下推到块库（iter_chunks 即"打分之前"的元数据过滤）。
        chunks = self.store.iter_chunks(
            topic=topic,
            entity=entity,
            geography=geography,
            period=period,
            language=language,
        )
        retriever = HybridRetriever(
            chunks,
            embedding_backend=self.embedding_backend,
            query_rewriter=self.query_rewriter,
            top_k=self.top_k,
            vector_store=self.vector_store,
            manage_vectors=False,  # 块向量已在入库时落盘——检索只嵌 query、查 store，绝不重嵌块。
            index_text_fn=self.index_text_fn,  # contextual 情境头与入库口径一致（默认 None=不变）。
        )
        results = retriever.search(query, top_k=top_k)
        if not rerank or self.judge is None:
            return results
        return listwise_rerank(
            query,
            results,
            self.judge,
            top_n=self.rerank_top_n if top_n is None else top_n,
        )
