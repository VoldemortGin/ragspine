"""W7a 多跳查询入口（opt-in、独立）：子公司汇总 / 同业对比 / 派生追溯。

这是【独立的 opt-in 能力】，绝不触碰 agent/agent.py 的 answer_question（默认回路字节不变）。
每个方法都返回一个带血缘的结果 dataclass，且把两项脊柱不变量绑死在代码里：
- 安全门继承 —— 子公司汇总 / 同业对比先用 SecurityGate.screen 复核主体；命中外部/竞品主体
  即【拒答】（refused=True，绝不返回竞品数据）。安全门是确定性、永不调 LLM 的前门（ADR 0010），
  本层直接继承，不另写竞品判断。
- RESTRICTED 隔离继承 —— 一切图遍历走 GraphStore，RESTRICTED 来源节点（如 RESTRICTED chunk
  的 doc 节点）绝不出现、也绝不作跳板。本层不另写隔离逻辑。

可引用：每个结果都携血缘——汇总/对比带贡献 Fact（source_doc_id + locator），派生追溯带逐边
GraphEdge（受控 config 血缘），故任何答案都可追溯到来源。隐私安全 trace 仅记码/计数，绝不记
事实数值（emit_trace 经隐私 sink 强制兜底）。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from ragspine.agent.security_gate import SECURITY_ALLOW, SecurityGate
from ragspine.common.company_profile import DomainProfile, load_company_profile
from ragspine.common.observability import emit_trace
from ragspine.graph.store import DEFAULT_MAX_DEPTH, GraphEdge, GraphNode, GraphStore
from ragspine.storage.fact_store import Fact, FactStore

__all__ = [
    "RollupResult",
    "ComparisonRow",
    "ComparisonResult",
    "DerivationTrace",
    "GraphQuery",
]


@dataclass(frozen=True)
class RollupResult:
    """子公司汇总结果：母实体 + 指标 + 期间 + 求和（未命中 None）+ 贡献事实（可引用）。"""

    parent: str
    metric: str
    period: str
    total: float | None
    found: bool
    facts: tuple[Fact, ...] = ()
    refused: bool = False
    refusal_message: str | None = None


@dataclass(frozen=True)
class ComparisonRow:
    """同业对比的一行：实体 + 是否命中 + 命中的可引用事实（未命中 None）。"""

    entity: str
    found: bool
    fact: Fact | None = None


@dataclass(frozen=True)
class ComparisonResult:
    """同业对比结果：指标 + 期间 + 逐实体行（可引用）。"""

    metric: str
    period: str
    rows: tuple[ComparisonRow, ...] = ()
    refused: bool = False
    refusal_message: str | None = None


@dataclass(frozen=True)
class DerivationTrace:
    """派生追溯结果：起点 + 沿 derives 走到的节点链 + 逐边血缘（GraphEdge）。"""

    start: str
    edges: tuple[GraphEdge, ...] = ()
    nodes: tuple[GraphNode, ...] = ()


class GraphQuery:
    """图上的 W7a 多跳业务查询，继承安全门拒答 + 存储层 RESTRICTED 隔离。

    profile 缺省 load_company_profile()；security_gate 缺省由 profile 的外部清单 + home 名构建。
    """

    def __init__(
        self,
        store: GraphStore,
        fact_store: FactStore,
        *,
        profile: DomainProfile | None = None,
        security_gate: SecurityGate | None = None,
    ) -> None:
        self._store = store
        self._facts = fact_store
        self._profile = profile if profile is not None else load_company_profile()
        self._gate = (
            security_gate
            if security_gate is not None
            else SecurityGate(self._profile.external_entities, self._profile.home_company_name)
        )

    def subsidiary_rollup(
        self,
        parent_entity: str,
        metric: str,
        period_type: str,
        period: str,
        *,
        channel: str = "TOTAL",
    ) -> RollupResult:
        """母实体 → 沿 parent_of 取子公司 → 逐子公司查事实 → 求和（贡献事实可引用）。

        先经安全门复核母实体（含指标）：命中外部/竞品主体即拒答，绝不返回数据。多跳能力——
        图遍历找子公司 + 多次精确 SQL + 求和，是 flat top-k / 单条精确 SQL 做不到的。
        """
        period_label = f"{period_type}{period}"
        verdict = self._gate.screen(raw_question=f"{parent_entity} {metric}", metric=metric)
        if verdict.decision != SECURITY_ALLOW:
            return RollupResult(
                parent=parent_entity,
                metric=metric,
                period=period_label,
                total=None,
                found=False,
                refused=True,
                refusal_message=verdict.message,
            )
        subsidiaries = self._store.neighbors(parent_entity, edge_type="parent_of")
        found_facts: list[Fact] = []
        for sub in subsidiaries:
            found_facts.extend(self._facts.query(metric, sub.id, period_type, period, channel))
        found = bool(found_facts)
        total = sum((f.value for f in found_facts), 0.0) if found else None
        emit_trace(
            route="graph_subsidiary_rollup",
            metric=metric,
            entity=parent_entity,
            period=period_label,
            subsidiary_count=len(subsidiaries),
            found_count=len(found_facts),
        )
        return RollupResult(
            parent=parent_entity,
            metric=metric,
            period=period_label,
            total=total,
            found=found,
            facts=tuple(found_facts),
        )

    def peer_comparison(
        self,
        entities: Sequence[str],
        metric: str,
        period_type: str,
        period: str,
        *,
        channel: str = "TOTAL",
    ) -> ComparisonResult:
        """逐实体取可引用事实供并排对比；任一实体命中外部/竞品主体即整体拒答。"""
        period_label = f"{period_type}{period}"
        for entity in entities:
            verdict = self._gate.screen(raw_question=f"{entity} {metric}", metric=metric)
            if verdict.decision != SECURITY_ALLOW:
                return ComparisonResult(
                    metric=metric,
                    period=period_label,
                    refused=True,
                    refusal_message=verdict.message,
                )
        rows: list[ComparisonRow] = []
        for entity in entities:
            hits = self._facts.query(metric, entity, period_type, period, channel)
            fact = hits[0] if hits else None
            rows.append(ComparisonRow(entity=entity, found=fact is not None, fact=fact))
        emit_trace(
            route="graph_peer_comparison",
            metric=metric,
            period=period_label,
            entity_count=len(rows),
            found_count=sum(1 for r in rows if r.found),
        )
        return ComparisonResult(metric=metric, period=period_label, rows=tuple(rows))

    def derivation_trace(
        self,
        start_value: str,
        *,
        edge_types: Sequence[str] = ("derives",),
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> DerivationTrace:
        """从 start_value 沿 derives 边确定性走，返回到达节点链 + 逐边血缘。

        走 GraphStore.subgraph（确定性升序、RESTRICTED 自动剔除）；nodes 为到达链（不含起点），
        edges 携受控 config 血缘，故派生关系（如 entity→geography、metric→派生指标）全程可引用。
        """
        sub = self._store.subgraph([start_value], depth=max_depth, edge_types=list(edge_types))
        nodes = tuple(n for n in sub.nodes if n.id != start_value)
        emit_trace(
            route="graph_derivation_trace",
            start=start_value,
            node_count=len(nodes),
            edge_count=len(sub.edges),
        )
        return DerivationTrace(start=start_value, edges=sub.edges, nodes=nodes)
