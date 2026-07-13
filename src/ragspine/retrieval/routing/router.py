"""库路由缝（批次 2.2 ②，opt-in）：按库描述选择目标库。

范式同 make_reranker / make_filter_extractor：Protocol + 确定性离线默认 + make_* 工厂 + env 选型。

默认离线路由 KeywordLibraryRouter：query 与各库 description 的词面重叠打分，选中有重叠的库（按分降序、
库序破平分）；全零重叠时【回落到全部库】（绝不因路由把召回饿死）。零模型、零网络、确定性。
LLM 路由作 opt-in 适配器经 LibraryRouter 协议注入（follow-up）。

make_library_router(None) 返回 None＝不路由（MultiIndexRetriever 扇出全部库，模式 a，默认）。
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ragspine.retrieval.lexical.retrieval import tokenize

# 选型读取的环境变量名（缺省 spec 时生效）。
LIBRARY_ROUTER_ENV = "RAGSPINE_LIBRARY_ROUTER"


@runtime_checkable
class RoutableLibrary(Protocol):
    """路由所需的库最小结构接口：库标识 + 描述（供词面匹配）。

    结构化声明而非 import multi_index.LibraryIndex——避免 router <-> multi_index 循环依赖，也让 beartype
    运行时无需解析跨模块前向引用。LibraryIndex 天然结构匹配。
    """

    library_id: str
    description: str


@runtime_checkable
class LibraryRouter(Protocol):
    """库路由缝：query + 库清单 -> 选中的 library_id 列表（顺序=优先级）。

    返回空列表表示【不限定】（调用方据此扇出全部库，绝不因路由饿死召回）。核心只 import 协议，零 SDK；
    LLM 路由器作 opt-in 适配器实现之。
    """

    def route(self, query: str, libraries: Sequence[RoutableLibrary]) -> list[str]: ...


@dataclass
class KeywordLibraryRouter:
    """确定性、零网络的关键词库路由（离线默认，opt-in）。

    对每个库，按 query 词元与库 description 词元的重叠计数打分；选中所有分>0 的库，按分降序、库出现序
    破平分。全零重叠 -> 返回空列表（调用方扇出全部库，不饿死召回）。分词复用 lexical.tokenize（中英混排、
    CJK uni+bigram），口径与检索一致。
    """

    def route(self, query: str, libraries: Sequence[RoutableLibrary]) -> list[str]:
        query_tokens = set(tokenize(query))
        scored: list[tuple[int, int, str]] = []  # (-score, 库序, library_id) 便于确定性排序
        for order, lib in enumerate(libraries):
            desc_tokens = set(tokenize(lib.description))
            score = len(query_tokens & desc_tokens)
            if score > 0:
                scored.append((-score, order, lib.library_id))
        scored.sort()
        return [library_id for _, _, library_id in scored]


_ALIASES = {
    "keyword": KeywordLibraryRouter,
    "description": KeywordLibraryRouter,
    "lexical": KeywordLibraryRouter,
}


def make_library_router(spec: str | None = None, **kwargs: object) -> LibraryRouter | None:
    """库路由器工厂：默认 'none' 返回 None（不路由，扇出全部库，字节不变）。

    spec 取值（大小写 / 留白 / 连字符不敏感；缺省读环境变量 RAGSPINE_LIBRARY_ROUTER）：
        - None / 'none'                          -> None（默认，扇出全部库，模式 a）。
        - 'keyword' / 'description' / 'lexical'  -> KeywordLibraryRouter（确定性关键词路由，模式 b）。
        - 其他                                    -> ValueError（列清可用 spec）。

    LLM 路由器作进一步 opt-in 适配器经 LibraryRouter 协议注入（follow-up），本工厂不内置任何联网实现。
    """
    if spec is None:
        spec = os.environ.get(LIBRARY_ROUTER_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_")
    if normalized == "none":
        return None
    factory = _ALIASES.get(normalized)
    if factory is None:
        raise ValueError(
            f"未知 library_router spec {spec!r}；可用：none（默认扇出） / keyword / description / lexical"
        )
    return factory(**kwargs)  # type: ignore[arg-type]
