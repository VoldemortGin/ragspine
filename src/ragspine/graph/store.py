"""GraphStore 缝（W7c）：可插拔图存储 + 确定性多跳遍历（live 契约见 src/ragspine/graph/CLAUDE.md）。

这条缝只担一件事：**存类型化的节点/边（带血缘）+ 回答确定性的多跳遍历查询**（neighbors /
subgraph / traverse）。它【不】管「如何从受控维度建图」（那在 relation.py）也【不】管「图上的
业务查询语义」（那在 query.py）。任何实现（内存默认实现现在，将来的 networkx / kuzu / neo4j
adapter）都跑同一套 tests/conformance（tests/conformance/test_graph_store.py），把 provenance /
isolation / determinism 三项不变量绑死在缝上——同 VectorStore 缝的范式（五段式：Protocol·离线
默认·薄 adapter·registry·conformance）。

InProcessGraphStore：零三方依赖、确定性的默认实现（零依赖邻接 dict），让 `pip install ragspine`
的精简默认仍可端到端跑 GraphRAG 的确定性半边（ADR 0005/0009）。

三项不变量如何被【实现真正保证】（非注释）：
- Provenance —— GraphNode / GraphEdge 的 metadata（含 source_doc_id / source_locator）原样存、
  原样随遍历结果回传；store 既不臆造也不丢血缘。每个节点/边都带血缘根。
- Isolation —— RESTRICTED 来源的节点【绝不出现在任何读/遍历结果里】，也【绝不作多跳跳板】：
  图遍历是继承「RESTRICTED 两出口」的【新出口】，故在存储层就把 sensitivity==RESTRICTED 的
  节点从 get_node / neighbors / subgraph / traverse 全部剔除（触及它的边一并不出现）。这与
  retrieval/link、retrieval/rerank 两出口同口径——GraphRAG 不得绕过竞品/越权与 RESTRICTED 隔离。
  诚实边界：RESTRICTED 节点仍【入库】（保留血缘可审计），但【永不出域】；存盘 adapter 的 at-rest
  衍生面护栏与 VectorStore 同款（见 CLAUDE.md）。
- Determinism —— 节点/边按 id 升序遍历，BFS 前沿按 id 升序展开，结果一律按 id 升序去重，
  跨调用 / 跨独立实例逐位一致；同输入同输出（W7a 建图确定 + 本层遍历确定 ⇒ 端到端确定）。
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# RESTRICTED 隔离不出域的常量，从 rerank 出口单点再导出（与两出口同一真源，避免漂移）。
from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY

# 遍历方向。
DIRECTION_OUT = "out"
DIRECTION_IN = "in"
DIRECTION_BOTH = "both"

# 多跳遍历默认深度上限（足够覆盖派生链/母子两三跳，又不至无界扫全图）。
DEFAULT_MAX_DEPTH = 3

# 工厂读取的环境变量名（缺省 spec 时生效；范式同 VECTOR_STORE_ENV）。
GRAPH_STORE_ENV = "RAGSPINE_GRAPH_STORE"

# 第三方后端自动发现的 entry-point group：一个包在此 group 下注册一行
# （pyproject `[project.entry-points."ragspine.graph_stores"]`），make_graph_store 就能按名字
# 选中它——核心零改动、零 SDK import（范式同 VECTOR_STORE_ENTRY_POINT_GROUP）。
GRAPH_STORE_ENTRY_POINT_GROUP = "ragspine.graph_stores"

__all__ = [
    "RESTRICTED_SENSITIVITY",
    "DIRECTION_OUT",
    "DIRECTION_IN",
    "DIRECTION_BOTH",
    "DEFAULT_MAX_DEPTH",
    "GRAPH_STORE_ENV",
    "GRAPH_STORE_ENTRY_POINT_GROUP",
    "GraphNode",
    "GraphEdge",
    "Subgraph",
    "GraphStore",
    "InProcessGraphStore",
    "make_graph_store",
]


@dataclass(frozen=True)
class GraphNode:
    """一个类型化节点 + 其身份 + 血缘/过滤元数据。

    id 是稳定身份（entity_code / metric_code / period / doc_id / 外部主体名 …）——provenance 锚点；
    type 是节点种类（'entity' / 'metric' / 'period' / 'doc' / 'external_entity' / 'geography' …）；
    metadata 含 source_doc_id / source_locator（血缘根）与 sensitivity（隔离判据），可带更多过滤维。
    frozen 保证入库后不被就地改写。
    """

    id: str
    type: str
    label: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    """一条类型化有向边 src→dst + 关系种类 + 血缘元数据。

    type 是关系种类（'parent_of' / 'derives' / 'competes_with' / 'mentions' …）；metadata 含
    source_doc_id / source_locator（profile 派生边来源记为受控 config，doc 共现边来源记为事实/块血缘）。
    """

    src: str
    dst: str
    type: str
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Subgraph:
    """子图查询结果：节点 + 边（均已施隔离，按 id / (src,dst,type) 升序，确定性）。"""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


@runtime_checkable
class GraphStore(Protocol):
    """图存储缝的最小结构接口（core 只 import 这个 Protocol，不 import 任何图库 SDK）。

    刻意保持在不变量所需的最低公约数（upsert + 三种遍历 + count）；后端特定旋钮（索引参数、
    Cypher 方言等）属各 adapter 自己的配置，不进核心 Protocol。三种遍历是 W7a 多跳能力的基语：
    neighbors（1 跳）/ traverse（多跳可达集）/ subgraph（诱导子图）。
    """

    def upsert_nodes(self, nodes: Sequence[GraphNode]) -> int: ...

    def upsert_edges(self, edges: Sequence[GraphEdge]) -> int: ...

    def get_node(self, node_id: str) -> GraphNode | None: ...

    def neighbors(
        self,
        node_id: str,
        *,
        edge_type: str | None = None,
        direction: str = DIRECTION_OUT,
        where: Mapping[str, str] | None = None,
    ) -> list[GraphNode]: ...

    def subgraph(
        self,
        seeds: Sequence[str],
        *,
        depth: int = 1,
        edge_types: Sequence[str] | None = None,
        where: Mapping[str, str] | None = None,
    ) -> Subgraph: ...

    def traverse(
        self,
        start: str,
        *,
        edge_types: Sequence[str] | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        direction: str = DIRECTION_OUT,
        where: Mapping[str, str] | None = None,
    ) -> list[GraphNode]: ...

    def count_nodes(self) -> int: ...

    def count_edges(self) -> int: ...


def _node_matches(node: GraphNode, where: Mapping[str, str] | None) -> bool:
    """节点 where 过滤：在 {type, label, **metadata} 的合并视图上做精确 AND 匹配。

    把 type / label 一并纳入可过滤面，让「只要 metric 型邻居」「只要某 entity」这类图查询用一个
    where 就能表达（metadata 优先，故 metadata 里若也有 type 键以 metadata 为准——不会发生于本域，
    type 是保留过滤维）。语义同 VectorStore._matches：缺键即排除。"""
    if not where:
        return True
    view = {"type": node.type, "label": node.label, **dict(node.metadata)}
    for key, value in where.items():
        if view.get(key) != value:
            return False
    return True


def _is_restricted(node: GraphNode | None) -> bool:
    """节点是否 RESTRICTED 来源（隔离判据）：metadata.sensitivity 归一大写后 == RESTRICTED。"""
    if node is None:
        return True  # 缺失节点等同不可达（不出域）
    return str(node.metadata.get("sensitivity", "")).upper() == RESTRICTED_SENSITIVITY


class InProcessGraphStore:
    """零依赖、确定性的内存默认实现：邻接 dict + 按 id 升序遍历 + RESTRICTED 出口隔离。

    存储：节点按 id 存 dict（upsert 替换）；边按 (src,dst,type) 存 dict（upsert 替换）。读/遍历
    一律剔除 RESTRICTED 来源节点与触及它的边——图遍历是继承两出口的【新出口】。
    """

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[tuple[str, str, str], GraphEdge] = {}

    def upsert_nodes(self, nodes: Sequence[GraphNode]) -> int:
        """写入/替换节点（按 id），返回写入条数；空输入 → 0。"""
        n = 0
        for node in nodes:
            self._nodes[node.id] = node
            n += 1
        return n

    def upsert_edges(self, edges: Sequence[GraphEdge]) -> int:
        """写入/替换边（按 (src,dst,type)），返回写入条数；空输入 → 0。"""
        n = 0
        for edge in edges:
            self._edges[(edge.src, edge.dst, edge.type)] = edge
            n += 1
        return n

    def get_node(self, node_id: str) -> GraphNode | None:
        """取节点；缺失或 RESTRICTED 来源 → None（隔离：RESTRICTED 永不出域）。"""
        node = self._nodes.get(node_id)
        if _is_restricted(node):
            return None
        return node

    def _visible(self, node_id: str) -> GraphNode | None:
        """内部：node_id 对应的【可见】节点（存在且非 RESTRICTED），否则 None。"""
        node = self._nodes.get(node_id)
        return None if _is_restricted(node) else node

    def _out_edges(self, node_id: str, edge_type: str | None) -> list[GraphEdge]:
        return [
            e
            for (src, _dst, etype), e in self._edges.items()
            if src == node_id and (edge_type is None or etype == edge_type)
        ]

    def _in_edges(self, node_id: str, edge_type: str | None) -> list[GraphEdge]:
        return [
            e
            for (_src, dst, etype), e in self._edges.items()
            if dst == node_id and (edge_type is None or etype == edge_type)
        ]

    def _step(
        self, node_id: str, edge_type: str | None, direction: str
    ) -> list[str]:
        """从 node_id 出发一步可达的邻居 id（按方向 + edge_type 过滤；不在此施隔离/where）。"""
        out: list[str] = []
        if direction in (DIRECTION_OUT, DIRECTION_BOTH):
            out.extend(e.dst for e in self._out_edges(node_id, edge_type))
        if direction in (DIRECTION_IN, DIRECTION_BOTH):
            out.extend(e.src for e in self._in_edges(node_id, edge_type))
        return out

    def neighbors(
        self,
        node_id: str,
        *,
        edge_type: str | None = None,
        direction: str = DIRECTION_OUT,
        where: Mapping[str, str] | None = None,
    ) -> list[GraphNode]:
        """1 跳邻居（按方向 + edge_type 过滤），按 id 升序去重；施隔离 + where。

        若起点本身 RESTRICTED / 不存在 → []（不可作跳板）。被隔离/where 排除的邻居即便相邻也不出现。
        """
        if self._visible(node_id) is None:
            return []
        seen: dict[str, GraphNode] = {}
        for nid in self._step(node_id, edge_type, direction):
            node = self._visible(nid)
            if node is None or not _node_matches(node, where):
                continue
            seen[nid] = node
        return [seen[nid] for nid in sorted(seen)]

    def traverse(
        self,
        start: str,
        *,
        edge_types: Sequence[str] | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        direction: str = DIRECTION_OUT,
        where: Mapping[str, str] | None = None,
    ) -> list[GraphNode]:
        """多跳可达集（BFS，≤max_depth 跳，不含起点），按 id 升序去重；施隔离 + where。

        RESTRICTED 节点既不出现、也不作跳板（永不入队）。edge_types=None 表示不限关系种类；否则
        只沿这些 edge_type 走。前沿按 id 升序展开 ⇒ 跨实例逐位一致（确定性）。where 仅筛【返回集】，
        不影响可达性扩展（与 VectorStore where 同为「打分/返回前过滤」语义）。
        """
        if self._visible(start) is None:
            return []
        etypes = set(edge_types) if edge_types is not None else None
        reached: dict[str, GraphNode] = {}
        visited: set[str] = {start}
        frontier = [start]
        for _ in range(max(0, max_depth)):
            nxt: list[str] = []
            for nid in sorted(frontier):
                for et in sorted(etypes) if etypes is not None else (None,):
                    for neighbor in self._step(nid, et, direction):
                        if neighbor in visited:
                            continue
                        node = self._visible(neighbor)
                        visited.add(neighbor)
                        if node is None:
                            continue  # RESTRICTED / 缺失：不入结果、不作跳板
                        reached[neighbor] = node
                        nxt.append(neighbor)
            frontier = nxt
            if not frontier:
                break
        out = {nid: node for nid, node in reached.items() if _node_matches(node, where)}
        return [out[nid] for nid in sorted(out)]

    def subgraph(
        self,
        seeds: Sequence[str],
        *,
        depth: int = 1,
        edge_types: Sequence[str] | None = None,
        where: Mapping[str, str] | None = None,
    ) -> Subgraph:
        """种子集 ≤depth 跳诱导子图：节点（含可见种子）+ 两端皆可见的边，均施隔离 + where。

        节点 = 可见种子 ∪ 各种子 depth 跳可达的可见节点；边 = 这些节点之间、edge_types 命中、
        两端皆在结果节点集里的边（触及 RESTRICTED 的边因端点被剔除而自然不出现）。确定性升序。
        """
        etypes = set(edge_types) if edge_types is not None else None
        nodes: dict[str, GraphNode] = {}
        for seed in seeds:
            node = self._visible(seed)
            if node is not None and _node_matches(node, where):
                nodes[seed] = node
            for reached in self.traverse(
                seed, edge_types=edge_types, max_depth=depth, where=where
            ):
                nodes[reached.id] = reached
        edges: list[GraphEdge] = []
        for (src, dst, etype), edge in self._edges.items():
            if src in nodes and dst in nodes and (etypes is None or etype in etypes):
                edges.append(edge)
        edges.sort(key=lambda e: (e.src, e.dst, e.type))
        return Subgraph(
            nodes=tuple(nodes[nid] for nid in sorted(nodes)),
            edges=tuple(edges),
        )

    def count_nodes(self) -> int:
        """节点总数（含 RESTRICTED——入库计数；可见性仅作用于读/遍历）。"""
        return len(self._nodes)

    def count_edges(self) -> int:
        """边总数。"""
        return len(self._edges)


# ---------------------------------------------------------------------------
# 注册表：内置后端名字 -> 惰性 loader（返回 GraphStore【类】，尚不实例化）。范式同 make_vector_store：
# loader 仅在被调用时才 import 对应 adapter 模块（模块体零顶层 SDK import）。第三方后端【不】登记此表，
# 而是经 entry-point 自动发现（见 _discover_entry_points），无需任何核心 PR。
# 当前内置：in_process（零依赖默认）+ networkx（薄 adapter，behind [graph]）。kuzu / neo4j 是
# 预留 seam——第三方今天即可经 entry-point group 注册（见 GRAPH_STORE_ENTRY_POINT_GROUP），
# 一方 adapter 是 follow-up（见 docs/prd-quality-depth.md W7c）。
# ---------------------------------------------------------------------------
def _load_in_process() -> type[GraphStore]:
    return InProcessGraphStore


def _load_networkx() -> type[GraphStore]:
    from ragspine.graph.adapters.networkx_store import NetworkxGraphStore

    return NetworkxGraphStore


_BUILTIN_LOADERS: dict[str, Callable[[], type[GraphStore]]] = {
    "in_process": _load_in_process,
    "in-process": _load_in_process,
    "inprocess": _load_in_process,
    "memory": _load_in_process,
    "networkx": _load_networkx,
    "nx": _load_networkx,
}

# 错误信息中展示的内置规范名（别名不重复列出，保持可读）。
_BUILTIN_DISPLAY_NAMES = ("none", "in_process", "networkx")


def _discover_entry_points() -> Sequence[Any]:
    """发现第三方在 GRAPH_STORE_ENTRY_POINT_GROUP 下注册的 GraphStore 后端（范式同 vector store）。"""
    from importlib.metadata import entry_points

    return list(entry_points(group=GRAPH_STORE_ENTRY_POINT_GROUP))


def _resolve_factory(normalized: str) -> Callable[..., GraphStore]:
    """归一化名字 -> 可 **kwargs 调用得到 GraphStore 的工厂（内置类或 entry-point 目标）。

    内置优先于同名 entry point（第三方不能劫持内置语义）；未命中再回落 entry-point 自动发现。
    只【解析】不【实例化】——对内置 adapter 不触发其 SDK import（SDK 由返回类 __init__ 延迟 import）。
    """
    loader = _BUILTIN_LOADERS.get(normalized)
    if loader is not None:
        return loader()
    discovered = _discover_entry_points()
    for entry_point in discovered:
        if entry_point.name.strip().lower() == normalized:
            factory: Callable[..., GraphStore] = entry_point.load()
            return factory
    names = sorted({entry_point.name for entry_point in discovered})
    raise ValueError(
        f"未知 graph store spec：{normalized!r}"
        f"（内置可选 {' / '.join(_BUILTIN_DISPLAY_NAMES)}；"
        f"已发现的 entry-point 后端：{names or '无'}；"
        f"第三方包可在 entry-point group {GRAPH_STORE_ENTRY_POINT_GROUP!r} 下注册一个后端）"
    )


def make_graph_store(spec: str | None = None, **kwargs: Any) -> GraphStore | None:
    """图存储工厂：把「选哪个 store」从改代码降为一个 spec/env（范式同 make_vector_store）。

    spec 取值（大小写 / 留白不敏感；缺省读环境变量 RAGSPINE_GRAPH_STORE）：
        - None / 'none'                          -> None（不注入具体 store；调用方自建内存默认）
        - 'in_process' / 'in-process' / 'memory' -> InProcessGraphStore（零依赖确定性内存默认）
        - 'networkx' / 'nx'                       -> NetworkxGraphStore（behind [graph] extra，延迟 import）
        - 其余                                    -> entry-point 自动发现；都不命中 -> ValueError 列出可选名字

    名字经注册表解析（内置 loader 或 entry point），再以 **kwargs 实例化；内置 adapter 的 SDK
    在实例化时才延迟 import（缺 [graph] extra 时由 adapter.__init__ 抛可执行的 pip 提示）。
    """
    if spec is None:
        spec = os.environ.get(GRAPH_STORE_ENV)
    normalized = (spec or "none").strip().lower()
    if normalized == "none":
        return None
    factory = _resolve_factory(normalized)
    return factory(**kwargs)
