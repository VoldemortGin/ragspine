"""领域切块预设（Item ⑩ + 批次 2.2）：若干【薄】LayoutAwareChunker 子类。

设计取向：不新增引擎、零三方依赖、确定性。每个预设只调优一件事，其余（小节内复用 chunk_document
预算贪心、parent_id/heading 标注、locator/全局段号、参数校验、空文本处理）全部从 LayoutAwareChunker
继承。经 make_chunker 选用，默认切块路径不受影响。

预设：
    - LawsChunker（laws/law/legal）：条款层级——每个「第N条」自成小节，条级子块（覆写 _is_heading）。
    - BookChunker（book/chapter）：章节层级——「第N章/节/篇/部/回/讲」起小节，正文散文不误判（覆写 _is_heading）。
    - QaChunker（qa/faq）：问答对成对——每个问句起小节，其后答案段落随之同块共享 parent_id（覆写 _is_heading）。
    - ParentChildChunker（parent_child/parent-child/small_to_big）：父子（small-to-big）分段——child 精准命中
      后确定性展开到 parent 小节富上下文（覆写 _child_extra 填 window_text=父小节全文 +
      parent_locator=父小节真实段落跨度）。
"""

import re

from ragspine.retrieval.chunking.layout_chunker import (
    _CHAPTER_RE,
    _MD_HEADING_RE,
    _NUM_HEADING_RE,
    LayoutAwareChunker,
)

# 法律条款正则：'第N条' / '第N款' / '第N项'（基座 _CHAPTER_RE 只认 章/节/篇/部/回/讲，缺条/款/项，
# 这正是 laws 预设的立足点）。单条 alternation。
_CLAUSE_RE = re.compile(r"^第[0-9一二三四五六七八九十百千]+[条款项]")

# 问句前缀（大小写不敏感由 IGNORECASE 处理 Q）：Q: / Q. / Q、 / Q) / Q） / 问： / 问: / 问、。
_QUESTION_PREFIX_RE = re.compile(r"^(?:Q[:.、)）]|问[：:、])", re.IGNORECASE)

# 问句尾（中英问号）。
_QUESTION_SUFFIX = ("?", "？")


class LawsChunker(LayoutAwareChunker):
    """条款层级切块（法律法规）：每个「第N条」起独立小节，条级子块。

    _is_heading 命中任一即小节边界：markdown 标题 / 章节标记（_CHAPTER_RE）/ 编号标题
    （_NUM_HEADING_RE）/ 法律条款（_CLAUSE_RE：第N条/款/项）。故每个第N条自成小节——条款行为
    heading、独立 parent_id，实现条级切块。

    刻意【不】启用基座的「短无标点行」通用启发式：法条内常有短的实质性句子（并非标题），通用
    启发式会把它们误切成独立小节。此预设只认结构性信号（markdown / 章 / 条 / 编号）。
    """

    def _is_heading(self, line: str) -> bool:
        s = line.strip()
        if not s:
            return False
        return bool(
            _MD_HEADING_RE.match(s)
            or _CHAPTER_RE.match(s)
            or _CLAUSE_RE.match(s)
            or _NUM_HEADING_RE.match(s)
        )


class BookChunker(LayoutAwareChunker):
    """章节层级切块（书籍）：markdown / 「第N章/节/篇/部/回/讲」/ 编号标题起小节，正文随其后。

    _is_heading = markdown OR _CHAPTER_RE OR _NUM_HEADING_RE（只认结构性信号）。刻意【不】启用
    基座的「短无标点行」通用启发式：散文 / 对话里的短行（≤30 字、无标点）绝不应被当作章节标题。
    """

    def _is_heading(self, line: str) -> bool:
        s = line.strip()
        if not s:
            return False
        return bool(_MD_HEADING_RE.match(s) or _CHAPTER_RE.match(s) or _NUM_HEADING_RE.match(s))


class QaChunker(LayoutAwareChunker):
    """问答对成对切块（QA/FAQ）：每个问句起小节，其后答案段落随之同块，问答成对。

    _is_heading = 问句检测：整行 strip 后以问句前缀开头（Q: / Q. / Q、 / Q) / Q） / 问： / 问: /
    问、），或以问号结尾（? / ？）。因 _sections 在每个「标题」处起新小节、并把其后的非标题段落收进
    该节，故每个问句 + 其后答案段落落在【同一小节】共享一个 parent_id（问句为 heading）——问答对
    因此保持成对。

    长答案取舍：若某答案超过 max_chars，小节内仍交给 chunk_document 按预算切成多块，但这些块
    【共享同一 parent_id】（small-to-big 归组即可拿回整对），问答对不会因预算切分而拆散。
    """

    def _is_heading(self, line: str) -> bool:
        s = line.strip()
        if not s:
            return False
        return bool(_QUESTION_PREFIX_RE.match(s)) or s.endswith(_QUESTION_SUFFIX)


class ParentChildChunker(LayoutAwareChunker):
    """父子（small-to-big）分段预设：child 精准命中 → 展开到 parent 小节富上下文。

    与基类 LayoutAwareChunker 的唯一差别：覆写 _child_extra，为每个 child 块补两个字段——
        - window_text   = 父小节全文（合成时展开的富上下文，检索粒度=细 child、生成上下文=整节，二者解耦）；
        - parent_locator = 父小节【真实】段落跨度 locator（'{prefix}#para{起}-{止}'，覆盖整节）——
          child 展开到 parent 时的 citation 回指，指向真实 parent 段落跨度，绝不臆造。

    child 的 text / source_locator / para 范围仍为其自身（原文连续子串，citation 诚实）；小节边界、
    parent_id/heading 标注、chunk_document 预算贪心全部继承。检索命中细 child 后，group_children_by_parent
    可按 parent_id 归组、window_text/parent_locator 提供整节上下文与真实 parent 溯源。

    child 粒度由 make_chunker 传入的 max_chars 控制（与家族其余切块器同口径）；小节可大于预算时天然切成
    多个共享同一 parent_id 的 child，每个 child 都带整节 window_text。
    """

    def _child_extra(self, section_text: str, parent_locator: str) -> dict[str, str]:
        return {"window_text": section_text, "parent_locator": parent_locator}
