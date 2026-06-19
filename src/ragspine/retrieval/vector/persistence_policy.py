"""持久化策略缝：决定某个块的向量是否可写盘（at-rest）。一条缝、一项决策、零编排。

落地 docs/prd-vector-store-seam.md 的「敏感度门控持久化」增量。范式同其它缝（Protocol +
离线默认 + 工厂选择 + 不变量绑定）；【刻意保持单方法】最低公约面——只回答「这个块的向量
能不能落盘」，不带任何 god-interface 的生命周期钩子 / 配置对象，免得滑向重型框架的过度抽象。

不变量绑定（隔离）：默认 IsolationFirstPolicy 绝不把 sensitivity=RESTRICTED 块的【衍生向量】
写盘——持久化的 embedding 是 RESTRICTED 正文的可还原衍生物，落在血缘旁即新增一个绕过两道
出口（link/rerank）的 at-rest 泄露面。权威剔除仍在两出口；本策略是「持久化层的第三道门」，
默认安全、可换（PersistEverythingPolicy 为 opt-in，仅当整库已按 RESTRICTED-tier 保护时）。
系统级绑定见 NarrativeIndex 测试：默认 policy 下 RESTRICTED 块的向量不进持久库。
"""

import os
from typing import Protocol, runtime_checkable

from ragspine.common.sensitivity import RESTRICTED
from ragspine.retrieval.chunking.chunk_store import StoredChunk
from ragspine.retrieval.chunking.chunking import Chunk

# 工厂读取的环境变量名（缺省 spec 时生效；范式同 VECTOR_STORE_ENV / EMBEDDING_BACKEND_ENV）。
PERSISTENCE_POLICY_ENV = "RAGSPINE_PERSISTENCE_POLICY"


@runtime_checkable
class PersistencePolicy(Protocol):
    """持久化决策的最小结构接口：一个块的向量是否可写盘（at-rest）。单方法，刻意不扩面。"""

    def persistable(self, chunk: StoredChunk | Chunk) -> bool: ...


class IsolationFirstPolicy:
    """隔离优先的安全默认：绝不把 RESTRICTED 块的向量写盘（其余照常落盘）。

    口径同两出口的 RESTRICTED 判定（strip + upper 后比对受控值），漏标 INTERNAL 仍按非
    RESTRICTED 落盘——本策略只防【已标 RESTRICTED】的 at-rest 衍生；漏标治理在 common/sensitivity。
    """

    def persistable(self, chunk: StoredChunk | Chunk) -> bool:
        return str(getattr(chunk, "sensitivity", "")).strip().upper() != RESTRICTED


class PersistEverythingPolicy:
    """opt-in：全部写盘（含 RESTRICTED）。仅当整个 vector db 已按 RESTRICTED-tier at-rest
    保护（加密盘 / 访问控制 / 不随普通备份外流，见 docs/invariants.md）时才该选用。"""

    def persistable(self, chunk: StoredChunk | Chunk) -> bool:
        return True


def make_persistence_policy(spec: str | None = None) -> PersistencePolicy:
    """策略工厂：把「用哪种持久化门控」从改代码降为一个 spec/env，默认隔离优先（最安全）。

    spec 取值（大小写 / 留白不敏感；缺省读环境变量 RAGSPINE_PERSISTENCE_POLICY）：
        - None / 'default' / 'isolation_first'        -> IsolationFirstPolicy（安全默认）
        - 'persist_everything' / 'all'                -> PersistEverythingPolicy（opt-in）
        - 其他                                        -> ValueError
    """
    if spec is None:
        spec = os.environ.get(PERSISTENCE_POLICY_ENV)
    normalized = (spec or "default").strip().lower()
    if normalized in ("default", "isolation_first", "isolation-first"):
        return IsolationFirstPolicy()
    if normalized in ("persist_everything", "persist-everything", "all"):
        return PersistEverythingPolicy()
    raise ValueError(
        f"未知 persistence policy spec：{spec!r}"
        "（可选 default / isolation_first / persist_everything / all）"
    )
