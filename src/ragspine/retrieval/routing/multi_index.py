"""多库检索 + 跨库融合（批次 2.2 ②）：并行多库检索后按 RRF 融合，保留库来源维度。

MultiIndexRetriever 满足 A 线 NarrativeRetriever 协议（retrieve(query, *, filters, top_k) -> list[dict]），
故可像单库检索器一样注入 answer_question，也可被 CorrectiveRetriever 等再包裹。

设计取向：
    - 隔离继承：每个库的 base 检索器已在各自出口剔除 RESTRICTED，本层只对各 base 的输出打标签+融合，
      绝不自行读块库、绝不造片段——输出恒为各 base 输出的并集的重排/截断，RESTRICTED 绝不出域。
    - provenance 保留库来源维度：每条融合结果都带 library_id（来源库），原有 doc_id/source_locator 不动。
    - 跨库融合复用既有 RRF 融合栈（lexical.rrf_fuse），确定性平分破除（库序 + 稳定 key）。
    - 路由缝（opt-in）：给了 router 则先按库描述选目标库子集（模式 b），否则扇出全部库（模式 a，默认）。
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ragspine.retrieval.lexical.retrieval import rrf_fuse
from ragspine.retrieval.routing.router import LibraryRouter

# 融合 key 的库/块分隔符（US/单元分隔符，绝不出现在 library_id / chunk_id 里，故 key 唯一、可逆定位）。
_KEY_SEP = "\x1f"

# 结果里承载库来源维度的键。
LIBRARY_ID_KEY = "library_id"


@runtime_checkable
class NarrativeRetriever(Protocol):
    """叙事检索协议（duck-typed，结构等同 agent.NarrativeRetriever）：本层包裹多个它、也实现它。

    本地声明而非 import 编排层的同名协议——只为结构一致，避免 retrieval 反向耦合 agent。
    """

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, Any]]: ...


@dataclass
class LibraryIndex:
    """一个事实库/索引：库标识 + 描述（供路由）+ 该库的 A 线检索器。"""

    library_id: str
    description: str
    retriever: NarrativeRetriever


@dataclass
class MultiIndexRetriever:
    """多库检索器（实现 NarrativeRetriever 协议）：路由选库 -> 并行多库检索 -> 跨库 RRF 融合。

    libraries：各库（library_id 唯一）。router：可选路由缝（None＝扇出全部库，模式 a）。top_k：融合后截断深度。
    """

    libraries: Sequence[LibraryIndex]
    router: LibraryRouter | None = None
    top_k: int = 50
    rrf_k: float = 60.0
    _by_id: dict[str, LibraryIndex] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        ids = [lib.library_id for lib in self.libraries]
        if len(ids) != len(set(ids)):
            raise ValueError(f"library_id 必须唯一，得到：{ids}")
        self._by_id = {lib.library_id: lib for lib in self.libraries}

    def _select(self, query: str) -> list[LibraryIndex]:
        """选目标库：给了 router 且返回非空 -> 该子集（保序）；否则扇出全部库（不饿死召回）。"""
        if self.router is None:
            return list(self.libraries)
        chosen = self.router.route(query, self.libraries)
        selected = [self._by_id[lid] for lid in chosen if lid in self._by_id]
        return selected or list(self.libraries)

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int | None = None
    ) -> list[dict[str, Any]]:
        """并行多库检索 + 跨库 RRF 融合，返回带 library_id 的融合 snippet（top_k）。

        每个库的 base 已在出口剔除 RESTRICTED（隔离继承）。融合 key = library_id + chunk_id（同块跨库
        视作不同来源），确定性平分破除（库序 + 稳定 key 升序）。top_k 缺省（None）回退到构造时的 self.top_k
        （范式同 NarrativeIndex.retrieve）；显式传入则以之为准（A 线编排恒显式传 top_k）。
        """
        limit = self.top_k if top_k is None else top_k
        rankings: list[list[str]] = []
        by_key: dict[str, dict[str, Any]] = {}
        # 库序即优先级，稳定 key 后缀保证跨库同块也确定性可分。
        for order, lib in enumerate(self._select(query)):
            snippets = lib.retriever.retrieve(query, filters=filters, top_k=limit)
            ranking: list[str] = []
            for i, snip in enumerate(snippets):
                chunk_id = str(snip.get("chunk_id") or f"idx{i}")
                key = f"{order:06d}{_KEY_SEP}{lib.library_id}{_KEY_SEP}{chunk_id}"
                tagged = dict(snip)
                tagged[LIBRARY_ID_KEY] = lib.library_id  # 保留库来源维度（provenance）
                by_key[key] = tagged
                ranking.append(key)
            rankings.append(ranking)

        fused = rrf_fuse(rankings, self.rrf_k)
        ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
        return [by_key[key] for key, _score in ordered[:limit]]
