"""representation —— W11 检索表示升级：单向量 dense 之上的晚交互 + 学习稀疏（opt-in，默认不变）。

两条 opt-in 重排后端，均作为 ListwiseJudge 接入 W2 精排链（listwise_rerank），故 RESTRICTED 隔离
继承自编排出口（不进打分）。多向量/稀疏 store（as-retriever 全库检索）是 follow-up。

Submodules:
    late_interaction.py — ColBERT 晚交互（多向量 MaxSim）：MultiVectorBackend 缝 + max_sim +
        ColBERTReranker（ListwiseJudge）+ fastembed 适配器（[colbert]）。
    learned_sparse.py — SPLADE 学习稀疏：SparseEmbeddingBackend 缝 + sparse_dot +
        SpladeReranker（ListwiseJudge）+ fastembed 适配器（[splade]）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
