"""retrieval —— 叙事 RAG：切块、词法/向量混合检索、listwise 精排、接入 agent。

RESTRICTED 隔离不变量：敏感度为 RESTRICTED 的内容在 link 与 rerank 两个出口
被剥离，绝不进入 prompt。

Submodules:
    chunking/ — 段落级切块器 + 版本化块库 + 布局感知/父子切块策略（W4b）。
    contextual.py — 确定性 contextual retrieval（W4a）：受控元数据情境头进索引文本（opt-in）。
    corrective.py — opt-in 纠错检索（W6b）：有界确定性 grade→act 环；默认 none 时返回 base 本身、字节不变。
    lexical/ — 混合检索：Okapi BM25（CJK uni+bigram）+ 向量 + RRF 融合。
    link/ — 适配层：把检索接入 agent 编排（NarrativeRetriever 协议）。
    rerank/ — Claude listwise 精排，RRF 退化兜底。
    vector/ — 可注入的 embedding 后端（默认无 = 纯 BM25）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
