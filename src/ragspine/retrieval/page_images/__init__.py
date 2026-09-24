"""page_images —— 图文混合上下文的检索侧（opt-in，``RAGSPINE_PAGE_IMAGES=off|tagged|all``，``on`` 是 ``all`` 的别名，默认 off）。

入库时（``ragspine.ingestion.page_images``）把关联 PDF 的每页渲染成 PNG、记进映射表，并逐页写页标签；检索时在按页
去重之后给前 N 页附页图引用（``tagged`` 再按页标签只留一部分，ADR 0025），由 agent 与整页文本一起组成图文混合上下文。

Submodules:
    store.py — 映射表 (doc_id, page) → 内容寻址 PNG 路径 / PDF sha256 / dpi；幂等替换、孤儿文件清理。
    attach.py — PageImageRetriever：前 N 条附 ``page_image`` 引用；含 RESTRICTED 块的页不发图；trace 只记计数。
    trigger/ — 触发策略（ADR 0025）：page_tag 表 + 旧库懒算；PageImageTriggerRetriever 在外层按标签 / 去重 / 上限只删不增。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
