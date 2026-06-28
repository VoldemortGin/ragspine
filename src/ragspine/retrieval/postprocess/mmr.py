"""W8 MMR 多样性去重（Maximal Marginal Relevance, Carbonell & Goldstein 1998）：确定性、零模型。

贪心选片段：每步选 `λ·rel − (1−λ)·max_sim_to_picked` 最大者，把近重复块往后压（并可按阈值硬剔）。
relevance 取【输入序】导出的名次分（首位最高，递减）——输入已是 retriever/reranker 的相关性序，故
名次即相关性代理，恒可得、确定（向量/fused-score MMR 留 follow-up，见 PRD W8）。相似度取片段文本
内容词集的 Jaccard（零模型、确定）。平分按原下标升序——稳定、确定。

对标 LlamaIndex MMRPostprocessor / SimilarityPostprocessor、Haystack DiversityRanker。

只返回输入片段的【子集/重排】（绝不造片段），故 RESTRICTED 隔离从 base 出口继承（见 chain.py）。
"""

from dataclasses import dataclass
from typing import Any

from ragspine.retrieval.postprocess.chain import jaccard, snippet_text, token_set


@dataclass
class MMRPostprocessor:
    """确定性 MMR 多样性去重 + 重排（实现 NodePostprocessor 协议）。

    参数：
        lambda_param：相关性 vs 多样性权衡，∈[0,1]，越大越偏相关性（默认 0.5，均衡）。
        top_n：最多保留几条（None=全保留，仅重排/去重）。
        similarity_threshold：硬去重阈值（None=不硬剔，纯 MMR 重排）。某候选对【已选集】的最大
            Jaccard 相似 > 阈值即判近重复、直接丢弃（不进结果），实现「去重」语义。
    """

    lambda_param: float = 0.5
    top_n: int | None = None
    similarity_threshold: float | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.lambda_param <= 1.0:
            raise ValueError(f"lambda_param 须 ∈[0,1]，得到 {self.lambda_param}")
        if self.top_n is not None and self.top_n < 0:
            raise ValueError(f"top_n 须 >= 0 或 None，得到 {self.top_n}")
        if self.similarity_threshold is not None and not (
            0.0 <= self.similarity_threshold <= 1.0
        ):
            raise ValueError(
                f"similarity_threshold 须 ∈[0,1] 或 None，得到 {self.similarity_threshold}"
            )

    def postprocess(
        self, query: str, snippets: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        n = len(snippets)
        if n <= 1:
            return list(snippets)

        # relevance：输入序名次分（首位 1.0，末位 1/n，线性递减）——确定、恒可得。
        relevance = [(n - i) / n for i in range(n)]
        tok_sets = [token_set(snippet_text(s)) for s in snippets]

        limit = n if self.top_n is None else min(self.top_n, n)
        remaining = list(range(n))
        picked: list[int] = []
        picked_sets: list[set[str]] = []

        while remaining and len(picked) < limit:
            best_i: int | None = None
            best_score = float("-inf")
            best_sim = 0.0
            for i in remaining:
                max_sim = (
                    max((jaccard(tok_sets[i], ps) for ps in picked_sets), default=0.0)
                )
                mmr = self.lambda_param * relevance[i] - (1.0 - self.lambda_param) * max_sim
                # 平分按原下标升序（稳定、确定）：仅严格更大才换，等值不换（i 自小到大遍历）。
                if mmr > best_score + 1e-12:
                    best_score = mmr
                    best_i = i
                    best_sim = max_sim
            assert best_i is not None  # remaining 非空 => 必有选中
            remaining.remove(best_i)
            # 硬去重：对已选集近重复则丢弃（不入结果，也不计入 picked_sets）。
            if (
                self.similarity_threshold is not None
                and picked_sets
                and best_sim > self.similarity_threshold
            ):
                continue
            picked.append(best_i)
            picked_sets.append(tok_sets[best_i])

        return [snippets[i] for i in picked]
