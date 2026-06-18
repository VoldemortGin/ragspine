"""retrieval —— 叙事 RAG：切块、词法/向量混合检索、listwise 精排、接入 agent。

RESTRICTED 隔离不变量：敏感度为 RESTRICTED 的内容在 link 与 rerank 两个出口
被剥离，绝不进入 prompt。

Submodules:
    chunking/ — 段落级切块器 + 版本化块库。
    lexical/ — 混合检索：Okapi BM25（CJK uni+bigram）+ 向量 + RRF 融合。
    link/ — 适配层：把检索接入 agent 编排（NarrativeRetriever 协议）。
    rerank/ — Claude listwise 精排，RRF 退化兜底。
    vector/ — 可注入的 embedding 后端（默认无 = 纯 BM25）。
"""
