"""retrieval —— 叙事 RAG：切块、词法/向量混合检索、listwise 精排、接入 agent。

RESTRICTED 隔离不变量：敏感度为 RESTRICTED 的内容在 link 与 rerank 两个出口
被剥离，绝不进入 prompt。

Submodules:
    chunking/ — 段落级切块器 + 版本化块库 + 布局感知/父子切块策略（W4b）。
    contextual.py — 确定性 contextual retrieval（W4a）：受控元数据情境头进索引文本（opt-in）。
    corrective.py — opt-in 纠错检索（W6b）：有界确定性 grade→act 环；默认 none 时返回 base 本身、字节不变。
    filtering/ — 元数据过滤：显式过滤条件 + 可注入自动过滤提取器（默认关闭）。
    lexical/ — 混合检索：Okapi BM25（CJK uni+bigram）+ 向量 + RRF 融合。
    link/ — 适配层：把检索接入 agent 编排（NarrativeRetriever 协议）。
    mode.py — 检索模式预设（hybrid / economy），把产品选择映射为确定性参数。
    postprocess.py — opt-in 后检索 postprocessor 链（W8）：MMR 去冗余 + lost-in-the-middle 重排 + 上下文压缩；默认 none 时不挂链、字节不变。
    raptor.py — opt-in RAPTOR 递归聚类+摘要树（W10）：确定性聚类 + is_synthesis 合成摘要 + 多粒度检索；默认关时返回 base 本身、字节不变。
    rerank/ — Claude listwise 精排，RRF 退化兜底。
    routing/ — 多知识库 / 多索引路由：按隔离边界选择检索目标并保序合并。
    vector/ — 可注入的 embedding 后端（默认无 = 纯 BM25）。
    vision/ — opt-in 视觉文档检索（W12）：ColPali/ColQwen2 page-as-image 晚交互（GPU）；默认关、字节不变。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
