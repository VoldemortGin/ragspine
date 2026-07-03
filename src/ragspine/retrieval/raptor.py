"""RAPTOR 递归聚类+摘要树（W10，opt-in 默认关）：全局综合的第二条路线（平行于 W7b 叙事 GraphRAG）。

对标 LlamaIndex RAPTOR pack / RAGFlow RAPTOR（Sarthi et al. 2024，Recursive Abstractive Processing
for Tree-Organized Retrieval）：底层是 chunk，逐层【对簇内节点向量聚类】+ 每簇摘要 → 上层节点，
得到一棵从细节（叶片 chunk）到主题（高层摘要）的多粒度树；检索时既能命中细节叶片、也能命中高层
摘要，补上 flat top-k 做不到的全局 / 多跳综合。

契合宪章的关键约束（与 W7b CommunitySummarizer 同的反编造纪律）：
- **聚类【确定性】默认**：确定性阈值聚类——余弦相似度 ≥ 阈值建边 + 连通分量（union-find，零随机），
  同输入同树、逐位可复现。刻意【不】用带随机性的 UMAP/GMM 作默认（那作 opt-in 适配器是 follow-up）。
- **摘要是【合成】、绝不可引为 fact**：每个上层节点 is_synthesis=True，明确标注；绝不进结构化数字通路、
  绝不当 citable fact——数字仍归结构化通道（W7a / 事实表）。LLM 摘要提示明确禁止给出具体数字。
- **每节点带 provenance**：合成节点记录它综合了哪些底层 chunk（source_doc_id/locator 并集，血缘可溯），
  且血缘 ⊆ 成员叶片血缘并集，绝不编造出处。
- **LLM 摘要 opt-in、默认走确定性 extractive**：behind [llm]；provider 故障 / 空回文 → 确定性降级到
  extractive 摘要（仍 is_synthesis=True，绝不崩、绝不编造）。聚类本身零 LLM、离线可跑。
- **isolation 继承**：sensitivity==RESTRICTED 的 chunk 绝不进树/摘要（建树入口即剔除，继承既有隔离）。

opt-in / 默认关（ADR 0001/0005）：build_raptor_tree / RaptorTree.retrieve 是【独立的新能力】（范式同
W7a GraphQuery——不接入 answer_question 默认路径）；RaptorRetriever 是可选的 NarrativeRetriever 包装件，
make_raptor_retriever(base, 'none') 返回 base 本身（字节不变，默认）。默认 answer_question / 检索 / eval
字节不变——本模块全是新增 opt-in 能力，不改任何默认路径文件。

follow-up（诚实边界，见 docs/prd-quality-depth.md W10）：collapsed-tree vs tree-traversal 检索模式的
A/B、增量建树、UMAP/GMM 聚类适配器、把合成节点安全地接入 answer_question 引用抑制通路、全局综合金标 A/B。
"""

import math
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ragspine.agent.llm_provider import LLMProvider, ProviderError
from ragspine.retrieval.chunking.chunking import _SENTENCE_RE, Chunk
from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY

# 选型读取的环境变量名（缺省 spec 时生效；范式同 RAGSPINE_NARRATIVE_GRAPH / RAGSPINE_CORRECTIVE）。
RAPTOR_ENV = "RAGSPINE_RAPTOR"
RAPTOR_SUMMARIZER_ENV = "RAGSPINE_RAPTOR_SUMMARIZER"

# 建树默认参数。
DEFAULT_SIMILARITY_THRESHOLD = 0.5  # 余弦 ≥ 此值即建边（同簇）
DEFAULT_MAX_LEVELS = 3              # 最多向上递归层数（有界）
DEFAULT_MIN_CLUSTER_SIZE = 2       # 小于此的簇不生成摘要节点（单例上带）
DEFAULT_SUMMARY_TOP_K = 5          # RaptorRetriever 追加的合成节点数上限
_EXTRACT_EXCERPT_CHARS = 240       # extractive 摘要每成员摘录上限

# 摘要提示（同 W7b：主题综述、明确禁止具体数字——数字属结构化通道）。
_SUMMARIZE_SYSTEM = (
    "你是主题综述器。给定同一簇的若干文本片段，写一段简短的主题性综述（合成，非事实陈述），"
    "概括它们共同的主题。严禁给出任何具体数字、金额、比率——数字属结构化通道，本综述不可作权威引用。"
)

# sensitivity 由弱到强的秩（合成节点取成员中最严格者；RESTRICTED 建树时已剔除，此处为诚实兜底）。
_SENSITIVITY_RANK = {"PUBLIC": 0, "INTERNAL": 1, "CONFIDENTIAL": 2, "RESTRICTED": 3}

__all__ = [
    "RAPTOR_ENV",
    "RAPTOR_SUMMARIZER_ENV",
    "DEFAULT_SIMILARITY_THRESHOLD",
    "DEFAULT_MAX_LEVELS",
    "DEFAULT_MIN_CLUSTER_SIZE",
    "RESTRICTED_SENSITIVITY",
    "RaptorNode",
    "RaptorHit",
    "RaptorTree",
    "RaptorSummarizer",
    "ExtractiveRaptorSummarizer",
    "LLMRaptorSummarizer",
    "NarrativeRetriever",
    "RaptorRetriever",
    "cluster_by_similarity",
    "build_raptor_tree",
    "make_raptor_summarizer",
    "make_raptor_retriever",
]


# ---------------------------------------------------------------------------
# 缝：embedding 后端 / 摘要器 / 被包装的检索器（均 duck-typed，结构最小接口）
# ---------------------------------------------------------------------------
@runtime_checkable
class _Embedder(Protocol):
    """embedding 后端最小结构接口（同 lexical.retrieval.EmbeddingBackend）。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class RaptorSummarizer(Protocol):
    """簇摘要缝：给一组成员文本生成一段【合成】综述文本（is_synthesis 标注由建树侧统一置位）。"""

    def summarize(self, texts: Sequence[str]) -> str: ...


@runtime_checkable
class NarrativeRetriever(Protocol):
    """叙事检索协议（duck-typed，结构等同 agent.NarrativeRetriever）：本层既包裹它、也实现它。"""

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]: ...


# ---------------------------------------------------------------------------
# 树的数据结构（frozen，血缘随身）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RaptorNode:
    """RAPTOR 树的一个节点：叶片（level 0，is_synthesis=False）或合成摘要（level ≥ 1，is_synthesis=True）。

    叶片：doc_id/source_locator 为该 chunk 的单点血缘；source_doc_ids/source_locators 为其单元素并集。
    合成节点：doc_id/source_locator 为空（合成无单一出处），血缘在 source_doc_ids/source_locators
    （成员叶片血缘的升序并集，⊆ 叶片血缘，绝不编造）；member_ids 为其直接子节点 id。
    """

    node_id: str
    level: int
    text: str
    is_synthesis: bool
    doc_id: str
    source_locator: str
    member_ids: tuple[str, ...]
    source_doc_ids: tuple[str, ...]
    source_locators: tuple[str, ...]
    sensitivity: str
    embedding: tuple[float, ...] = ()


@dataclass(frozen=True)
class RaptorHit:
    """一次检索命中：节点 + 相对查询的余弦分（is_synthesis 见 node）。"""

    node: RaptorNode
    score: float


@dataclass(frozen=True)
class RaptorTree:
    """RAPTOR 多粒度树：所有节点（叶片 + 合成层），支持 collapsed-tree 多粒度检索。"""

    nodes: tuple[RaptorNode, ...] = ()

    @property
    def leaves(self) -> list[RaptorNode]:
        return [n for n in self.nodes if not n.is_synthesis]

    @property
    def summaries(self) -> list[RaptorNode]:
        return [n for n in self.nodes if n.is_synthesis]

    @property
    def max_level(self) -> int:
        return max((n.level for n in self.nodes), default=0)

    def retrieve(
        self,
        query: str,
        embedder: _Embedder,
        *,
        top_k: int = 10,
        granularity: str = "all",
    ) -> tuple[RaptorHit, ...]:
        """collapsed-tree 多粒度检索：对（按 granularity 选定的）节点按查询余弦打分取 top_k。

        granularity: 'all'（叶片+摘要，默认）/ 'leaves'（仅细节）/ 'summaries'（仅高层主题）。
        打分降序、同分按 node_id 升序 → 确定性。空 embedding 的节点（无 embedder 降级树）被跳过。
        """
        q = embedder.embed_texts([query])[0]
        if granularity == "summaries":
            candidates: list[RaptorNode] = self.summaries
        elif granularity == "leaves":
            candidates = self.leaves
        else:
            candidates = list(self.nodes)
        hits = [
            RaptorHit(node=n, score=_cosine(q, list(n.embedding)))
            for n in candidates
            if n.embedding
        ]
        hits.sort(key=lambda h: (-h.score, h.node.node_id))
        return tuple(hits[:top_k])


# ---------------------------------------------------------------------------
# 摘要器：extractive 默认（确定性、零 LLM） + LLM opt-in（degrade 到 extractive）
# ---------------------------------------------------------------------------
def _leading_excerpt(text: str, max_chars: int = _EXTRACT_EXCERPT_CHARS) -> str:
    """一段文本的确定性代表摘录：取首句（无句末标点则取整段），并截断到 max_chars。"""
    stripped = text.strip()
    if not stripped:
        return ""
    match = _SENTENCE_RE.search(stripped)
    excerpt = match.group(0).strip() if match else stripped
    return excerpt[:max_chars]


@dataclass
class ExtractiveRaptorSummarizer:
    """确定性 extractive 簇摘要器（零 LLM、离线默认）：取每个成员的代表摘录并升序无关地按输入序拼接。

    诚实：这是【摘录式合成】——文本取自成员原句子片段，节点仍 is_synthesis=True、绝不可引为 fact
    （数字仍归结构化通道）。确定性：同输入两次逐字一致。让 determinism-only 部署也拿到多粒度树。
    """

    max_chars: int = _EXTRACT_EXCERPT_CHARS

    def summarize(self, texts: Sequence[str]) -> str:
        parts = [ex for t in texts if (ex := _leading_excerpt(t, self.max_chars))]
        return " ".join(parts)


class LLMRaptorSummarizer:
    """LLM 驱动的簇摘要器（opt-in，behind [llm]）：产出主题综述文本，degrade 到确定性 extractive。

    单轮调用 provider（系统提示明确禁止具体数字，同 W7b）；provider 故障 / 空回文 → 回落注入的
    fallback（默认 ExtractiveRaptorSummarizer）——绝不崩、绝不编造。is_synthesis 标注由建树侧统一置位。
    """

    def __init__(self, provider: LLMProvider, *, fallback: RaptorSummarizer | None = None):
        self.provider = provider
        self.fallback: RaptorSummarizer = fallback or ExtractiveRaptorSummarizer()

    def summarize(self, texts: Sequence[str]) -> str:
        try:
            resp = self.provider.chat(
                [
                    {"role": "system", "content": _SUMMARIZE_SYSTEM},
                    {"role": "user", "content": "\n\n".join(texts)},
                ]
            )
        except ProviderError:
            return self.fallback.summarize(texts)
        text = (resp.choices[0].message.content or "").strip()
        return text if text else self.fallback.summarize(texts)


# ---------------------------------------------------------------------------
# 确定性聚类：余弦阈值图的连通分量（union-find，零随机）
# ---------------------------------------------------------------------------
def _cosine(a: list[float], b: list[float]) -> float:
    """两向量余弦（含零范数守护：任一为零向量 -> 0.0，不抛）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _uf_find(parent: dict[str, str], x: str) -> str:
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


def cluster_by_similarity(
    ids: Sequence[str],
    embeddings: Sequence[Sequence[float]],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> tuple[tuple[str, ...], ...]:
    """确定性阈值聚类：余弦 ≥ threshold 建边，取连通分量。成员升序、簇整体升序 → 逐位可复现。

    每个 id 都归入某簇（含孤立点自成单例簇）。零随机、零三方依赖——同输入同结果。
    """
    parent: dict[str, str] = {i: i for i in ids}
    n = len(ids)
    for a in range(n):
        for b in range(a + 1, n):
            if _cosine(list(embeddings[a]), list(embeddings[b])) >= threshold:
                _uf_union(parent, ids[a], ids[b])
    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(_uf_find(parent, i), []).append(i)
    ordered = sorted(tuple(sorted(members)) for members in groups.values())
    return tuple(ordered)


# ---------------------------------------------------------------------------
# 建树：叶片 → 逐层聚类 + 每簇摘要 → 多粒度树（isolation 入口剔除 RESTRICTED）
# ---------------------------------------------------------------------------
def _most_restrictive(sensitivities: Iterable[str]) -> str:
    items = list(sensitivities)
    if not items:
        return "INTERNAL"
    return max(items, key=lambda s: _SENSITIVITY_RANK.get(str(s).upper(), 1))


def build_raptor_tree(
    chunks: Sequence[Chunk],
    *,
    embedder: _Embedder | None,
    summarizer: RaptorSummarizer | None = None,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    max_levels: int = DEFAULT_MAX_LEVELS,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
) -> RaptorTree:
    """从 chunk 建 RAPTOR 多粒度树：叶片 → 逐层确定性聚类 + 每簇合成摘要 → 上层节点。

    - isolation：RESTRICTED chunk 建树入口即剔除（绝不进树/摘要）。
    - embedder is None → 只有叶片层（无法聚类，诚实降级、不建摘要层）。
    - summarizer 缺省 = ExtractiveRaptorSummarizer（确定性、离线）；LLM 摘要经注入 opt-in。
    - 合成节点 is_synthesis=True、带成员血缘并集（⊆ 叶片血缘），成员升序 → 逐位可复现。
    - 有界：至多 max_levels 层；某层无可合并（全单例）或规模不再缩小即停（绝不无限递归）。
    """
    summarizer = summarizer or ExtractiveRaptorSummarizer()
    visible = [c for c in chunks if str(c.sensitivity).upper() != RESTRICTED_SENSITIVITY]
    if not visible:
        return RaptorTree(nodes=())

    leaf_vectors = embedder.embed_texts([c.text for c in visible]) if embedder is not None else None
    nodes: list[RaptorNode] = []
    current: list[RaptorNode] = []
    for i, c in enumerate(visible):
        emb = tuple(leaf_vectors[i]) if leaf_vectors is not None else ()
        leaf = RaptorNode(
            node_id=f"L0#{c.chunk_id}",
            level=0,
            text=c.text,
            is_synthesis=False,
            doc_id=c.doc_id,
            source_locator=c.source_locator,
            member_ids=(),
            source_doc_ids=(c.doc_id,),
            source_locators=(c.source_locator,),
            sensitivity=c.sensitivity,
            embedding=emb,
        )
        nodes.append(leaf)
        current.append(leaf)

    if embedder is None:
        return RaptorTree(nodes=tuple(nodes))  # 只有叶片层（诚实降级）

    for level in range(1, max_levels + 1):
        if len(current) <= 1:
            break
        by_id = {n.node_id: n for n in current}
        clusters = cluster_by_similarity(
            [n.node_id for n in current],
            [list(n.embedding) for n in current],
            threshold=threshold,
        )
        if all(len(cl) < min_cluster_size for cl in clusters):
            break  # 无可合并（全单例）→ 停
        next_level: list[RaptorNode] = []
        summary_index = 0
        for cluster in clusters:
            members = [by_id[cid] for cid in cluster]
            if len(cluster) < min_cluster_size:
                next_level.append(members[0])  # 单例上带（可在更高层再聚）
                continue
            summary_text = summarizer.summarize([m.text for m in members])
            summary_emb = tuple(embedder.embed_texts([summary_text])[0])
            node = RaptorNode(
                node_id=f"L{level}#c{summary_index}",
                level=level,
                text=summary_text,
                is_synthesis=True,
                doc_id="",
                source_locator="",
                member_ids=tuple(cluster),
                source_doc_ids=tuple(sorted({d for m in members for d in m.source_doc_ids})),
                source_locators=tuple(sorted({loc for m in members for loc in m.source_locators})),
                sensitivity=_most_restrictive(m.sensitivity for m in members),
                embedding=summary_emb,
            )
            nodes.append(node)
            next_level.append(node)
            summary_index += 1
        if len(next_level) >= len(current):
            break  # 规模不再缩小 → 停（安全网，防无限递归）
        current = next_level

    return RaptorTree(nodes=tuple(nodes))


# ---------------------------------------------------------------------------
# 检索接入：opt-in NarrativeRetriever 包装件（默认关，字节不变）
# ---------------------------------------------------------------------------
def _summary_to_snippet(hit: RaptorHit) -> dict[str, object]:
    """合成节点 -> 追加 snippet：【明确标注 is_synthesis=True】，血缘为成员叶片并集（诚实、非编造）。

    is_synthesis 让下游可区分「合成主题上下文」与「可引叶片 fact」；数字仍归结构化通道。
    """
    node = hit.node
    return {
        "text": node.text,
        "is_synthesis": True,
        "doc_id": ",".join(node.source_doc_ids),
        "source_locator": ",".join(node.source_locators),
        "sensitivity": node.sensitivity,
        "raptor_level": node.level,
        "scores": {"raptor": hit.score},
    }


class RaptorRetriever:
    """RAPTOR 多粒度检索包装件（实现 NarrativeRetriever）：base 叶片（可引）+ 追加高层合成节点。

    - 叶片来自 base.retrieve（已在 link/ 出口剔除 RESTRICTED，隔离继承）；
    - 追加的合成节点来自树（建树入口已剔除 RESTRICTED，故永不含 RESTRICTED 血缘），明确标注
      is_synthesis=True——补全局/主题上下文，绝不当 citable fact。
    - 默认关：经 make_raptor_retriever('none') 时根本不构造本类（返回 base，字节不变）。
    """

    def __init__(
        self,
        base: NarrativeRetriever,
        tree: RaptorTree,
        *,
        embedder: _Embedder,
        summary_top_k: int = DEFAULT_SUMMARY_TOP_K,
    ):
        self.base = base
        self.tree = tree
        self.embedder = embedder
        self.summary_top_k = max(0, summary_top_k)

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]:
        """base 叶片在前（可引、检索不丢），高层合成节点作 is_synthesis snippet 追加在后。"""
        leaves = list(self.base.retrieve(query, filters=filters, top_k=top_k))
        if self.summary_top_k == 0:
            return leaves
        summary_hits = self.tree.retrieve(
            query, self.embedder, top_k=self.summary_top_k, granularity="summaries"
        )
        return leaves + [_summary_to_snippet(h) for h in summary_hits]


def make_raptor_summarizer(
    spec: str | None = None, *, provider: LLMProvider | None = None, **kwargs: Any
) -> RaptorSummarizer | None:
    """摘要器选型工厂：默认 None＝关；extractive 确定性默认、llm opt-in（需 provider）。

    spec（大小写/留白/连字符不敏感；缺省读 RAGSPINE_RAPTOR_SUMMARIZER）：
        - None / 'none'                 -> None（不选摘要器；调用方可自行传 ExtractiveRaptorSummarizer）
        - 'extractive' / 'deterministic'-> ExtractiveRaptorSummarizer（确定性、离线默认）
        - 'llm' / 'on'                  -> 注入 provider 则 LLMRaptorSummarizer，否则 None（诚实降级）
        - 其他                          -> ValueError（列清可用 spec）
    """
    if spec is None:
        spec = os.environ.get(RAPTOR_SUMMARIZER_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_")
    if normalized == "none":
        return None
    if normalized in {"extractive", "deterministic"}:
        return ExtractiveRaptorSummarizer(**kwargs)
    if normalized in {"llm", "on"}:
        if provider is None:
            return None
        return LLMRaptorSummarizer(provider, **kwargs)
    raise ValueError(
        f"未知 raptor summarizer spec {spec!r}；可用：none / extractive / llm（llm 需注入 provider）"
    )


def make_raptor_retriever(
    base: NarrativeRetriever,
    spec: str | None = None,
    *,
    tree: RaptorTree | None = None,
    embedder: _Embedder | None = None,
    **kwargs: Any,
) -> NarrativeRetriever:
    """RAPTOR 检索选型工厂：默认 'none' 返回 base 本身（字节不变），多粒度包装 opt-in。

    spec（大小写/留白/连字符不敏感；缺省读 RAGSPINE_RAPTOR）：
        - None / 'none'      -> base 本身（opt-out，字节不变，默认；未接线即无影响）
        - 'raptor' / 'on'    -> 有 tree + embedder 则 RaptorRetriever，否则 base（诚实降级为关）
        - 其他               -> ValueError（列清可用 spec）

    其余 kwargs（summary_top_k）透传给 RaptorRetriever。
    """
    if spec is None:
        spec = os.environ.get(RAPTOR_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_")
    if normalized == "none":
        return base
    if normalized in {"raptor", "on"}:
        if tree is None or embedder is None:
            return base  # 诚实降级：缺 tree/embedder 即视为关
        return RaptorRetriever(base, tree, embedder=embedder, **kwargs)
    raise ValueError(f"未知 raptor spec {spec!r}；可用：none / raptor / on")
