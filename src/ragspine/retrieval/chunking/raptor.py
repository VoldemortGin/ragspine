"""RAPTOR 多粒度树（W10，Chunker 缝的 opt-in 实现，默认仍 DefaultChunker）。

RAPTOR（Recursive Abstractive Processing for Tree-Organized Retrieval, Sarthi et al. 2024）：递归地把块
聚类、对每簇生成摘要、再对摘要聚类……得到一棵从细节（叶）到主题（内部节点）的树。检索可拉一个叶
（细节）或一个内部节点（主题），填全局/多跳综合问题的空白——与 W7b 叙事 GraphRAG 并列的第二条全局
综合路线。对标 LlamaIndex RAPTOR pack、RAGFlow RAPTOR。

本实现把 RAPTOR 落在 Chunker 缝上（per-document 树）：chunk(text, meta) 先用 DefaultChunker 切叶，再
递归聚类 + 摘要，返回【叶 + 全部摘要节点】的扁平列表（摘要节点 is_synthesis=True、parent_id 串成树）。
跨文档的【全局】RAPTOR 树（在 ingestion 期对全库块建树）是 follow-up（见 PRD W10）。

硬约束（守 ADR 0001 确定性 + 反编造 + provenance）：
- **聚类确定**：默认【连通分量】聚类（在 cosine >= 阈值的相似图上求连通分量，按下标序遍历，确定）。
  论文的 UMAP + GMM 是 follow-up（需重依赖）。
- **摘要可确定**：默认 ExtractiveSummarizer（抽取式中心句，零 LLM、零网络、确定）——故 RAPTOR 默认
  即可离线确定性建树；LLMSummarizer（[llm]）为 opt-in 抽象式摘要，provider 故障回落抽取式。
- **摘要绝不可引为事实**：摘要节点 is_synthesis=True（同 W5/W7b 反编造纪律）。
- **每节点带血缘**：摘要节点 doc_id = 文档、para 范围 = 其子树叶覆盖的段范围、source_locator 同
  chunk_document 口径——溯源到「本摘要概括了文档的哪段范围」。
- **有界**：max_levels 上限、min_cluster_size 下限（防退化）。

嵌入后端默认零依赖确定性 DeterministicEmbeddingBackend（同 SemanticChunker）；真语义注入 ONNX 后端。
"""

import math
import re
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ragspine.retrieval.chunking.chunking import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    Chunk,
    DocumentMeta,
    chunk_document,
)
from ragspine.retrieval.lexical.retrieval import EmbeddingBackend

# 默认聚类相似阈值：cosine >= 阈值的两节点连边，连通分量即一簇。
DEFAULT_CLUSTER_SIMILARITY = 0.5
# 默认树高上限与最小簇规模（有界，防退化）。
DEFAULT_MAX_LEVELS = 3
DEFAULT_MIN_CLUSTER_SIZE = 2
# 抽取式摘要默认保留中心句数。
DEFAULT_SUMMARY_SENTENCES = 3

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")
_SUMMARY_SENT_RE = re.compile(r"[^。！？!?；;\n]+")


def _token_set(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@runtime_checkable
class ClusterSummarizer(Protocol):
    """簇摘要缝：一簇文本 -> 一段摘要。默认 ExtractiveSummarizer（确定性）；LLMSummarizer 为 opt-in。"""

    def summarize(self, texts: list[str]) -> str: ...


class ExtractiveSummarizer:
    """确定性抽取式摘要（零 LLM、零网络）：选与簇内其它句重叠最高的【中心句】，按原序拼回。

    句少于 max_sentences 时全保留。中心性 = 该句 token 集与其它句 token 集的 Jaccard 之和（确定）。
    平分按原序——稳定。
    """

    def __init__(self, max_sentences: int = DEFAULT_SUMMARY_SENTENCES):
        if max_sentences < 1:
            raise ValueError(f"max_sentences 须 >= 1，得到 {max_sentences}")
        self.max_sentences = max_sentences

    def summarize(self, texts: list[str]) -> str:
        sentences: list[str] = []
        for t in texts:
            sentences.extend(s.strip() for s in _SUMMARY_SENT_RE.findall(t) if s.strip())
        if not sentences:
            return ""
        if len(sentences) <= self.max_sentences:
            return "。".join(sentences)
        tok = [_token_set(s) for s in sentences]
        centrality = [
            sum(_jaccard(tok[i], tok[j]) for j in range(len(sentences)) if j != i)
            for i in range(len(sentences))
        ]
        # 选中心性最高的 max_sentences 个下标（平分按原序：先按 -centrality 再按下标排，取前 K）。
        top = sorted(range(len(sentences)), key=lambda i: (-centrality[i], i))[
            : self.max_sentences
        ]
        for_order = sorted(top)
        return "。".join(sentences[i] for i in for_order)


class LLMSummarizer:
    """LLM 抽象式摘要（opt-in，[llm]）：单轮让 provider 概括一簇文本；provider 故障 -> 抽取式兜底。"""

    _SYSTEM = (
        "你是资料摘要器。把以下若干段落概括成一段简洁、忠实于原文的摘要（不要编造原文没有的事实，"
        "不要前后缀解释）。"
    )

    def __init__(self, provider: object, *, fallback: ClusterSummarizer | None = None):
        self.provider = provider
        self.fallback = fallback or ExtractiveSummarizer()

    def summarize(self, texts: list[str]) -> str:
        # 延迟 import，避免 chunking 域硬依赖 agent provider 错误类型。
        from ragspine.agent.llm_provider import ProviderError

        joined = "\n\n".join(texts)
        try:
            resp = self.provider.chat([  # type: ignore[attr-defined]
                {"role": "system", "content": self._SYSTEM},
                {"role": "user", "content": joined},
            ])
        except ProviderError:
            return self.fallback.summarize(texts)
        text = (resp.choices[0].message.content or "").strip()
        return text or self.fallback.summarize(texts)


def _connected_components(
    vectors: list[list[float]], threshold: float
) -> list[list[int]]:
    """在 cosine >= threshold 的相似图上求连通分量（确定性：按下标序遍历 + BFS）。

    返回若干分量（各为升序下标列表），分量按其最小下标升序——确定、可复现。
    """
    n = len(vectors)
    adj: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if _cosine(vectors[i], vectors[j]) >= threshold:
                adj[i].append(j)
                adj[j].append(i)
    seen = [False] * n
    components: list[list[int]] = []
    for start in range(n):
        if seen[start]:
            continue
        stack = [start]
        seen[start] = True
        comp: list[int] = []
        while stack:
            node = stack.pop()
            comp.append(node)
            for nb in adj[node]:
                if not seen[nb]:
                    seen[nb] = True
                    stack.append(nb)
        components.append(sorted(comp))
    return components


class _Node:
    """建树中间节点：承载文本 + 其子树叶覆盖的段范围（para_lo/para_hi）+ 已落地的 Chunk（若叶）。"""

    __slots__ = ("text", "para_lo", "para_hi", "chunk")

    def __init__(self, text: str, para_lo: int, para_hi: int, chunk: Chunk):
        self.text = text
        self.para_lo = para_lo
        self.para_hi = para_hi
        self.chunk = chunk


class RaptorChunker:
    """RAPTOR 多粒度树切块器（实现 Chunker 协议）：叶 + 递归聚类摘要节点的扁平列表。

    embedding_backend 默认 None -> 零依赖确定性 DeterministicEmbeddingBackend。summarizer 默认 None ->
    ExtractiveSummarizer（确定性）。clustering 默认连通分量。max_levels / min_cluster_size 有界。
    """

    def __init__(
        self,
        embedding_backend: EmbeddingBackend | None = None,
        summarizer: ClusterSummarizer | None = None,
        *,
        cluster_similarity: float = DEFAULT_CLUSTER_SIMILARITY,
        max_levels: int = DEFAULT_MAX_LEVELS,
        min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
        cluster_fn: Callable[[list[list[float]], float], list[list[int]]] | None = None,
    ):
        if max_levels < 1:
            raise ValueError(f"max_levels 须 >= 1，得到 {max_levels}")
        if min_cluster_size < 2:
            raise ValueError(f"min_cluster_size 须 >= 2，得到 {min_cluster_size}")
        self._backend = embedding_backend
        self.summarizer = summarizer or ExtractiveSummarizer()
        self.cluster_similarity = cluster_similarity
        self.max_levels = max_levels
        self.min_cluster_size = min_cluster_size
        self.cluster_fn = cluster_fn or _connected_components

    def _backend_or_default(self) -> EmbeddingBackend:
        if self._backend is None:
            from ragspine.retrieval.vector.embedding_backends import (
                DeterministicEmbeddingBackend,
            )

            self._backend = DeterministicEmbeddingBackend()
        return self._backend

    def chunk(
        self,
        text: str,
        meta: DocumentMeta,
        *,
        max_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    ) -> list[Chunk]:
        leaves = chunk_document(text, meta, max_chars=max_chars, overlap_chars=overlap_chars)
        if not leaves:
            return []

        prefix = meta.source_locator_prefix or meta.doc_id
        backend = self._backend_or_default()

        all_chunks: list[Chunk] = list(leaves)
        next_seq = len(leaves)  # 摘要节点 seq 从叶之后连续编号（store 唯一索引 (doc_id,version,seq)）
        synth_counter = 0

        # 当前层节点（首层 = 叶）。
        level_nodes: list[_Node] = [
            _Node(c.text, c.para_start, c.para_end, c) for c in leaves
        ]

        for level in range(1, self.max_levels + 1):
            if len(level_nodes) < self.min_cluster_size:
                break
            vectors = backend.embed_texts([n.text for n in level_nodes])
            components = self.cluster_fn(vectors, self.cluster_similarity)
            # 只对规模达标的簇生成摘要节点；达标簇数 < 1 时无可归并 -> 停。
            clusters = [c for c in components if len(c) >= self.min_cluster_size]
            if not clusters:
                break

            parents: list[_Node] = []
            for comp in clusters:
                members = [level_nodes[i] for i in comp]
                summary_text = self.summarizer.summarize([m.text for m in members])
                if not summary_text:
                    continue
                para_lo = min(m.para_lo for m in members)
                para_hi = max(m.para_hi for m in members)
                para_part = (
                    f"para{para_lo}" if para_lo == para_hi else f"para{para_lo}-{para_hi}"
                )
                synth_id = f"{meta.doc_id}#r{level}_{synth_counter}"
                synth_counter += 1
                synth = Chunk(
                    chunk_id=synth_id,
                    doc_id=meta.doc_id,
                    seq=next_seq,
                    text=summary_text,
                    source_locator=f"{prefix}#{para_part}",
                    para_start=para_lo,
                    para_end=para_hi,
                    title=meta.title,
                    topic=meta.topic,
                    entity=meta.entity,
                    geography=meta.geography,
                    period=meta.period,
                    language=meta.language,
                    sensitivity=meta.sensitivity,
                    is_synthesis=True,
                )
                next_seq += 1
                # 子节点 parent_id 指向本摘要节点（small-to-big / 树遍历的父句柄）。
                for m in members:
                    m.chunk.parent_id = synth_id
                all_chunks.append(synth)
                parents.append(_Node(summary_text, para_lo, para_hi, synth))

            if not parents:
                break
            # 下一层只对【本层新摘要节点】继续聚类（经典 RAPTOR collapsed-build 简化：未归并的单例不再上提）。
            level_nodes = parents
            if len(parents) < self.min_cluster_size:
                break

        return all_chunks
