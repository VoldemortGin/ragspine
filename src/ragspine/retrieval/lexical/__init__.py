"""lexical —— 叙事通路混合检索：BM25（纯 Python）+ 向量 + RRF 融合 + 元数据预过滤 + multi-query。

向量后端通过协议注入，默认缺省即退化为纯 BM25。

Submodules:
    retrieval.py — 叙事通路混合检索：BM25 + 向量（注入）+ RRF + 预过滤 + multi-query。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
