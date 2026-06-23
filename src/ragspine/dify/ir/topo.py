"""拓扑排序与并行分层（Kahn 算法）+ 环检测。纯 stdlib。

输入是节点 id 集合 + 有向边（控制流 + 数据依赖合并后的边）。输出：
- topo_order：一个确定性的合法拓扑序（同一图恒定，按 id 排序打破平局，保证离线可复现）。
- parallel_layers：拓扑分层——第 k 层是「所有前驱都在更早层」的节点，同层彼此无依赖、可并发。

环（Dify 工作流应为 DAG）→ CyclicGraph，带卷入环的节点 id 上下文。
"""

from __future__ import annotations

from collections.abc import Iterable

from ragspine.dify.errors import CyclicGraph


def _adjacency(
    node_ids: Iterable[str], edges: Iterable[tuple[str, str]]
) -> tuple[list[str], dict[str, set[str]], dict[str, int]]:
    """构建邻接表与入度表。返回 (排序后的节点列表, 后继集合, 入度)。

    边以 set 去重（同向重复边只算一次依赖），自指边忽略不计入度（不影响拓扑且避免假环）。
    未在 node_ids 中声明但出现在边里的节点不引入（边的端点应都在节点集内，否则上游 lower 有 bug）。
    """
    nodes = sorted(set(node_ids))
    node_set = set(nodes)
    succ: dict[str, set[str]] = {n: set() for n in nodes}
    indeg: dict[str, int] = {n: 0 for n in nodes}
    seen: set[tuple[str, str]] = set()
    for src, dst in edges:
        if src not in node_set or dst not in node_set or src == dst:
            continue
        if (src, dst) in seen:
            continue
        seen.add((src, dst))
        succ[src].add(dst)
        indeg[dst] += 1
    return nodes, succ, indeg


def topo_order(
    node_ids: Iterable[str], edges: Iterable[tuple[str, str]]
) -> tuple[str, ...]:
    """Kahn 拓扑排序，确定性（每步在入度为 0 的节点里取 id 最小者）。环 → CyclicGraph。"""
    nodes, succ, indeg = _adjacency(node_ids, edges)
    ready = sorted(n for n in nodes if indeg[n] == 0)
    order: list[str] = []
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        newly: list[str] = []
        for nxt in succ[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                newly.append(nxt)
        # 合并后保持有序，维持确定性。
        for n in newly:
            _insort(ready, n)
    if len(order) != len(nodes):
        stuck = sorted(n for n in nodes if n not in set(order))
        raise CyclicGraph(
            f"节点图存在环，无法拓扑排序；卷入环的节点：{stuck}",
            nodes=stuck,
        )
    return tuple(order)


def parallel_layers(
    node_ids: Iterable[str], edges: Iterable[tuple[str, str]]
) -> tuple[tuple[str, ...], ...]:
    """拓扑分层：同层节点彼此无依赖、可并发；层间严格有序。环 → CyclicGraph。

    第 0 层为所有入度 0 的节点；移除它们后新的入度 0 节点构成第 1 层，依此类推。
    每层内按 id 排序，保证确定性。
    """
    nodes, succ, indeg = _adjacency(node_ids, edges)
    remaining = dict(indeg)
    layers: list[tuple[str, ...]] = []
    placed = 0
    frontier = sorted(n for n in nodes if remaining[n] == 0)
    while frontier:
        layers.append(tuple(frontier))
        placed += len(frontier)
        nxt_frontier: list[str] = []
        for n in frontier:
            for m in succ[n]:
                remaining[m] -= 1
                if remaining[m] == 0:
                    nxt_frontier.append(m)
        frontier = sorted(nxt_frontier)
    if placed != len(nodes):
        stuck = sorted(n for n in nodes if remaining[n] > 0)
        raise CyclicGraph(
            f"节点图存在环，无法分层；卷入环的节点：{stuck}",
            nodes=stuck,
        )
    return tuple(layers)


def _insort(seq: list[str], value: str) -> None:
    """把 value 插入有序列表 seq 的正确位置（保持升序，确定性）。"""
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    seq.insert(lo, value)
