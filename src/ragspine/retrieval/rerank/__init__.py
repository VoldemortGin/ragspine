"""rerank —— ⭐ 精排出口：listwise 编排协议 + LLM/本地两种重排大脑 + RESTRICTED 不出域。

解析失败 / judge 缺省时退化回 RRF 排序；RESTRICTED 内容在此出口被剥离（与 link 共两道防线）。

Submodules:
    listwise_rerank.py — listwise 二审编排：ListwiseJudge 协议 + prompt + 鲁棒解析 + 退化 + 隔离。
    cross_encoder.py — 本地 cross-encoder 重排（W2）：fastembed TextCrossEncoder 实现 ListwiseJudge + make_reranker 工厂（离线、确定性、纯 CPU、归 [rerank]；默认仍不重排，opt-in）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
