"""布局感知 + 父子切块策略（W4b，Chunker 缝的 opt-in 实现，默认仍 DefaultChunker）。

家族独有杠杆：在【结构边界】（标题/章节）切，而非纯定长贪心——同一标题下的段落聚在一起、
**绝不跨标题边界合并**；每个子块带 parent_id（= 所属小节，small-to-big 检索的父句柄）与
heading（小节标题，亦可喂 contextual 情境头）。

布局来源：Chunker Protocol 只拿到文档级纯文本 + 元数据，故本策略用【标题启发式】
（markdown # / 编号标题 / 章节关键字 / 短无标点行）在文本内识别小节边界——比纯定长更贴合
结构。家族 extractor（pdfspine / docspine）能给更富的结构（标题层级、表边界等），把那种富结构
喂进切块是后续 follow-up（见 docs/prd-quality-depth.md W4b）。

provenance（不破子串契约 / citation 诚实）：
    - 每个子块 text 仍是原文段落以 '\\n' 连接（「块文本 = 原文连续子串」不破）；
    - locator / para 范围用【全局】段号（与 chunk_document 同口径，非小节内重置）；
    - 小节内的「预算贪心聚合 + 重叠回带 + 超长段句切/硬切 + 参数校验」全部【直接复用】
      chunk_document（零重复、行为一致），本策略只在其上叠加小节边界与父子标注。

small-to-big：检索命中小子块后，group_children_by_parent 把同 parent_id 的兄弟归组，供合成时
展开到父小节。检索期的「命中即展开父块」接线是 follow-up（见 W4b）。
"""

import re
from collections.abc import Sequence

from ragspine.retrieval.chunking.chunking import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    Chunk,
    DocumentMeta,
    chunk_document,
)

# 标题启发式（确定性、零三方依赖）：
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+\S")                       # markdown ATX：'# 标题'
_CHAPTER_RE = re.compile(r"^第[0-9一二三四五六七八九十百千]+[章节篇部回讲]")  # '第三章 ...'
_NUM_HEADING_RE = re.compile(                                       # '1. ' / '1.2 ' / '一、' / 'IV) '
    r"^(\d+(\.\d+)*|[一二三四五六七八九十百千]+|[IVXLCDMivxlcdm]+)[.、)）]\s*\S"
)

# 短无标点行（标题型）的判定阈值：不超过该长度、且整行不含任何句末/子句标点。
_HEADING_MAX_CHARS = 30
_TERMINAL_PUNCT = "。！？；…，、：.!?;,:"


def is_heading(line: str) -> bool:
    """判定一个段落行是否为标题（确定性启发式）。

    命中任一即标题：markdown ATX（'# 标题'）/ 中文章节（'第N章 …'）/ 编号标题（'1. ' / '一、'）/
    或「短行且整行无标点」。长句、含逗号/句号的行、空行一律不是标题。
    """
    s = line.strip()
    if not s:
        return False
    if _MD_HEADING_RE.match(s) or _CHAPTER_RE.match(s) or _NUM_HEADING_RE.match(s):
        return True
    return len(s) <= _HEADING_MAX_CHARS and not any(ch in _TERMINAL_PUNCT for ch in s)


def group_children_by_parent(chunks: Sequence[Chunk]) -> dict[str, list[Chunk]]:
    """把子块按 parent_id 归组（small-to-big：命中小块后展开到父小节的兄弟全集）。

    无 parent_id 的块（如 DefaultChunker 产出）以自身 chunk_id 自成一组，保持函数全集可用。
    分组内按出现顺序（即 seq 顺序）保留。
    """
    groups: dict[str, list[Chunk]] = {}
    for c in chunks:
        key = getattr(c, "parent_id", "") or c.chunk_id
        groups.setdefault(key, []).append(c)
    return groups


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """非空白行 = 段落，全局 1-based 编号（与 chunk_document 同口径）。"""
    paras: list[tuple[int, str]] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            paras.append((len(paras) + 1, stripped))
    return paras


def _sections(
    paras: list[tuple[int, str]],
) -> list[tuple[str, list[tuple[int, str]]]]:
    """把全局段落切成小节：每个标题段起一节（标题为该节首段，随其内容同块）；
    首个标题前的前导段自成一节（heading=''）。返回 [(heading, [(全局段号, 段文本), ...]), ...]。
    """
    sections: list[tuple[str, list[tuple[int, str]]]] = []
    heading = ""
    current: list[tuple[int, str]] = []
    for gno, ptext in paras:
        if is_heading(ptext):
            if current:
                sections.append((heading, current))
            heading = ptext
            current = [(gno, ptext)]
        else:
            current.append((gno, ptext))
    if current:
        sections.append((heading, current))
    return sections


class LayoutAwareChunker:
    """布局感知 + 父子切块器（Chunker 缝实现）：标题边界切 + 小节内复用 chunk_document 预算贪心。"""

    def chunk(
        self,
        text: str,
        meta: DocumentMeta,
        *,
        max_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    ) -> list[Chunk]:
        """文档级纯文本 -> Chunk 列表，块永不跨标题边界；带 parent_id / heading。

        参数校验、空/纯空白 -> []、预算/重叠/超长段处理与 chunk_document 完全一致（复用之）。
        """
        paras = _paragraphs(text)
        if not paras:
            # 空 / 纯空白：复用 chunk_document 兼做参数校验（非法参数 -> ValueError）并返回 []。
            return chunk_document(
                text, meta, max_chars=max_chars, overlap_chars=overlap_chars
            )

        prefix = meta.source_locator_prefix or meta.doc_id
        out: list[Chunk] = []
        for s_idx, (heading, sec_paras) in enumerate(_sections(paras)):
            section_text = "\n".join(t for _, t in sec_paras)
            # 小节内的预算贪心 / 重叠 / 超长段切分全交给 chunk_document（行为一致、零重复）。
            local_chunks = chunk_document(
                section_text, meta, max_chars=max_chars, overlap_chars=overlap_chars
            )
            globals_ = [gno for gno, _ in sec_paras]  # 小节内局部段号 -> 全局段号映射表
            parent_id = f"{meta.doc_id}#s{s_idx}"
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
                        parent_id=parent_id,
                        heading=heading,
                    )
                )
        return out
