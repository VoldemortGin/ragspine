"""semantic 切块策略（W10，Chunker 缝的 opt-in 实现，默认仍 DefaultChunker）。

对标 LlamaIndex SemanticSplitterNodeParser：按【相邻段 embedding 距离峰值】切分（拓扑连贯），
而非纯定长——话题变化处（相邻段向量距离突增）起新块，同一话题的连续段聚在一起。

落地口径（不破既有切块契约、provenance / 子串契约不丢）：
    - 粒度=段落（系统 provenance 以段落 locator 为最小单位）：嵌入每个段落，算相邻段距离
      distance = 1 - cosine，距离 ≥ 阈值（距离谱的 breakpoint_percentile 分位）处切一刀；
    - 段落分组后，段内的预算贪心 / 重叠 / 超长段切分 / 参数校验【全部复用】chunk_document（零重复、
      行为一致），并把段内局部段号【重映射回全局】段号——故块 text 仍是原文子串、locator 全局诚实
      （与 W4b 布局切块同的复用手法，只是边界来自 embedding 距离而非标题）；
    - embedding 默认走【零依赖确定性词法散列后端】（DeterministicEmbeddingBackend），故离线可跑、
      确定性可复现；真语义 ONNX 后端（[embed-onnx]）经构造注入 opt-in（检索质量更高）。

诚实边界（follow-up）：句级（sub-paragraph）语义切分需子段 locator（本系统 locator 到段落为止），
故本策略在【段落粒度】上做语义边界；句级细分留作 follow-up（见 docs/prd-quality-depth.md W10）。
"""

import math
from typing import Protocol, runtime_checkable

from ragspine.retrieval.chunking.chunking import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    Chunk,
    DocumentMeta,
    chunk_document,
)

# 距离谱分位阈值：默认 90 分位——只在距离最突出的相邻边界（话题切换）切，稳健不过碎。
DEFAULT_BREAKPOINT_PERCENTILE = 90.0


@runtime_checkable
class _Embedder(Protocol):
    """embedding 后端最小结构接口（duck-typed，同 lexical.retrieval.EmbeddingBackend）。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """非空白行 = 段落，全局 1-based 编号（与 chunk_document 同口径）。"""
    paras: list[tuple[int, str]] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            paras.append((len(paras) + 1, stripped))
    return paras


def _cosine(a: list[float], b: list[float]) -> float:
    """两向量余弦（含零范数守护：任一为零向量 -> 0.0，不抛）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _percentile(sorted_vals: list[float], p: float) -> float:
    """升序序列的 p 分位（线性插值，numpy-free、确定性）。空 -> 0.0。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (p / 100.0) * (len(sorted_vals) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_vals[int(lo)]
    frac = rank - lo
    return sorted_vals[int(lo)] + (sorted_vals[int(hi)] - sorted_vals[int(lo)]) * frac


def _semantic_segments(
    paras: list[tuple[int, str]],
    vectors: list[list[float]],
    breakpoint_percentile: float,
) -> list[list[tuple[int, str]]]:
    """把段落按相邻段距离峰值切成语义段组：距离 ≥ 阈值且 > 0 处起新段（相同段落距离 0 不切）。"""
    if len(paras) <= 1:
        return [list(paras)]
    distances = [1.0 - _cosine(vectors[i], vectors[i + 1]) for i in range(len(paras) - 1)]
    threshold = _percentile(sorted(distances), breakpoint_percentile)
    segments: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = [paras[0]]
    for i in range(1, len(paras)):
        d = distances[i - 1]
        if d >= threshold and d > 0.0:
            segments.append(current)
            current = [paras[i]]
        else:
            current.append(paras[i])
    segments.append(current)
    return segments


class SemanticChunker:
    """语义切块器（Chunker 缝实现）：按相邻段 embedding 距离峰值切 + 段内复用 chunk_document 预算贪心。"""

    def __init__(
        self,
        embedder: _Embedder | None = None,
        *,
        breakpoint_percentile: float = DEFAULT_BREAKPOINT_PERCENTILE,
    ):
        if embedder is None:
            # 默认零依赖确定性词法散列后端（离线可跑、确定性）；真语义 ONNX 后端经注入 opt-in。
            from ragspine.retrieval.vector.embedding_backends import (
                DeterministicEmbeddingBackend,
            )

            embedder = DeterministicEmbeddingBackend()
        self.embedder = embedder
        self.breakpoint_percentile = breakpoint_percentile

    def chunk(
        self,
        text: str,
        meta: DocumentMeta,
        *,
        max_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    ) -> list[Chunk]:
        """文档级纯文本 -> Chunk 列表，块边界落在相邻段 embedding 距离峰值处。"""
        paras = _paragraphs(text)
        if not paras:
            # 空 / 纯空白：复用 chunk_document 兼做参数校验（非法参数 -> ValueError）并返回 []。
            return chunk_document(text, meta, max_chars=max_chars, overlap_chars=overlap_chars)
        if max_chars <= 0:
            raise ValueError("max_chars 必须为正整数")
        if overlap_chars < 0 or overlap_chars >= max_chars:
            raise ValueError("须满足 0 <= overlap_chars < max_chars")

        vectors = self.embedder.embed_texts([ptext for _, ptext in paras])
        segments = _semantic_segments(paras, vectors, self.breakpoint_percentile)

        prefix = meta.source_locator_prefix or meta.doc_id
        out: list[Chunk] = []
        for segment in segments:
            section_text = "\n".join(t for _, t in segment)
            # 段内预算贪心 / 重叠 / 超长段切分全交给 chunk_document（行为一致、零重复）。
            local_chunks = chunk_document(
                section_text, meta, max_chars=max_chars, overlap_chars=overlap_chars
            )
            globals_ = [gno for gno, _ in segment]  # 段内局部段号 -> 全局段号映射表
            for lc in local_chunks:
                seq = len(out)
                g_start = globals_[lc.para_start - 1]
                g_end = globals_[lc.para_end - 1]
                para_part = (
                    f"para{g_start}" if g_start == g_end else f"para{g_start}-{g_end}"
                )
                out.append(
                    Chunk(
                        chunk_id=f"{meta.doc_id}#c{seq}",
                        doc_id=meta.doc_id,
                        seq=seq,
                        text=lc.text,
                        source_locator=f"{prefix}#{para_part}",
                        para_start=g_start,
                        para_end=g_end,
                        title=meta.title,
                        topic=meta.topic,
                        entity=meta.entity,
                        geography=meta.geography,
                        period=meta.period,
                        language=meta.language,
                        sensitivity=meta.sensitivity,
                    )
                )
        return out
