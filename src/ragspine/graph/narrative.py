"""W7b 叙事 GraphRAG 骨架（opt-in，默认关，behind [graph]+[llm]）：LLM 抽取 + 社区发现 + 社区摘要。

这是 Microsoft 式 GraphRAG 层：从【叙事文本】用 LLM 抽取实体/关系，再做社区发现 + 社区摘要，
服务全局/主题型查询（"哪些主题在涨""整体格局如何"）。它【编造风险高且非确定】（依赖 LLM），故守
宪章三条铁律，作【纯 opt-in、默认关、血缘纪律】的能力，绝不上默认路径（ADR 0001/0005）：

- **血缘代码强制**：每条抽取出的实体/关系都带 source_doc_id + source_locator，且血缘是【调用方传入】
  的、不取信模型自报——LLMGraphExtractor 用传入值【覆盖戳】每一条，模型即便自报血缘也被忽略。
- **数字留在结构化通道**：本叙事图【绝不】把数字当权威 fact 发出。社区摘要是【明确标注的合成】
  （CommunitySummary.is_synthesis 恒为 True），永不可引为事实；要数字请走结构化通道（W7a / 事实表）。
- **LLM 抽取非确定 → 永不默认**：抽取/摘要走 LLMProvider 缝、可被假 provider 离线测；解析失败 /
  provider 故障 → 确定性降级（空图 / 占位摘要），绝不崩、绝不编造。
- **社区发现【本身确定】**：用确定性连通分量（union-find，非随机化算法），成员/社区均升序——给定
  一份固定的抽取图，社区可逐位复现。

骨架 vs follow-up（诚实边界，刻意不过度建造）：
- 骨架（本模块）：LLMGraphExtractor（提示→JSON→戳血缘→降级，有界）、detect_communities（确定性
  连通分量）、LLMCommunitySummarizer（合成摘要 + 确定性占位降级）、make_narrative_graph 工厂 + env。
- follow-up（文档化，不在此实现）：真正的社区算法（Leiden / Louvain，分层社区）、增量抽取、
  claim 锚定（把合成回挂到具体出处片段）、全局查询编排（map-reduce over 社区摘要）、把抽取实体/关系
  物化进 GraphStore 复用其遍历/隔离。

抽取 LLM 非确定，故仅作 opt-in 适配器（经 make_narrative_graph / RAGSPINE_NARRATIVE_GRAPH 选用，
且必须注入 provider 才生效）；默认 None＝关，默认 answer_question / 检索 / eval 字节不变。
"""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ragspine.agent.llm_provider import LLMProvider, ProviderError

# 叙事图选型读取的环境变量名（缺省 spec 时生效；范式同 RAGSPINE_QUERY_DECOMPOSE）。
NARRATIVE_GRAPH_ENV = "RAGSPINE_NARRATIVE_GRAPH"

# 抽取数量默认上限（有界，防发散 / 防 prompt-injection 撑爆）。
DEFAULT_MAX_ENTITIES = 32
DEFAULT_MAX_RELATIONS = 64

# 类型/关系种类缺省时的回退标签（绝不编造具体语义，只给中性占位）。
_DEFAULT_ENTITY_TYPE = "entity"
_DEFAULT_RELATION_KIND = "related_to"

# 抽取提示：要求模型只输出 {"entities":[...], "relations":[...]} 的 JSON 对象，不要解释。
# 刻意【不】要求模型给血缘——血缘由调用方传入值代码强制覆盖（反编造）。
_EXTRACT_SYSTEM = (
    "你是叙事知识图谱抽取器。从给定文本中抽取实体与关系，只输出一个 JSON 对象，"
    '形如 {"entities":[{"name":"...","type":"..."}],'
    '"relations":[{"source":"...","target":"...","kind":"..."}]}，不要任何解释。'
    "实体名用文本中的原词；无可抽取内容时输出 entities/relations 均为空数组的对象。"
)

# 社区摘要提示：要求主题性综述，明确禁止给出任何具体数字（数字留在结构化通道）。
_SUMMARIZE_SYSTEM = (
    "你是社区主题综述器。给定一组实体及其关系，写一段简短的主题性综述（合成，非事实陈述），"
    "概括它们共同构成的主题。严禁给出任何具体数字、金额、比率——数字属结构化通道，本综述不可作权威引用。"
)

__all__ = [
    "NARRATIVE_GRAPH_ENV",
    "DEFAULT_MAX_ENTITIES",
    "DEFAULT_MAX_RELATIONS",
    "ExtractedEntity",
    "ExtractedRelation",
    "ExtractedGraph",
    "Community",
    "CommunitySummary",
    "GraphExtractor",
    "CommunitySummarizer",
    "LLMGraphExtractor",
    "LLMCommunitySummarizer",
    "NarrativeGraphPipeline",
    "detect_communities",
    "make_narrative_graph",
]


# ---------------------------------------------------------------------------
# 抽取产物（frozen，血缘随身）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExtractedEntity:
    """一个抽取出的实体 + 血缘根。

    name/type 来自模型；source_doc_id/source_locator 由【调用方】戳入（代码强制，非模型自报）。
    """

    name: str
    type: str
    source_doc_id: str
    source_locator: str


@dataclass(frozen=True)
class ExtractedRelation:
    """一条抽取出的关系 source→target（kind 为关系种类）+ 血缘根。

    每条关系都带 source_doc_id + source_locator（血缘是代码强制的不变量，调用方传入值覆盖戳）。
    """

    source: str
    target: str
    kind: str
    source_doc_id: str
    source_locator: str


@dataclass(frozen=True)
class ExtractedGraph:
    """一次抽取的结果：实体集 + 关系集（均不可变，血缘随身）。"""

    entities: tuple[ExtractedEntity, ...]
    relations: tuple[ExtractedRelation, ...]


# 共享的空图（降级返回值；frozen + 空 tuple，复用安全）。
_EMPTY_GRAPH = ExtractedGraph(entities=(), relations=())


@dataclass(frozen=True)
class Community:
    """一个社区（关系无向投影的连通分量）：id + 升序成员 + 内部关系条数（确定性）。"""

    id: str
    member_names: tuple[str, ...]
    relation_count: int


@dataclass(frozen=True)
class CommunitySummary:
    """社区摘要：【明确标注的合成】，永不可引为 fact。

    is_synthesis 恒为 True——综述是合成、非权威事实；正文不含权威数字（数字留在结构化通道）。
    source_doc_ids 携带贡献该社区的来源 doc（升序去重），仅供可溯，不使综述本身可引。
    """

    community_id: str
    text: str
    member_names: tuple[str, ...]
    is_synthesis: bool = True
    source_doc_ids: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 缝：GraphExtractor / CommunitySummarizer 协议
# ---------------------------------------------------------------------------
@runtime_checkable
class GraphExtractor(Protocol):
    """叙事抽取缝：从文本抽出实体/关系，每条带【传入的】血缘。

    实现可为非确定（LLM），故只作 opt-in 注入件——默认不在任何默认路径上。
    """

    def extract(
        self, text: str, *, source_doc_id: str, source_locator: str
    ) -> ExtractedGraph: ...


@runtime_checkable
class CommunitySummarizer(Protocol):
    """社区摘要缝：给社区生成一段【合成】综述（is_synthesis=True，永不可引为 fact）。"""

    def summarize(self, community: Community, graph: ExtractedGraph) -> CommunitySummary: ...


# ---------------------------------------------------------------------------
# LLMGraphExtractor：提示 → JSON → 戳血缘 → 有界 → 确定性降级
# ---------------------------------------------------------------------------
class LLMGraphExtractor:
    """LLM 驱动的叙事实体/关系抽取器（opt-in）。

    单轮调用 provider 让其产出 {"entities":[...],"relations":[...]} 的 JSON；鲁棒解析 + 有界截断 +
    用【传入血缘】代码强制覆盖戳每一条 + 确定性降级：
    - provider 抛 ProviderError（网络/API 故障）→ 空 ExtractedGraph（不崩、不分图、不编造）；
    - 回文非 JSON 对象 / 字段缺失 / 元素不合规 → 跳过该条；整体不合规 → 空图；
    - 实体/关系超上限 → 截断（防发散）。
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_entities: int = DEFAULT_MAX_ENTITIES,
        max_relations: int = DEFAULT_MAX_RELATIONS,
    ) -> None:
        self.provider = provider
        self.max_entities = max(0, max_entities)
        self.max_relations = max(0, max_relations)

    def extract(
        self, text: str, *, source_doc_id: str, source_locator: str
    ) -> ExtractedGraph:
        try:
            resp = self.provider.chat(
                [
                    {"role": "system", "content": _EXTRACT_SYSTEM},
                    {"role": "user", "content": text},
                ]
            )
        except ProviderError:
            return _EMPTY_GRAPH
        out = resp.choices[0].message.content or ""
        return self._parse(out, source_doc_id=source_doc_id, source_locator=source_locator)

    def _parse(
        self, text: str, *, source_doc_id: str, source_locator: str
    ) -> ExtractedGraph:
        """从模型回文鲁棒解析；任何不合规一律降级（空图 / 跳过该条），绝不抛、绝不编造。"""
        try:
            parsed = json.loads(text.strip())
        except (TypeError, ValueError):
            return _EMPTY_GRAPH
        if not isinstance(parsed, dict):
            return _EMPTY_GRAPH
        entities = _parse_entities(
            parsed.get("entities"), source_doc_id, source_locator, self.max_entities
        )
        relations = _parse_relations(
            parsed.get("relations"), source_doc_id, source_locator, self.max_relations
        )
        return ExtractedGraph(entities=entities, relations=relations)


def _str_field(value: Any, default: str = "") -> str:
    """取一个非空字符串字段；非字符串/空白 → default。"""
    return value.strip() if isinstance(value, str) and value.strip() else default


def _parse_entities(
    raw: Any, source_doc_id: str, source_locator: str, cap: int
) -> tuple[ExtractedEntity, ...]:
    """解析 entities 数组：每项需有非空 name；type 缺省回退中性标签；血缘用传入值覆盖戳；有界截断。"""
    if not isinstance(raw, list):
        return ()
    out: list[ExtractedEntity] = []
    for item in raw:
        if len(out) >= cap:
            break
        if not isinstance(item, dict):
            continue
        name = _str_field(item.get("name"))
        if not name:
            continue
        out.append(
            ExtractedEntity(
                name=name,
                type=_str_field(item.get("type"), _DEFAULT_ENTITY_TYPE),
                source_doc_id=source_doc_id,
                source_locator=source_locator,
            )
        )
    return tuple(out)


def _parse_relations(
    raw: Any, source_doc_id: str, source_locator: str, cap: int
) -> tuple[ExtractedRelation, ...]:
    """解析 relations 数组：每项需有非空 source+target；kind 缺省回退；血缘用传入值覆盖戳；有界截断。"""
    if not isinstance(raw, list):
        return ()
    out: list[ExtractedRelation] = []
    for item in raw:
        if len(out) >= cap:
            break
        if not isinstance(item, dict):
            continue
        source = _str_field(item.get("source"))
        target = _str_field(item.get("target"))
        if not source or not target:
            continue
        out.append(
            ExtractedRelation(
                source=source,
                target=target,
                kind=_str_field(item.get("kind"), _DEFAULT_RELATION_KIND),
                source_doc_id=source_doc_id,
                source_locator=source_locator,
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# 确定性社区发现：关系无向投影的连通分量（union-find）
# ---------------------------------------------------------------------------
def _uf_find(parent: dict[str, str], x: str) -> str:
    """带路径压缩的 find（迭代，确定性）。"""
    root = x
    while parent[root] != root:
        root = parent[root]
    while parent[x] != root:
        parent[x], x = root, parent[x]
    return root


def _uf_union(parent: dict[str, str], a: str, b: str) -> None:
    """合并 a、b 所在分量；以字典序较小者为根（确定性，与合并顺序无关）。"""
    ra, rb = _uf_find(parent, a), _uf_find(parent, b)
    if ra == rb:
        return
    hi, lo = (ra, rb) if ra > rb else (rb, ra)
    parent[hi] = lo


def detect_communities(graph: ExtractedGraph) -> tuple[Community, ...]:
    """确定性社区发现：对关系无向投影做连通分量（union-find），成员/社区均升序。

    节点取自关系端点（无关系的孤立实体不构成社区——社区是"相关实体的簇"）。给定固定抽取图，
    结果逐位可复现（id 按社区成员元组升序编号 c0/c1/…，与合并顺序无关）。relation_count 为该
    分量内部关系条数。零三方依赖、零随机。真正的分层社区算法（Leiden/Louvain）是 follow-up。
    """
    parent: dict[str, str] = {}
    for rel in graph.relations:
        for name in (rel.source, rel.target):
            if name not in parent:
                parent[name] = name
        _uf_union(parent, rel.source, rel.target)

    groups: dict[str, set[str]] = {}
    for name in parent:
        groups.setdefault(_uf_find(parent, name), set()).add(name)

    rel_counts: dict[str, int] = {}
    for rel in graph.relations:
        root = _uf_find(parent, rel.source)
        rel_counts[root] = rel_counts.get(root, 0) + 1

    ordered = sorted(
        (tuple(sorted(members)), rel_counts.get(root, 0))
        for root, members in groups.items()
    )
    return tuple(
        Community(id=f"c{i}", member_names=members, relation_count=count)
        for i, (members, count) in enumerate(ordered)
    )


# ---------------------------------------------------------------------------
# LLMCommunitySummarizer：合成摘要（is_synthesis=True）+ 确定性占位降级
# ---------------------------------------------------------------------------
def _community_source_doc_ids(community: Community, graph: ExtractedGraph) -> tuple[str, ...]:
    """该社区内部关系贡献的 source_doc_id（升序去重）；社区是连通分量，按 source 端筛即覆盖内部边。"""
    members = set(community.member_names)
    ids = {
        rel.source_doc_id
        for rel in graph.relations
        if rel.source in members and rel.source_doc_id
    }
    return tuple(sorted(ids))


def _summarize_user_prompt(community: Community, graph: ExtractedGraph) -> str:
    """构造摘要 user 提示：列出成员与内部关系（确定性升序），供模型生成主题综述。"""
    members = set(community.member_names)
    lines = sorted(
        f"{r.source} --{r.kind}--> {r.target}"
        for r in graph.relations
        if r.source in members
    )
    rel_block = "\n".join(lines) if lines else "（无显式关系）"
    return f"社区成员：{'、'.join(community.member_names)}\n关系：\n{rel_block}"


def _placeholder_summary(
    community: Community, source_doc_ids: tuple[str, ...]
) -> CommunitySummary:
    """provider 不可用时的确定性占位摘要：仍是合成、仍带血缘、绝不含数字。"""
    members = "、".join(community.member_names)
    text = (
        f"[合成摘要·占位] 社区 {community.id} 含实体：{members}"
        "（provider 不可用，确定性占位；非可引事实，不含数字）"
    )
    return CommunitySummary(
        community_id=community.id,
        text=text,
        member_names=community.member_names,
        is_synthesis=True,
        source_doc_ids=source_doc_ids,
    )


class LLMCommunitySummarizer:
    """LLM 驱动的社区摘要器（opt-in）：产出【明确标注的合成】综述，永不可引为 fact。

    单轮调用 provider 让其产出主题综述文本；is_synthesis 恒 True，携带社区贡献 source_doc_ids 供可溯。
    确定性降级：provider 故障 / 空回文 → 确定性占位摘要（仍 is_synthesis=True，绝不编造数字）。
    """

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def summarize(self, community: Community, graph: ExtractedGraph) -> CommunitySummary:
        source_doc_ids = _community_source_doc_ids(community, graph)
        try:
            resp = self.provider.chat(
                [
                    {"role": "system", "content": _SUMMARIZE_SYSTEM},
                    {"role": "user", "content": _summarize_user_prompt(community, graph)},
                ]
            )
        except ProviderError:
            return _placeholder_summary(community, source_doc_ids)
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return _placeholder_summary(community, source_doc_ids)
        return CommunitySummary(
            community_id=community.id,
            text=text,
            member_names=community.member_names,
            is_synthesis=True,
            source_doc_ids=source_doc_ids,
        )


# ---------------------------------------------------------------------------
# 编排件 + 工厂（默认关）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NarrativeGraphPipeline:
    """叙事 GraphRAG 编排件（opt-in）：捆绑抽取器 + 摘要器 + 确定性社区发现。

    detect_communities 委派给模块级确定性函数（无状态、纯函数）；extractor/summarizer 共享同一 provider。
    """

    extractor: GraphExtractor
    summarizer: CommunitySummarizer

    def detect_communities(self, graph: ExtractedGraph) -> tuple[Community, ...]:
        """确定性社区发现（委派模块级 detect_communities）。"""
        return detect_communities(graph)


def make_narrative_graph(
    spec: str | None = None, *, provider: LLMProvider | None = None, **kwargs: Any
) -> NarrativeGraphPipeline | None:
    """叙事图选型工厂：默认 None＝关（绝不上默认路径），LLM 叙事图 opt-in。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_NARRATIVE_GRAPH）：
        - None / 'none'  -> None（默认关；不构建任何叙事图，answer_question/检索/eval 字节不变）
        - 'llm' / 'on'   -> 注入了 provider 则 NarrativeGraphPipeline；未注入 provider 则 None
                            （"注入 provider 才生效"——诚实降级为关，绝不空跑）
        - 其他           -> ValueError（列清可用 spec）

    其余 kwargs（max_entities / max_relations）透传给 LLMGraphExtractor。
    """
    if spec is None:
        spec = os.environ.get(NARRATIVE_GRAPH_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "none":
        return None
    if normalized in {"llm", "on"}:
        if provider is None:
            return None
        return NarrativeGraphPipeline(
            extractor=LLMGraphExtractor(provider, **kwargs),
            summarizer=LLMCommunitySummarizer(provider),
        )
    raise ValueError(
        f"未知 narrative-graph spec：{normalized!r}（可选 none / llm / on；llm 需注入 provider）"
    )
