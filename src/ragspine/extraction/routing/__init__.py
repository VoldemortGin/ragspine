"""routing —— PDF 逐页分诊路由：判定每页走数字型还是扫描型抽取路径。

将 scripts/classify_pdfs.py 的分诊逻辑库化为可复用契约。

Submodules:
    pdf_router.py — PDF 分诊路由（二期 PDF 线）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
