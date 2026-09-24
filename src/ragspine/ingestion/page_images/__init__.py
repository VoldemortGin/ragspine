"""page_images —— DI markdown 关联原 PDF，入库时渲染页图（图文混合上下文的入库侧）。

未关联 PDF 时什么都不做，纯文本入库行为不变；检索期怎么用这些页图见 ``ragspine.retrieval.page_images``。

Submodules:
    source_pdf.py — 关联解析（显式参数 > sidecar ``<stem>.meta.json`` 的 ``source_pdf`` 字段）、页数校验、sha256。
    render.py — pdfspine 渲染 PNG；默认 144dpi、长边封顶 1568px。
    index.py — sync_page_images：按 doc 幂等同步进映射表，RESTRICTED 页不渲染，trace 只记计数；sync_page_tags 同时写页标签（ADR 0025）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
