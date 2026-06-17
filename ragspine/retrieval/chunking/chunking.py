"""叙事通路切块器：文档级纯文本 + 元数据 -> Chunk 列表（story：Narrative Path 检索侧）。

规则：
    - 按段落（按行切分，空行/空白行丢弃）贪心聚合到目标字符预算 max_chars；
    - 相邻块按 overlap_chars 重叠（段落粒度回带，保证 source_locator 始终诚实覆盖块内容）；
    - 超长单段先按句末标点（。！？；.!?;）切句聚合，无标点则按 max_chars 硬切——
      超长段的句级子块不做相邻重叠，保持「块文本 = 原文连续子串」的性质，citation 精确；
    - 每块保留 source_locator（前缀#para起-止，1-based）并完整继承文档元数据。

token 预算取舍（不依赖任何 tokenizer 三方库）：
    用字符数近似 token 数。主流多语 tokenizer 下 CJK 约 1 字 ≈ 1 token、英文约 4 字符
    ≈ 1 token，故「字符数 ≤ 预算」对中文是贴合估计、对英文是保守上界，中英混排不会超窗。
    默认 480 字符落在拍板建议的 400–600 区间：≈480 中文 token，留足主流 embedding 模型
    512-token 窗口的余量，同时不至于碎到丢失段落级语义。重叠默认 80 字符（约 1/6），
    防句子恰被块边界切断导致两边都召回不到。
"""

import re
from dataclasses import dataclass

DEFAULT_CHUNK_CHARS = 480
DEFAULT_OVERLAP_CHARS = 80

# 句末标点（中英），超长段切句用。
_SENTENCE_ENDERS = "。！？；.!?;"

# 一个"句子" = 非句末标点连续串 + 其后的句末标点串（或纯标点串），拼接可还原原文。
_SENTENCE_RE = re.compile(
    rf"[^{_SENTENCE_ENDERS}]+[{_SENTENCE_ENDERS}]*|[{_SENTENCE_ENDERS}]+"
)


@dataclass
class DocumentMeta:
    """文档级元数据（切块时逐块继承）。

    source_locator_prefix 缺省为空串，此时块的 locator 前缀回退为 doc_id。
    """

    doc_id: str
    title: str = ""
    topic: str = ""
    entity: str = ""
    geography: str = ""
    period: str = ""
    language: str = ""
    sensitivity: str = "INTERNAL"
    source_locator_prefix: str = ""


@dataclass
class Chunk:
    """一个检索块：文本 + 段落范围定位 + 继承的文档元数据。

    字段语义约定：
        chunk_id:       块唯一标识（doc_id#c{seq}）。
        seq:            文档内块序（0-based 连续）。
        source_locator: 'prefix#para{起}' 或 'prefix#para{起}-{止}'（1-based，闭区间），
                        服务 citation 回指。
        para_start/end: 块覆盖的段落范围（1-based，含重叠回带的段落）。
    """

    chunk_id: str
    doc_id: str
    seq: int
    text: str
    source_locator: str
    para_start: int
    para_end: int
    title: str = ""
    topic: str = ""
    entity: str = ""
    geography: str = ""
    period: str = ""
    language: str = ""
    sensitivity: str = "INTERNAL"


def chunk_document(
    text: str,
    meta: DocumentMeta,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """把文档级纯文本切成 Chunk 列表。

    - 空 / 纯空白文本 -> []。
    - max_chars <= 0 或 overlap_chars < 0 或 overlap_chars >= max_chars -> ValueError。
    - 块文本为段落以 '\\n' 连接；超长段的句级子块各自独立成块（原文连续子串）。
    """
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正整数")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("须满足 0 <= overlap_chars < max_chars")

    # 段落 = 非空白行（strip 后非空），编号 1-based。
    paras: list[tuple[int, str]] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            paras.append((len(paras) + 1, stripped))
    if not paras:
        return []

    prefix = meta.source_locator_prefix or meta.doc_id
    chunks: list[Chunk] = []

    def emit(chunk_text: str, p_start: int, p_end: int) -> None:
        seq = len(chunks)
        para_part = f"para{p_start}" if p_start == p_end else f"para{p_start}-{p_end}"
        chunks.append(
            Chunk(
                chunk_id=f"{meta.doc_id}#c{seq}",
                doc_id=meta.doc_id,
                seq=seq,
                text=chunk_text,
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

    def joined_len(items: list[tuple[int, str]]) -> int:
        """段落以 '\\n' 连接后的总长。"""
        return sum(len(t) for _, t in items) + max(len(items) - 1, 0)

    # buf 为当前在聚合的块（含上一块回带的重叠段落）；fresh 为其中"新"段落数。
    buf: list[tuple[int, str]] = []
    fresh = 0

    def flush_and_carry() -> list[tuple[int, str]]:
        """把 buf 落成一块，并返回按 overlap_chars 预算回带的尾部段落。"""
        emit("\n".join(t for _, t in buf), buf[0][0], buf[-1][0])
        carry: list[tuple[int, str]] = []
        for item in reversed(buf):
            if joined_len([item, *carry]) <= overlap_chars:
                carry.insert(0, item)
            else:
                break
        return carry

    i = 0
    while i < len(paras):
        pno, ptext = paras[i]
        if len(ptext) > max_chars:
            # 超长单段：先冲洗含新内容的 buf；回带不跨超长段（保持子块=原文连续子串）。
            if fresh:
                flush_and_carry()
            buf, fresh = [], 0
            for piece in _split_oversized(ptext, max_chars):
                emit(piece, pno, pno)
            i += 1
            continue
        if not buf or joined_len([*buf, (pno, ptext)]) <= max_chars:
            buf.append((pno, ptext))
            fresh += 1
            i += 1
            continue
        if fresh == 0:
            # 纯回带 + 新段放不下：从头丢弃回带段落，保证推进（绝不死循环）。
            buf.pop(0)
            continue
        buf, fresh = flush_and_carry(), 0

    if fresh:
        emit("\n".join(t for _, t in buf), buf[0][0], buf[-1][0])
    return chunks


def _split_oversized(ptext: str, max_chars: int) -> list[str]:
    """超长单段切分：先按句末标点切句，超预算的"句"再硬切，最后贪心聚合。

    子块以 '' 连接可精确还原原段（citation 诚实）；子块间不做重叠。
    """
    pieces: list[str] = []
    for sentence in _SENTENCE_RE.findall(ptext):
        if len(sentence) <= max_chars:
            pieces.append(sentence)
        else:
            pieces.extend(
                sentence[j : j + max_chars] for j in range(0, len(sentence), max_chars)
            )

    out: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > max_chars:
            out.append(current)
            current = piece
        else:
            current += piece
    if current:
        out.append(current)
    return out
