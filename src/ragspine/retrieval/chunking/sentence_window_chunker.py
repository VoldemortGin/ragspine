"""sentence-window 切块策略（W10，Chunker 缝的 opt-in 实现，默认仍 DefaultChunker）。

对标 LlamaIndex SentenceWindowNodeParser：把【检索粒度】与【生成上下文】解耦——
索引/嵌入单句（检索命中更精准），合成时换回 ±N 句的句子窗口（上下文更完整）。

落地口径（不破既有切块契约）：
    - 每个句子成一块，块 text = 该句（原文连续子串，citation 诚实，与 chunk_document 同的子串契约）；
    - 每块带 window_text = 以本句为中心的 ±window_size 句窗口（合成时展开的富上下文，Chunk 的可选字段，
      默认 ''，其余切块器不填 → 等价安全）；
    - locator / para 用【全局】段号（句子所属段落，1-based；与 chunk_document 同口径）；
    - 参数校验 / 空文本处理直接复用 chunk_document（非法参数 -> ValueError，空/纯空白 -> []）。

诚实边界（follow-up）：window_text 是在切块期物化到内存 Chunk 上的窗口；把它【持久化过 chunk_store】
并在检索命中时于 prompt 里换回窗口，与 W4b 布局切块「命中即展开父块」同属检索期接线的 follow-up
（见 docs/prd-quality-depth.md W10 / retrieval/docs/chunker.md）。超长单句的预算切分亦为 follow-up
（句子天然短；本策略保「块=单句」的语义纯粹）。
"""

from ragspine.retrieval.chunking.chunking import (
    _SENTENCE_ENDERS,
    _SENTENCE_RE,
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    Chunk,
    DocumentMeta,
    chunk_document,
)

# ±window_size 句：默认回带前后各 3 句，兼顾上下文完整与不至过长。
DEFAULT_WINDOW_SIZE = 3


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """非空白行 = 段落，全局 1-based 编号（与 chunk_document 同口径）。"""
    paras: list[tuple[int, str]] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            paras.append((len(paras) + 1, stripped))
    return paras


def _is_content_sentence(sentence: str) -> bool:
    """句子含至少一个非空白、非句末标点的实义字符（滤掉纯标点/纯空白碎片）。"""
    return any(not ch.isspace() and ch not in _SENTENCE_ENDERS for ch in sentence)


def _split_sentences(paras: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """把段落展开成扁平句子表 [(全局段号, 句子文本), ...]（句子=原文连续子串）。"""
    out: list[tuple[int, str]] = []
    for gno, ptext in paras:
        for sentence in _SENTENCE_RE.findall(ptext):
            if _is_content_sentence(sentence):
                out.append((gno, sentence))
    return out


def _window_text(sentences: list[tuple[int, str]], center: int, window_size: int) -> str:
    """以 center 句为中心的 ±window_size 句窗口：同段内以 '' 拼接（还原段内原文），跨段以 '\\n' 分隔。"""
    lo = max(0, center - window_size)
    hi = min(len(sentences), center + window_size + 1)
    parts: list[str] = []
    prev_para: int | None = None
    for gno, sent in sentences[lo:hi]:
        if prev_para is not None and gno != prev_para:
            parts.append("\n")
        parts.append(sent)
        prev_para = gno
    return "".join(parts)


class SentenceWindowChunker:
    """句子窗口切块器（Chunker 缝实现）：索引单句、带 ±N 句窗口供合成时展开。"""

    def __init__(self, window_size: int = DEFAULT_WINDOW_SIZE):
        self.window_size = max(0, window_size)

    def chunk(
        self,
        text: str,
        meta: DocumentMeta,
        *,
        max_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    ) -> list[Chunk]:
        """文档级纯文本 -> 单句 Chunk 列表，每块带 window_text（±window_size 句窗口）。"""
        paras = _paragraphs(text)
        if not paras:
            # 空 / 纯空白：复用 chunk_document 兼做参数校验（非法参数 -> ValueError）并返回 []。
            return chunk_document(text, meta, max_chars=max_chars, overlap_chars=overlap_chars)
        # 参数校验与 chunk_document 一致（非空路径亦须守约）。
        if max_chars <= 0:
            raise ValueError("max_chars 必须为正整数")
        if overlap_chars < 0 or overlap_chars >= max_chars:
            raise ValueError("须满足 0 <= overlap_chars < max_chars")

        sentences = _split_sentences(paras)
        if not sentences:
            return []

        prefix = meta.source_locator_prefix or meta.doc_id
        chunks: list[Chunk] = []
        for seq, (gno, sentence) in enumerate(sentences):
            chunks.append(
                Chunk(
                    chunk_id=f"{meta.doc_id}#c{seq}",
                    doc_id=meta.doc_id,
                    seq=seq,
                    text=sentence,
                    source_locator=f"{prefix}#para{gno}",
                    para_start=gno,
                    para_end=gno,
                    title=meta.title,
                    topic=meta.topic,
                    entity=meta.entity,
                    geography=meta.geography,
                    period=meta.period,
                    language=meta.language,
                    sensitivity=meta.sensitivity,
                    window_text=_window_text(sentences, seq, self.window_size),
                )
            )
        return chunks
