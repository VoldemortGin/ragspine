"""检索模式预设（批次 2.2 ④）：把「用不用 embedding 向量通道」抬成一个显式、可配置的检索预设。

economy（零 embedding 成本）预设把既有纯 BM25 关键词检索（HybridRetriever 在 embedding_backend=None
时的原生模式）包装成一个【显式命名】的检索模式，与向量 / 混合模式在【同一配置面】
（ServiceConfig.retrieval_mode）切换。默认 'auto' = 混合模式（embedding 仍按 ServiceConfig.embedding
装配，字节不变）。

设计取向（范式同 make_reranker / make_vector_store / make_chunker 等缝的 make_* 工厂）：
    - 零三方依赖、确定性、离线优先；economy 模式绝不构造 / 调用任何 embedding 后端（零 embedding 成本）。
    - 反编造 / provenance / RESTRICTED 双出口三项不变量对两种模式一致成立——economy 就是既有 BM25 默认
      通路本身（NarrativeIndexRetriever 出口照常剔除 RESTRICTED、每 snippet 带 doc_id/locator），仅关掉
      向量通道，不新增任何绕过点。
"""

import os
from dataclasses import dataclass

# 选型读取的环境变量名（缺省 spec 时生效；范式同 store.VECTOR_STORE_ENV / chunker.CHUNKER_ENV）。
RETRIEVAL_MODE_ENV = "RAGSPINE_RETRIEVAL_MODE"


@dataclass(frozen=True)
class RetrievalMode:
    """检索模式预设：name + 是否启用向量（embedding）通道。

    uses_embedding=False 即 economy（零 embedding 成本，纯 BM25+RRF）；True 即混合 / 向量模式
    （embedding 按上层配置装配）。装配层据 uses_embedding 决定是否构造 embedding 后端 / 向量库。
    """

    name: str
    uses_embedding: bool


# 两个内置预设：economy（零 embedding）与 hybrid（默认，向量通道按配置装配）。
ECONOMY = RetrievalMode("economy", uses_embedding=False)
HYBRID = RetrievalMode("hybrid", uses_embedding=True)

# 别名（大小写 / 连字符不敏感）。economy 家族：显式关向量；hybrid 家族含 auto/none（默认，字节不变）。
_ECONOMY_ALIASES = frozenset({"economy", "bm25", "lexical", "keyword"})
_HYBRID_ALIASES = frozenset({"none", "auto", "hybrid", "vector", "dense"})


def make_retrieval_mode(spec: str | None = None) -> RetrievalMode:
    """检索模式工厂：把「economy vs 向量」从改代码降为一个 spec/env。

    spec 取值（大小写 / 留白 / 连字符不敏感；缺省读环境变量 RAGSPINE_RETRIEVAL_MODE）：
        - None / 'auto' / 'none' / 'hybrid' / 'vector' / 'dense' -> HYBRID（默认，embedding 按
          ServiceConfig.embedding 装配，字节不变）。
        - 'economy' / 'bm25' / 'lexical' / 'keyword'             -> ECONOMY（零 embedding 成本，纯 BM25）。
        - 其他                                                    -> ValueError（列清可用 spec）。
    """
    if spec is None:
        spec = os.environ.get(RETRIEVAL_MODE_ENV)
    normalized = (spec or "auto").strip().lower().replace("-", "_")
    if normalized in _ECONOMY_ALIASES:
        return ECONOMY
    if normalized in _HYBRID_ALIASES:
        return HYBRID
    raise ValueError(
        f"未知 retrieval_mode spec {spec!r}；可用："
        "economy/bm25/lexical/keyword（零 embedding 成本，纯 BM25） "
        "或 auto/hybrid/vector/dense（默认，embedding 按配置装配）"
    )
