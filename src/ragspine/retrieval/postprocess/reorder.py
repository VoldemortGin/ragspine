"""W8 lost-in-the-middle 重排（Liu et al. 2023, *Lost in the Middle*）：确定性、零模型。

LLM 对长上下文【中部】注意力最差，而 reranked 序恰把最相关的命中放在中间。本 processor 把相关性
序的片段重排成：最相关落【首尾】、最不相关沉【中部】。

算法（对标 LangChain LongContextReorder / Haystack LostInTheMiddleRanker）：输入按相关性降序；先翻转
成升序，再交替「插入头部 / 追加尾部」——结果第 1 相关在头、第 2 相关在尾、最不相关居中。纯位置变换，
确定、零模型，输入输出同一集合（只重排，不增删）。

只返回输入片段的【重排】（绝不造片段），故 RESTRICTED 隔离从 base 出口继承（见 chain.py）。
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class LostInTheMiddleReorder:
    """确定性 lost-in-the-middle 重排（实现 NodePostprocessor 协议）。

    假设输入按相关性降序（retriever/reranker 的出口序）。输出把最相关的置于首尾、最不相关置于中部。
    无参数：纯位置变换，输入即输出集合的一个排列。
    """

    def postprocess(
        self, query: str, snippets: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if len(snippets) <= 2:
            return list(snippets)
        # 翻转成相关性升序，再交替 插头/追尾：偶数下标插到头部、奇数下标追加尾部。
        ascending = list(reversed(snippets))
        reordered: list[dict[str, Any]] = []
        for i, snippet in enumerate(ascending):
            if i % 2 == 1:
                reordered.append(snippet)
            else:
                reordered.insert(0, snippet)
        return reordered
