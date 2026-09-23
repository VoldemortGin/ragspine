"""打分式重排器 → ListwiseJudge 的薄适配（零 SDK、零 import 依赖）。

``/v1/rerank``（Cohere 形状 ``results[].{index, relevance_score}``）的 HTTP 适配器（如
``ragspine.common.evidence.providers.local_models`` 的 ``LocalRerankAdapter``，对接 vLLM 上的
Qwen3-Reranker）返回的是每个候选的相关性分；精排出口的 ``ListwiseJudge`` 协议要的是候选下标排列。
本模块把前者鸭子类型地接成后者：按分降序、平分保原（RRF）序，确定性。

隔离：本类只是一个 judge，照常经 ``listwise_rerank`` 编排调用——RESTRICTED 候选在那里就被排除在
judge 之外，绝不进入 HTTP 打分请求。空白候选（适配器拒收）不送打分，按原序补在末尾。
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class ScoredIndex(Protocol):
    """一条重排结果：候选下标 + 相关性分。"""

    @property
    def index(self) -> int: ...

    @property
    def relevance_score(self) -> float: ...


@runtime_checkable
class ScoringReranker(Protocol):
    """打分式重排器（如 LocalRerankAdapter.rerank）。"""

    def rerank(
        self, query: str, documents: tuple[str, ...], *, limit: int
    ) -> Sequence[ScoredIndex]: ...


class ScoredRerankJudge:
    """实现 ListwiseJudge 协议：judge(query, candidates) -> 按相关性降序的下标。"""

    def __init__(self, reranker: ScoringReranker) -> None:
        self._reranker = reranker

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        scorable = [i for i, text in enumerate(candidates) if text.strip()]
        if not scorable:
            return list(range(len(candidates)))
        documents = tuple(candidates[i] for i in scorable)
        results = self._reranker.rerank(query, documents, limit=len(documents))
        ranked = sorted(results, key=lambda r: (-r.relevance_score, r.index))
        order = [scorable[r.index] for r in ranked]
        seen = set(order)
        order.extend(i for i in range(len(candidates)) if i not in seen)
        return order
