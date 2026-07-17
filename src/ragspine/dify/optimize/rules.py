"""8 条纯静态优化规则 + 注入式环境上限 OptimizeEnv。

每条规则都是纯函数 `rule_xxx(ir, env) -> list[Suggestion]`：只读 IR 结构（节点类型 / 数据依赖 /
parallel_layers / 节点配置），绝不调任何 provider、绝不读真实环境变量（上限经 OptimizeEnv 注入）。
迭代体（IterationNode.body）按需递归进其 `.graph.nodes` 检查内层节点。

规则 id 稳定可 grep：PARALLEL_001/002、BOTTLE_001/002、CACHE_001、RESOURCE_001/002、LLM_001。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ragspine.dify.ir.model import (
    IRNode,
    IterationNode,
    KnowledgeRetrievalNode,
    LLMNode,
    WorkflowIR,
)
from ragspine.dify.optimize.suggestion import Category, Severity, Suggestion


@dataclass(frozen=True)
class OptimizeEnv:
    """注入式部署侧环境上限（不读 os.environ，由调用方显式传入）。"""

    max_parallel_workers: int = 10   # 部署侧并发上限（线程池可用 worker 数上限）
    max_llm_tokens: int = 8192       # 单次 LLM max_tokens 的合理上界（超过即“极大”）


# ---------------------------------------------------------------------------
# 通用 helper（节点分类 / 迭代体遍历）。
# ---------------------------------------------------------------------------


def _is_heavy(node: IRNode) -> bool:
    """重型节点：llm / code / iteration / 检索 / 工具 / 参数抽取 / http 请求。

    按 kind 判定：P9 后 http-request 已是真实 IR 节点（HttpRequestNode.kind ==
    'http-request'）；形状不支持而落 UnsupportedNode 的 http 节点其 kind 同为
    node_type（'http-request'），两者天然被同一判据覆盖。
    """
    return node.kind in (
        "llm", "code", "iteration",
        "knowledge-retrieval", "tool", "parameter-extractor", "http-request",
    )


def _is_io_heavy(node: IRNode) -> bool:
    """I/O 重型节点：llm / 检索 / 工具 / http 请求（迭代体内是否值得并行的判定）。"""
    return node.kind in (
        "llm", "knowledge-retrieval", "tool", "parameter-extractor", "http-request",
    )


def _body_nodes(node: IterationNode) -> tuple[IRNode, ...]:
    """取一个迭代节点子图的全部内层节点（无子图则空）。"""
    return node.body.graph.nodes if node.body is not None else ()


def _branch_gate(ir: WorkflowIR, node_id: str) -> tuple[str, str] | None:
    """若 node_id 的唯一入边来自某 if-else 节点的某 handle，返回 (ifelse_id, handle)，否则 None。

    用于识别互斥分支：经同一 if-else 不同 handle 进入的两节点，运行期只走其一，不构成并发机会。
    """
    node_map = ir.graph.node_map()
    preds = ir.graph.predecessors(node_id)
    if len(preds) != 1:
        return None
    edge = preds[0]
    src = node_map.get(edge.source)
    if src is not None and src.kind == "if-else":
        return (edge.source, edge.source_handle or "true")
    return None


def _drop_branch_exclusive(ir: WorkflowIR, node_ids: list[str]) -> list[str]:
    """从同层节点里剔除「与本组其它节点互斥」者：与某节点共享 if-else 父、但 handle 不同即互斥。

    保留极大的「彼此非互斥」子集（贪心：逐个加入，与已留集合无互斥冲突才保留）。
    """
    gates = {nid: _branch_gate(ir, nid) for nid in node_ids}
    kept: list[str] = []
    for nid in node_ids:
        g = gates[nid]
        if g is None:
            kept.append(nid)
            continue
        ifelse_id, handle = g
        conflict = any(
            gates[k] is not None
            and gates[k][0] == ifelse_id  # type: ignore[index]
            and gates[k][1] != handle  # type: ignore[index]
            for k in kept
        )
        if not conflict:
            kept.append(nid)
    return kept


# ---------------------------------------------------------------------------
# PARALLEL_*：并行机会。
# ---------------------------------------------------------------------------


def rule_parallel_001(ir: WorkflowIR, env: OptimizeEnv) -> list[Suggestion]:
    """某 parallel_layer 内有 ≥2 个互不依赖的重型节点：它们已被 codegen 自动并发执行，
    提示确认部署线程池容量与之匹配（并行机会告警，非缺陷）。每个命中层一条建议。"""
    out: list[Suggestion] = []
    node_map = ir.graph.node_map()
    for idx, layer in enumerate(ir.parallel_layers):
        heavy = [nid for nid in layer if nid in node_map and _is_heavy(node_map[nid])]
        # 剔除「互斥分支」节点：经同一 if-else 的不同 handle 进入者运行期只走其一，非并发机会。
        heavy = _drop_branch_exclusive(ir, heavy)
        if len(heavy) < 2:
            continue
        out.append(Suggestion(
            rule_id="PARALLEL_001",
            severity=Severity.HIGH,
            category=Category.PARALLEL,
            title=f"第 {idx} 层有 {len(heavy)} 个互相独立的重型节点可并发执行",
            detail=(
                f"节点 {heavy} 彼此无数据依赖，codegen 会自动用线程池并发执行；"
                f"请确认部署侧线程池容量（当前注入上限 {env.max_parallel_workers}）足以承载。"
            ),
            node_ids=tuple(heavy),
        ))
    return out


def rule_parallel_002(ir: WorkflowIR, env: OptimizeEnv) -> list[Suggestion]:
    """迭代节点体内含 I/O 重型节点（内层 llm 或 http/检索/工具）但 is_parallel=False：
    逐项串行跑 I/O 浪费时间，建议开启并行迭代。"""
    out: list[Suggestion] = []
    for node in ir.graph.nodes:
        if not isinstance(node, IterationNode) or node.is_parallel:
            continue
        if any(_is_io_heavy(inner) for inner in _body_nodes(node)):
            out.append(Suggestion(
                rule_id="PARALLEL_002",
                severity=Severity.MEDIUM,
                category=Category.PARALLEL,
                title=f"迭代节点 {node.id!r} 体内含 I/O 重型节点但未开启并行",
                detail="每轮迭代会串行执行内层 I/O 调用，建议设置 is_parallel=true 以并发提速。",
                node_ids=(node.id,),
            ))
    return out


# ---------------------------------------------------------------------------
# BOTTLE_*：串行瓶颈。
# ---------------------------------------------------------------------------


def rule_bottle_001(ir: WorkflowIR, env: OptimizeEnv) -> list[Suggestion]:
    """存在 ≥3 个 LLM 节点的严格串行链（沿数据/控制流边逐个依赖，A→B→C…全为 llm）：
    串行 LLM 链是显著延迟来源。报最长链（去其子链）。"""
    node_map = ir.graph.node_map()
    llm_ids = {nid for nid, n in node_map.items() if isinstance(n, LLMNode)}
    # 仅保留两端都是 llm 的边，构成 llm 子图的邻接表。
    succ: dict[str, list[str]] = {nid: [] for nid in llm_ids}
    indeg: dict[str, int] = {nid: 0 for nid in llm_ids}
    for e in ir.graph.edges:
        if e.source in llm_ids and e.target in llm_ids:
            succ[e.source].append(e.target)
            indeg[e.target] += 1

    # 从无 llm 前驱的链头出发，沿单链向后走，收集 ≥3 长度的极大链。
    chains: list[list[str]] = []
    for nid in ir.topo_order:
        if nid not in llm_ids or indeg[nid] != 0:
            continue
        chain = [nid]
        cur = nid
        while len(succ.get(cur, [])) == 1:
            cur = succ[cur][0]
            chain.append(cur)
        if len(chain) >= 3:
            chains.append(chain)

    return [
        Suggestion(
            rule_id="BOTTLE_001",
            severity=Severity.MEDIUM,
            category=Category.BOTTLENECK,
            title=f"{len(chain)} 个 LLM 节点串行依赖，构成延迟瓶颈",
            detail=(
                f"链 {chain} 中每个 LLM 都依赖上一个，无法并发；"
                "可考虑合并提示、缩短链路或引入缓存以降低端到端延迟。"
            ),
            node_ids=tuple(chain),
        )
        for chain in chains
    ]


def rule_bottle_002(ir: WorkflowIR, env: OptimizeEnv) -> list[Suggestion]:
    """迭代节点体内含同步 HTTP 节点（kind='http-request'，含真实 HttpRequestNode 与
    形状不支持而留钩子的 UnsupportedNode）：每轮迭代一次同步 HTTP，是按项放大的串行瓶颈。"""
    out: list[Suggestion] = []
    for node in ir.graph.nodes:
        if not isinstance(node, IterationNode):
            continue
        has_http = any(
            inner.kind == "http-request" for inner in _body_nodes(node)
        )
        if has_http:
            out.append(Suggestion(
                rule_id="BOTTLE_002",
                severity=Severity.MEDIUM,
                category=Category.BOTTLENECK,
                title=f"迭代节点 {node.id!r} 体内每轮发起同步 HTTP 请求",
                detail="逐项同步 HTTP 会随数组长度线性放大耗时，建议批量化或并行化该请求。",
                node_ids=(node.id,),
            ))
    return out


# ---------------------------------------------------------------------------
# CACHE_*：缓存机会。
# ---------------------------------------------------------------------------


def rule_cache_001(ir: WorkflowIR, env: OptimizeEnv) -> list[Suggestion]:
    """≥2 个 knowledge-retrieval 节点引用同一数据集（dataset_ids 相同）：
    重复检索同一数据集可加缓存。按数据集分组，每组（≥2 个）一条建议。"""
    groups: dict[str, list[str]] = {}
    for node in ir.graph.nodes:
        if not isinstance(node, KnowledgeRetrievalNode):
            continue
        if not node.dataset_ids:
            continue
        # 数据集 id 集合统一成稳定 key（顺序无关）。
        key = ",".join(sorted(node.dataset_ids))
        groups.setdefault(key, []).append(node.id)

    out: list[Suggestion] = []
    for key, ids in groups.items():
        if len(ids) < 2:
            continue
        out.append(Suggestion(
            rule_id="CACHE_001",
            severity=Severity.LOW,
            category=Category.CACHE,
            title=f"{len(ids)} 个知识检索节点查询同一数据集，可加缓存",
            detail=f"节点 {ids} 均检索数据集 {key!r}，结果可缓存以省去重复检索开销。",
            node_ids=tuple(ids),
        ))
    return out


# ---------------------------------------------------------------------------
# RESOURCE_*：资源配置。
# ---------------------------------------------------------------------------


def rule_resource_001(ir: WorkflowIR, env: OptimizeEnv) -> list[Suggestion]:
    """并行迭代的 parallel_nums 超过注入的部署侧并发上限：会超额占用线程资源。"""
    out: list[Suggestion] = []
    for node in ir.graph.nodes:
        if not isinstance(node, IterationNode):
            continue
        if node.is_parallel and node.parallel_nums > env.max_parallel_workers:
            out.append(Suggestion(
                rule_id="RESOURCE_001",
                severity=Severity.MEDIUM,
                category=Category.RESOURCE,
                title=f"迭代节点 {node.id!r} 并发数 {node.parallel_nums} 超过部署上限",
                detail=(
                    f"配置 parallel_nums={node.parallel_nums}，超过注入的并发上限 "
                    f"{env.max_parallel_workers}，会过度占用线程池资源；建议调低或扩容。"
                ),
                node_ids=(node.id,),
            ))
    return out


def rule_resource_002(ir: WorkflowIR, env: OptimizeEnv) -> list[Suggestion]:
    """声明 is_parallel=True 但 parallel_nums<=1：并行形同虚设（配置错误）。"""
    out: list[Suggestion] = []
    for node in ir.graph.nodes:
        if not isinstance(node, IterationNode):
            continue
        if node.is_parallel and node.parallel_nums <= 1:
            out.append(Suggestion(
                rule_id="RESOURCE_002",
                severity=Severity.LOW,
                category=Category.RESOURCE,
                title=f"迭代节点 {node.id!r} 声明并行但 parallel_nums={node.parallel_nums}（实为串行）",
                detail="已开启 is_parallel 却把并发数设为 ≤1，并行未生效；请上调 parallel_nums。",
                node_ids=(node.id,),
            ))
    return out


# ---------------------------------------------------------------------------
# LLM_*：LLM 配置。
# ---------------------------------------------------------------------------


def _llm_nodes(ir: WorkflowIR) -> list[LLMNode]:
    """收集本图及所有迭代体内的全部 LLMNode（递归进 iteration 子图）。"""
    out: list[LLMNode] = []
    for node in ir.graph.nodes:
        if isinstance(node, LLMNode):
            out.append(node)
        elif isinstance(node, IterationNode) and node.body is not None:
            out.extend(_llm_nodes(node.body))
    return out


def rule_llm_001(ir: WorkflowIR, env: OptimizeEnv) -> list[Suggestion]:
    """LLM 节点 max_tokens 配置体检：缺失（None）提示设置；过大（> 上限）提示可能浪费。
    含迭代体内层 LLM。每个命中节点一条建议。"""
    out: list[Suggestion] = []
    for node in _llm_nodes(ir):
        if node.max_tokens is None:
            out.append(Suggestion(
                rule_id="LLM_001",
                severity=Severity.LOW,
                category=Category.LLM,
                title=f"LLM 节点 {node.id!r} 未设置 max_tokens",
                detail="未设 max_tokens 时输出长度不可控，可能超额计费；建议显式设置合理上界。",
                node_ids=(node.id,),
            ))
        elif node.max_tokens > env.max_llm_tokens:
            out.append(Suggestion(
                rule_id="LLM_001",
                severity=Severity.LOW,
                category=Category.LLM,
                title=f"LLM 节点 {node.id!r} 的 max_tokens={node.max_tokens} 偏大",
                detail=(
                    f"max_tokens={node.max_tokens} 超过合理上界 {env.max_llm_tokens}，"
                    "可能造成不必要的延迟与费用；建议按实际需要调小。"
                ),
                node_ids=(node.id,),
            ))
    return out


# 全部规则（稳定顺序，analyzer 据此遍历）。
ALL_RULES: tuple[Callable[[WorkflowIR, OptimizeEnv], list[Suggestion]], ...] = (
    rule_parallel_001,
    rule_parallel_002,
    rule_bottle_001,
    rule_bottle_002,
    rule_cache_001,
    rule_resource_001,
    rule_resource_002,
    rule_llm_001,
)
