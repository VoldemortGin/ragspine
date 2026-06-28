"""sentence-window 切块策略（W10，Chunker 缝的 opt-in 实现，默认仍 DefaultChunker）。

精确检索 + 富上下文：以【单句】为检索粒度，但每块文本带上 ±window_size 句的窗口上下文——既能让
检索精确命中含查询词的那一句，又在合成时给足相邻句的语境。对标 LlamaIndex SentenceWindowNodeParser。

provenance（不破子串契约 / citation 诚实）：
    - 每块 text = 中心句 ± window_size 句（原文连续子串，'' 直接拼接还原），para 范围 = 窗口覆盖的
      段范围，source_locator = 'prefix#para{起}-{止}'（与 chunk_document 同口径）；
    - 中心句所在段记在 seq 序里，块按文档顺序连续编号。

口径取舍：本实现把窗口直接写进块文本（落库即可用、与现有 narrative_chunk 表零迁移）。LlamaIndex 那种
「嵌入单句、合成时再 query-time 换成窗口」需要把窗口元数据单独落库 + 检索期换文，属 follow-up
（见 docs/prd-quality-depth.md W10；现状同 W4b parent_id 未落库）。本实现先给「细粒度 + 带窗上下文」
这一确定性、落库即用的版本。

确定性、零三方依赖（仅 stdlib re）。
"""

from ragspine.retrieval.chunking.chunking import (
    _SENTENCE_RE,
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    Chunk,
    DocumentMeta,
)

# 默认窗口半径：中心句两侧各取 N 句拼进块文本。
DEFAULT_WINDOW_SIZE = 2


class SentenceWindowChunker:
    """sentence-window 切块器（实现 Chunker 协议）：单句粒度 + ±window_size 句窗口上下文。

    切句口径复用 chunking._SENTENCE_RE（与超长段句切同口径）：跨整篇正文（段落以换行折叠为空格后
    连续切句），每个非空句成一个「中心句」，块文本 = 中心句 ± window_size 句。max_chars / overlap_chars
    形参为 Chunker 协议签名保留（本策略以句窗为单位，不走字符预算贪心），传入不影响切句。
    """

    def __init__(self, window_size: int = DEFAULT_WINDOW_SIZE):
        if window_size < 0:
            raise ValueError(f"window_size 须 >= 0，得到 {window_size}")
        self.window_size = window_size

    def chunk(
        self,
        text: str,
        meta: DocumentMeta,
        *,
        max_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    ) -> list[Chunk]:
        # 段落 = 非空白行，记 1-based 段号；切句时记录每句所属段号（取该句首字符所在段）。
        paras: list[str] = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not paras:
            return []

        # 逐段切句，记 (句文本, 段号)。空句丢弃。
        sentences: list[tuple[str, int]] = []
        for pno, ptext in enumerate(paras, start=1):
            for raw in _SENTENCE_RE.findall(ptext):
                s = raw.strip()
                if s:
                    sentences.append((s, pno))
        if not sentences:
            return []

        prefix = meta.source_locator_prefix or meta.doc_id
        chunks: list[Chunk] = []
        n = len(sentences)
        for center in range(n):
            lo = max(0, center - self.window_size)
            hi = min(n, center + self.window_size + 1)
            window = sentences[lo:hi]
            window_text = "".join(s for s, _ in window)
            p_start = min(p for _, p in window)
            p_end = max(p for _, p in window)
            seq = len(chunks)
            para_part = f"para{p_start}" if p_start == p_end else f"para{p_start}-{p_end}"
            chunks.append(
                Chunk(
                    chunk_id=f"{meta.doc_id}#c{seq}",
                    doc_id=meta.doc_id,
                    seq=seq,
                    text=window_text,
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
