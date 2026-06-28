"""postprocess —— W8 后检索 postprocessor 链：精排出口与 prompt 组装之间的确定性补全阶段。

NodePostprocessor 缝（只许返回输入的子集/重排，隔离从 base 出口继承）+ 三个 processor + 链编排 +
检索器包裹 + 选型工厂；默认 None=不接链=字节不变（opt-in，recommended-on）。

Submodules:
    chain.py — NodePostprocessor 协议 + PostprocessorChain + PostprocessingRetriever 包裹 +
        make_postprocessor / make_postprocessing_retriever 工厂（RAGSPINE_POSTPROCESSOR）+ 词面 helpers。
    mmr.py — MMRPostprocessor（确定性多样性去重，Carbonell & Goldstein 1998；零模型）。
    reorder.py — LostInTheMiddleReorder（确定性 lost-in-the-middle 重排，Liu et al. 2023；零模型）。
    compress.py — Compressor 缝 + ExtractiveCompressor（确定性句级过滤）+ CompressionPostprocessor；
        LLMLingua-2 / LLM 抽取作 opt-in follow-up。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
