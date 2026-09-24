"""page_parent —— 页级父子 + 按页去重（opt-in，``RAGSPINE_PAGE_PARENT=off|dedup|page+child``，默认 off）。

检索粒度仍是小块（child），交给 LLM 的上下文是整页（parent）：同一页被多个小块命中时只返回一次，
代表块 = 该页排名最好的块，其 ``window_text`` 换成整页文本（复用 ADR 0018 的 small-to-big 展开路径，
经 ``link/_to_snippet`` 写成 ``prompt_text``）。编排在 ``NarrativeIndex.retrieve``（融合之后、精排之前）。

Submodules:
    pages.py  — 开关解析 + 页标识（从 locator 解析 (doc_id, page)）+ 按页分组 / RESTRICTED 判定。
    window.py — 整页窗口：同页非 RESTRICTED 块按 seq 拼接、去掉段落回带重叠、按字符预算截断（命中块必在窗口内）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
