"""adapters —— 第三方 GraphStore 适配器（networkx 等），延迟 import，behind [graph] extra。

每个适配器都跑同一套 tests/conformance（参数化 graph_store 夹具 + corespine 机制层套件），把
provenance / isolation / determinism 绑死在缝上——不通过 conformance 的适配器直接 CI 红，而非
生产事故。零依赖默认仍是 ragspine.graph.store.InProcessGraphStore。

Submodules:
    networkx_store.py — networkx（MultiDiGraph）适配器：薄包一层图库，逐字段复用 store.py 的 RESTRICTED 隔离 / where 过滤 / 升序确定性，故同一套 conformance 全过（BSD-3，过 ADR 0009 ≤Apache-2.0 许可门）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
