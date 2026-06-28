"""semantic 切块策略（W10，Chunker 缝的 opt-in 实现，默认仍 DefaultChunker）。

在【嵌入相似度边界】切块，而非纯定长：相邻句的嵌入距离突增处起一个新块（话题转换边界）。比定长更
贴合语义，避免把一个论点切两半、或把两个话题塞一块。对标 LlamaIndex SemanticSplitterNodeParser。

嵌入后端（依赖注入，零 SDK）：经构造期注入的 EmbeddingBackend.embed_texts 算句向量。默认回落到
零依赖确定性 DeterministicEmbeddingBackend（blake2b 词法散列，离线、跨进程可复现）——故 make_chunker
('semantic') 不装 [embed-onnx] 也能跑（语义边界基于词法散列相似，近似）；装了 [embed-onnx] 注入真
ONNX 句向量后端即为真语义边界。离线性诚实同 W1。

provenance（不破子串契约 / citation 诚实）：每块 text = 边界内连续句拼接（原文连续子串），para 范围 =
块覆盖段范围，source_locator 同 chunk_document 口径。确定性 = 后端确定（默认 hash 后端确定）=> 切块确定。
"""

import math

from ragspine.retrieval.chunking.chunking import (
    _SENTENCE_RE,
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    Chunk,
    DocumentMeta,
)
from ragspine.retrieval.lexical.retrieval import EmbeddingBackend

# 默认边界阈值：相邻句嵌入【距离】（1 - cosine）超过该分位点对应阈值即开新块。这里用绝对阈值口径：
# 相邻句 cosine 相似 < similarity_threshold 即判话题切换、起新块。
DEFAULT_SIMILARITY_THRESHOLD = 0.5


def _cosine(a: list[float], b: list[float]) -> float:
    """两向量余弦相似度；任一零向量记 0.0（无方向可比）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class SemanticChunker:
    """semantic 切块器（实现 Chunker 协议）：相邻句嵌入相似度跌破阈值处切块。

    embedding_backend 默认 None -> 零依赖确定性 DeterministicEmbeddingBackend（延迟构造）。
    similarity_threshold：相邻句 cosine < 阈值即开新块（越高切得越碎）。max_chars 作为【安全上界】：
    即便语义不切，块超 max_chars 也强制开新块（防超窗）；overlap_chars 形参为协议签名保留（语义块
    不做相邻重叠，保持「块文本 = 原文连续子串」）。
    """

    def __init__(
        self,
        embedding_backend: EmbeddingBackend | None = None,
        *,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ):
        if not -1.0 <= similarity_threshold <= 1.0:
            raise ValueError(
                f"similarity_threshold 须 ∈[-1,1]，得到 {similarity_threshold}"
            )
        self._backend = embedding_backend
        self.similarity_threshold = similarity_threshold

    def _backend_or_default(self) -> EmbeddingBackend:
        if self._backend is None:
            # 延迟构造零依赖确定性默认（不装 [embed-onnx] 也能跑；真语义需注入 ONNX 后端）。
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
        if max_chars <= 0:
            raise ValueError("max_chars 必须为正整数")
        paras: list[str] = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not paras:
            return []

        sentences: list[tuple[str, int]] = []
        for pno, ptext in enumerate(paras, start=1):
            for raw in _SENTENCE_RE.findall(ptext):
                s = raw.strip()
                if s:
                    sentences.append((s, pno))
        if not sentences:
            return []

        backend = self._backend_or_default()
        vectors = backend.embed_texts([s for s, _ in sentences])

        # 贪心聚合：从首句起，相邻句 cosine >= 阈值且不超 max_chars 则并入当前块，否则开新块。
        groups: list[list[tuple[str, int]]] = [[sentences[0]]]
        cur_len = len(sentences[0][0])
        for i in range(1, len(sentences)):
            sim = _cosine(vectors[i - 1], vectors[i])
            s_text = sentences[i][0]
            too_long = cur_len + len(s_text) > max_chars
            if sim < self.similarity_threshold or too_long:
                groups.append([sentences[i]])
                cur_len = len(s_text)
            else:
                groups[-1].append(sentences[i])
                cur_len += len(s_text)

        prefix = meta.source_locator_prefix or meta.doc_id
        chunks: list[Chunk] = []
        for group in groups:
            seq = len(chunks)
            group_text = "".join(s for s, _ in group)
            p_start = min(p for _, p in group)
            p_end = max(p for _, p in group)
            para_part = f"para{p_start}" if p_start == p_end else f"para{p_start}-{p_end}"
            chunks.append(
                Chunk(
                    chunk_id=f"{meta.doc_id}#c{seq}",
                    doc_id=meta.doc_id,
                    seq=seq,
                    text=group_text,
                    source_locator=f"{prefix}#{para_part}",
                    para_start=p_start,
                    para_end=p_end,
                    title=meta.title,
                    topic=meta.topic,
                    entity=meta.entity,
                    geography=meta.geography,
                    period=meta.period,
                    language=meta.language,
                    sensitivity=meta.sensitivity,
                )
            )
        return chunks
