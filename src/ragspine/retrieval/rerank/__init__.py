"""rerank —— Claude listwise 二审精排：协议 + prompt 构造 + 鲁棒解析 + 退化策略。

解析失败时退化回 RRF 排序；RESTRICTED 内容在此出口被剥离（与 link 共两道防线）。

Submodules:
    listwise_rerank.py — Claude listwise 二审（精排）：协议 + prompt + 解析 + 退化。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
