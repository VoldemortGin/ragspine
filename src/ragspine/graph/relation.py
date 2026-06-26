"""W7a 结构关系图建图器：从【受控维度】确定性建图（零 LLM、零编造、全程可引用）。

在【已有受控维度】（DomainProfile 声明的实体 / 指标 / 派生维 / 竞品清单 + 确定性抽取的
Fact + 切块 chunk）之上，确定性地建出一张类型化关系图——解锁 flat top-k + 精确 SQL 做不到
的多跳结构化查询（母子汇总 / 派生追溯 / 同业对比 / doc↔实体·指标共现）。

建图是【纯确定性】的（按 id 升序单遍构造、同输入同输出），且把两项脊柱不变量绑死在代码里：
- Provenance —— 每个节点/边都带【非空】source_doc_id + source_locator。profile 派生的
  节点/边记受控 config 血缘（source_doc_id="company_profile" + 描述性 locator）；事实/块
  共现边记事实/块自身血缘。血缘是代码强制的不变量，不是注释。
- Isolation —— doc 节点的 metadata["sensitivity"] 取自 chunk 敏感度（事实无敏感度 →
  INTERNAL；同一 doc 多来源时最严者胜），于是 RESTRICTED chunk 的 doc 节点被存储层自动
  从一切读/遍历里剔除——本层不另写隔离逻辑，直接继承 GraphStore 的「RESTRICTED 新出口」。

节点/边类型与建图来源（均来自注入/载入的 profile，绝不硬编码任何公司）：
    entity          —— 各 entity_code（home_entity_synonyms.values() ∪ entity_geography
                       ∪ home_entity_labels 的并集）。
    metric          —— 名为 "metric" 的维度 synonyms.values()。
    external_entity —— external_entities.values()（竞品展示名）。
    <派生维名>      —— 任何带 derived_from + derivation 的维度，其 derivation 取值
                       （如 geography / 派生指标）；通用机制，不特判 geography。
    doc             —— 各 fact.source_doc_id 与 chunk.doc_id。
边：parent_of（home → 其余实体）/ derives（派生维 src→dst）/ competes_with（home → 竞品）/
    mentions（doc → fact 的实体·指标；doc → chunk 的实体）。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from ragspine.common.company_profile import DimensionSpec, DomainProfile
from ragspine.graph.store import GraphEdge, GraphNode, GraphStore, InProcessGraphStore
from ragspine.storage.fact_store import Fact

# profile 派生节点/边的受控 config 血缘根（非空、稳定、可审计）。
_PROFILE_DOC_ID = "company_profile"

# 敏感度严格度排序（doc 节点同 id 多来源时取最严者；未知级别按 INTERNAL 计）。
_SENSITIVITY_RANK = {"PUBLIC": 0, "INTERNAL": 1, "CONFIDENTIAL": 2, "RESTRICTED": 3}
_DEFAULT_SENSITIVITY = "INTERNAL"

__all__ = ["build_relation_graph"]


def _node_meta(source_locator: str, *, source_doc_id: str = _PROFILE_DOC_ID,
               sensitivity: str = _DEFAULT_SENSITIVITY) -> dict[str, str]:
    """节点血缘 + 隔离判据元数据（三键皆非空）。"""
    return {
        "source_doc_id": source_doc_id,
        "source_locator": source_locator,
        "sensitivity": sensitivity,
    }


def _edge_meta(source_locator: str, *, source_doc_id: str = _PROFILE_DOC_ID) -> dict[str, str]:
    """边血缘元数据（两键皆非空）。"""
    return {"source_doc_id": source_doc_id, "source_locator": source_locator}


def _dimension(profile: DomainProfile, name: str) -> DimensionSpec | None:
    """取名为 name 的声明维度（无则 None）。"""
    for dim in profile.dimensions:
        if dim.name == name:
            return dim
    return None


def _derivation_dims(profile: DomainProfile) -> list[DimensionSpec]:
    """所有带 derived_from + derivation 的维度（派生链的通用来源），按维名升序。"""
    dims = [d for d in profile.dimensions if d.derived_from and d.derivation]
    return sorted(dims, key=lambda d: d.name)


def _entity_codes(profile: DomainProfile) -> list[str]:
    """全部 distinct entity_code（同义词值 ∪ 地理键 ∪ 展示名键 ∪ home），升序。"""
    codes = set(profile.home_entity_synonyms.values())
    codes |= set(profile.entity_geography)
    codes |= set(profile.home_entity_labels)
    codes.add(profile.home_entity_code)
    return sorted(codes)


def _rank(sensitivity: str) -> int:
    return _SENSITIVITY_RANK.get(sensitivity.upper(), _SENSITIVITY_RANK[_DEFAULT_SENSITIVITY])


def _doc_sensitivities(
    facts: Iterable[Fact], chunks: Iterable[object]
) -> dict[str, str]:
    """每个 doc_id 的敏感度：事实来源 → INTERNAL，chunk 来源 → chunk.sensitivity；
    同 doc 多来源取【最严】（按 (rank, 串) 比较，与来源遍历顺序无关，确定性）。"""
    sens: dict[str, str] = {}

    def consider(doc_id: str, level: str) -> None:
        level = level or _DEFAULT_SENSITIVITY
        current = sens.get(doc_id)
        if current is None or (_rank(level), level) > (_rank(current), current):
            sens[doc_id] = level

    for fact in facts:
        consider(str(fact.source_doc_id), _DEFAULT_SENSITIVITY)
    for chunk in chunks:
        doc_id = str(getattr(chunk, "doc_id", ""))
        if not doc_id:
            continue
        consider(doc_id, str(getattr(chunk, "sensitivity", _DEFAULT_SENSITIVITY)))
    return sens


@dataclass(frozen=True)
class _Build:
    """建图过程的可变累加器：节点按 id、边按 (src,dst,type) 去重（最后写入者胜）。"""

    nodes: dict[str, GraphNode]
    edges: dict[tuple[str, str, str], GraphEdge]


def _add_profile_nodes(profile: DomainProfile, build: _Build) -> None:
    """实体 / 指标 / 外部主体 / 派生目标节点（均带受控 config 血缘）。"""
    for code in _entity_codes(profile):
        build.nodes[code] = GraphNode(
            id=code,
            type="entity",
            label=profile.home_entity_labels.get(code, ""),
            metadata=_node_meta(f"company_profile#home_entity/{code}"),
        )
    metric_dim = _dimension(profile, "metric")
    if metric_dim is not None:
        for code in sorted(set(metric_dim.synonyms.values())):
            build.nodes[code] = GraphNode(
                id=code,
                type="metric",
                label=metric_dim.labels.get(code, ""),
                metadata=_node_meta(f"company_profile#metric/{code}"),
            )
    for name in sorted(set(profile.external_entities.values())):
        build.nodes[name] = GraphNode(
            id=name,
            type="external_entity",
            label=name,
            metadata=_node_meta(f"company_profile#external_entities/{name}"),
        )
    # 派生目标节点：仅在尚未存在时创建（不覆盖既有实体/指标节点），类型 = 派生维名。
    for dim in _derivation_dims(profile):
        for value in sorted(set(dim.derivation.values())):
            if value in build.nodes:
                continue
            build.nodes[value] = GraphNode(
                id=value,
                type=dim.name,
                label=dim.labels.get(value, ""),
                metadata=_node_meta(f"company_profile#derivation/{dim.name}/{value}"),
            )


def _add_doc_nodes(facts: Iterable[Fact], chunks: Iterable[object], build: _Build) -> None:
    """doc 节点：id = doc_id，sensitivity 取自来源（最严者胜），血缘锚到 doc 自身。"""
    for doc_id, sensitivity in sorted(_doc_sensitivities(facts, chunks).items()):
        build.nodes[doc_id] = GraphNode(
            id=doc_id,
            type="doc",
            label=doc_id,
            metadata=_node_meta(
                f"{doc_id}#doc", source_doc_id=doc_id, sensitivity=sensitivity
            ),
        )


def _add_profile_edges(profile: DomainProfile, build: _Build) -> None:
    """parent_of（home→其余实体）/ derives（派生维 src→dst）/ competes_with（home→竞品）。"""
    home = profile.home_entity_code
    for code in _entity_codes(profile):
        if code == home:
            continue
        build.edges[(home, code, "parent_of")] = GraphEdge(
            src=home,
            dst=code,
            type="parent_of",
            metadata=_edge_meta(f"company_profile#parent_of/{home}->{code}"),
        )
    for dim in _derivation_dims(profile):
        for src in sorted(dim.derivation):
            dst = dim.derivation[src]
            build.edges[(src, dst, "derives")] = GraphEdge(
                src=src,
                dst=dst,
                type="derives",
                metadata=_edge_meta(f"company_profile#derivation/{dim.name}/{src}->{dst}"),
            )
    for name in sorted(set(profile.external_entities.values())):
        build.edges[(home, name, "competes_with")] = GraphEdge(
            src=home,
            dst=name,
            type="competes_with",
            metadata=_edge_meta(f"company_profile#external_entities/{home}->{name}"),
        )


def _fact_sort_key(fact: Fact) -> tuple[str, str, str, str, str, str]:
    return (
        str(fact.source_doc_id),
        str(fact.metric_code),
        str(fact.entity),
        str(fact.period_type),
        str(fact.period),
        str(fact.source_locator),
    )


def _add_fact_mentions(facts: Iterable[Fact], build: _Build) -> None:
    """每条事实：doc → 实体、doc → 指标，边带事实自身血缘（按事实排序，最后写入者胜）。"""
    for fact in sorted(facts, key=_fact_sort_key):
        doc_id = str(fact.source_doc_id)
        meta = _edge_meta(str(fact.source_locator), source_doc_id=doc_id)
        for dst in (str(fact.entity), str(fact.metric_code)):
            build.edges[(doc_id, dst, "mentions")] = GraphEdge(
                src=doc_id, dst=dst, type="mentions", metadata=dict(meta)
            )


def _chunk_sort_key(chunk: object) -> tuple[str, str, str]:
    return (
        str(getattr(chunk, "doc_id", "")),
        str(getattr(chunk, "entity", "")),
        str(getattr(chunk, "source_locator", "")),
    )


def _add_chunk_mentions(chunks: Iterable[object], build: _Build) -> None:
    """每个 chunk（entity 非空）：doc → 实体共现边，带 chunk 自身血缘。"""
    for chunk in sorted(chunks, key=_chunk_sort_key):
        doc_id = str(getattr(chunk, "doc_id", ""))
        entity = str(getattr(chunk, "entity", ""))
        if not doc_id or not entity:
            continue
        locator = str(getattr(chunk, "source_locator", "")) or f"{doc_id}#chunk"
        build.edges[(doc_id, entity, "mentions")] = GraphEdge(
            src=doc_id,
            dst=entity,
            type="mentions",
            metadata=_edge_meta(locator, source_doc_id=doc_id),
        )


def build_relation_graph(
    profile: DomainProfile,
    *,
    facts: Iterable[Fact] = (),
    chunks: Iterable[object] = (),
    store: GraphStore | None = None,
) -> GraphStore:
    """从受控维度（profile + facts + chunks）确定性地建出结构关系图，返回 store。

    store 为 None 时新建零依赖内存默认 InProcessGraphStore。建图按 id / (src,dst,type) 升序
    单遍构造、幂等（upsert 替换），同输入同输出。每个节点/边带非空血缘；doc 节点的敏感度取自
    chunk（最严者胜），于是 RESTRICTED chunk 的 doc 节点被存储层自动隔离、绝不出域。
    """
    facts = list(facts)
    chunks = list(chunks)
    if store is None:
        store = InProcessGraphStore()
    build = _Build(nodes={}, edges={})
    _add_profile_nodes(profile, build)
    _add_doc_nodes(facts, chunks, build)
    _add_profile_edges(profile, build)
    _add_fact_mentions(facts, build)
    _add_chunk_mentions(chunks, build)
    store.upsert_nodes([build.nodes[nid] for nid in sorted(build.nodes)])
    store.upsert_edges([build.edges[key] for key in sorted(build.edges)])
    return store
