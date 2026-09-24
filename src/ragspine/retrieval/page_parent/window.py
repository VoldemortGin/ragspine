"""整页窗口：同一页的块按 seq 拼接、去掉段落回带重叠，过长时按字符预算围绕命中块截断。

回带重叠的口径与 ``chunk_document`` 一致：相邻块的重叠是整段回带（上一块末尾的若干段 = 下一块开头的若干段，
段数 = ``prev.para_end - cur.para_start + 1``）。只有这些行逐字相等时才去掉——同页的下一段（段号从 1 重新
开始）或超长段的句级子块不会被误删。

隔离：RESTRICTED 块在这里被再滤一次（调用方给的兄弟块本就只含非 RESTRICTED），整页窗口绝不含 RESTRICTED 文本；
命中块本身是 RESTRICTED 时返回空串（它会在 link 出口被整块剔除，不需要窗口）。
"""

from collections.abc import Sequence
from typing import Any

from ragspine.retrieval.page_parent.pages import is_restricted

# 整页窗口的字符上限（约 1.3k Qwen3 token）；样本页长中位数 817 字、最长 5370 字。
DEFAULT_PAGE_WINDOW_CHARS = 4000


def _overlap_lines(prev: Any, cur: Any) -> int:
    """cur 开头与 prev 末尾逐字相同的回带段数（不满足整段回带时为 0）。"""
    k = int(prev.para_end) - int(cur.para_start) + 1
    if k <= 0:
        return 0
    prev_lines = prev.text.split("\n")
    cur_lines = cur.text.split("\n")
    if k > len(prev_lines) or k > len(cur_lines):
        return 0
    return k if prev_lines[-k:] == cur_lines[:k] else 0


def page_window(
    siblings: Sequence[Any], hit: Any, *, max_chars: int = DEFAULT_PAGE_WINDOW_CHARS
) -> str:
    """命中块所在页的上下文窗口（整页或围绕命中块截断），命中块全文一定在窗口内。"""
    if is_restricted(hit):
        return ""
    members = sorted(
        {c.chunk_id: c for c in [*siblings, hit] if not is_restricted(c)}.values(),
        key=lambda c: c.seq,
    )
    full = [c.text for c in members]
    pieces = [full[0]] + [
        "\n".join(cur.text.split("\n")[_overlap_lines(prev, cur) :])
        for prev, cur in zip(members, members[1:], strict=False)
    ]

    def render(lo: int, hi: int) -> str:
        # 窗口首块用全文（它的回带段落不在窗口里），其余块用去重叠后的部分。
        return "\n".join([full[lo], *(p for p in pieces[lo + 1 : hi + 1] if p)])

    last = len(members) - 1
    whole = render(0, last)
    if len(whole) <= max_chars:
        return whole
    lo = hi = next(i for i, c in enumerate(members) if c.chunk_id == hit.chunk_id)
    grew = True
    while grew:
        grew = False
        if hi < last and len(render(lo, hi + 1)) <= max_chars:
            hi += 1
            grew = True
        if lo > 0 and len(render(lo - 1, hi)) <= max_chars:
            lo -= 1
            grew = True
    return render(lo, hi)
