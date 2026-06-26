"""networkx 适配器：把 GraphStore 缝落到 networkx.MultiDiGraph（成熟图库的索引/算法基座）。

第一个真实 GraphStore adapter（GraphStore 缝的五段式契约见 src/ragspine/graph/store.py 顶部）。范式同
ragspine 既有 VectorStore 适配（sqlite_vec / qdrant）：networkx 延迟 import（不装也能 import 本模块），
behind `[graph]` extra；零依赖默认仍是 ragspine.graph.store.InProcessGraphStore。networkx 是
BSD-3，过 ADR 0009 的 ≤Apache-2.0 许可门。

**口径与 InProcessGraphStore 逐字段一致**——直接复用 store._is_restricted / store._node_matches，
且 neighbors / traverse / subgraph 算法逐行同构、一律按 id /(src,dst,type) 升序，故同一套
tests/conformance 全过：RESTRICTED 来源节点绝不出现在任何读/遍历结果、也绝不作多跳跳板；
where「缺键即排除」的精确 AND；provenance metadata 原样存原样回传；跨实例 byte-identical。

存储：
  - 节点存进 MultiDiGraph 节点属性 `obj=GraphNode`（按 id；add_node 同 id 覆盖 = upsert 替换）。
  - 边按 `key=edge.type` 存进 MultiDiGraph（(src,dst,type) 唯一；add_edge 同键覆盖 = upsert 替换），
    属性 `obj=GraphEdge`。
  - add_edge 会为未入库的端点凭空建出无 `obj` 的「幽灵节点」；统一按「缺失节点」处理——不可见、
    不计入 count_nodes、不作跳板——与 InProcessGraphStore 对未 upsert 端点的语义一致。
读/遍历一律剔除 RESTRICTED 来源节点与触及它的边（图遍历是继承两出口的【新出口】）。

诚实边界（持久化 + 隔离）：当前 MultiDiGraph 纯进程内、零落盘，RESTRICTED 节点入库（保留血缘
可审计）但永不出域；若将来 adapter 支持落盘，其 at-rest 衍生面护栏与 VectorStore 同款（见 CLAUDE.md）。
"""

from collections.abc import Mapping, Sequence
from typing import Any

from ragspine.graph.store import (
    DEFAULT_MAX_DEPTH,
    DIRECTION_BOTH,
    DIRECTION_IN,
    DIRECTION_OUT,
    GraphEdge,
    GraphNode,
    Subgraph,
    _is_restricted,
    _node_matches,
)


class NetworkxGraphStore:
    """networkx.MultiDiGraph 之上的薄 GraphStore 适配器（复用 store.py 的隔离/过滤/确定性逻辑）。"""

    def __init__(self) -> None:
        try:
            import networkx as nx  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "未安装 networkx：pip install 'ragspine[graph]' 或 pip install networkx"
            ) from exc
        self._g: Any = nx.MultiDiGraph()

    # -- upsert / count -----------------------------------------------------
    def upsert_nodes(self, nodes: Sequence[GraphNode]) -> int:
        """写入/替换节点（按 id），返回写入条数；空输入 → 0。"""
        n = 0
        for node in nodes:
            self._g.add_node(node.id, obj=node)
            n += 1
        return n

    def upsert_edges(self, edges: Sequence[GraphEdge]) -> int:
        """写入/替换边（按 (src,dst,type)，即 MultiDiGraph 的 key），返回写入条数；空输入 → 0。"""
        n = 0
        for edge in edges:
            self._g.add_edge(edge.src, edge.dst, key=edge.type, obj=edge)
            n += 1
        return n

    def count_nodes(self) -> int:
        """节点总数（仅计入带 obj 的已入库节点，幽灵端点不计；含 RESTRICTED——计数不施可见性）。"""
        return sum(1 for _nid, data in self._g.nodes(data=True) if "obj" in data)

    def count_edges(self) -> int:
        """边总数。"""
        return int(self._g.number_of_edges())

    # -- 内部：可见性 / 单步（隔离判据集中在此，与 InProcessGraphStore 同口径）-----
    def _node_obj(self, node_id: str) -> GraphNode | None:
        """node_id 对应的原始 GraphNode（缺失或幽灵端点 → None）。"""
        if node_id not in self._g:
            return None
        obj: GraphNode | None = self._g.nodes[node_id].get("obj")
        return obj

    def get_node(self, node_id: str) -> GraphNode | None:
        """取节点；缺失或 RESTRICTED 来源 → None（隔离：RESTRICTED 永不出域）。"""
        node = self._node_obj(node_id)
        if _is_restricted(node):
            return None
        return node

    def _visible(self, node_id: str) -> GraphNode | None:
        """内部：node_id 对应的【可见】节点（存在且非 RESTRICTED），否则 None。"""
        node = self._node_obj(node_id)
        return None if _is_restricted(node) else node

    def _step(self, node_id: str, edge_type: str | None, direction: str) -> list[str]:
        """从 node_id 出发一步可达的邻居 id（按方向 + edge_type 过滤；不在此施隔离/where）。"""
        out: list[str] = []
        if node_id not in self._g:
            return out
        if direction in (DIRECTION_OUT, DIRECTION_BOTH):
            for _src, dst, key in self._g.out_edges(node_id, keys=True):
                if edge_type is None or key == edge_type:
                    out.append(dst)
        if direction in (DIRECTION_IN, DIRECTION_BOTH):
            for src, _dst, key in self._g.in_edges(node_id, keys=True):
                if edge_type is None or key == edge_type:
                    out.append(src)
        return out

    # -- neighbors / traverse / subgraph （逐行同构 InProcessGraphStore）-----
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

        RESTRICTED 节点既不出现、也不作跳板（永不入队）。前沿按 id 升序展开 ⇒ 跨实例逐位一致。
        where 仅筛【返回集】，不影响可达性扩展（与 store.py 同语义）。
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
        for src, dst, key, obj in self._g.edges(keys=True, data="obj"):
            if src in nodes and dst in nodes and (etypes is None or key in etypes):
                edges.append(obj)
        edges.sort(key=lambda e: (e.src, e.dst, e.type))
        return Subgraph(
            nodes=tuple(nodes[nid] for nid in sorted(nodes)),
            edges=tuple(edges),
        )
