"""RelationExtractor 缝（Item ④）：默认确定性共现抽取 + opt-in LLM 关系抽取（behind [llm]）。

在 W7a 结构关系图（relation.py）之上开一条【追加关系】的缝：从 chunk 里抽出【base 图之外】
的实体↔实体关系边，喂回 GraphStore。两个实现、一个工厂，默认关（None）＝字节不变：

- DeterministicRelationExtractor —— 默认、纯规则、零 LLM、确定：同一 doc 内共现的实体两两连
  co_occurs_with 边（canonical src<dst、去重、升序）。血缘【清白】（只带 doc 血缘，绝无模型标记，
  因为这是规则派生非模型派生）。它与 base 图的 doc→entity `mentions` 边【本质不同】——那是文档
  提及实体，这是实体在同一文档内共现——故不重复建 mentions。RESTRICTED chunk 在输入端即剔除。
- LLMRelationExtractor —— opt-in，镜像 narrative.py 的 LLMGraphExtractor 降级纪律：提示 → JSON →
  鲁棒解析 → 有界 → 【调用方（chunk）戳血缘，绝不取信模型自报】→ 降级到空（ProviderError / 坏 JSON）。
  每条 LLM 抽出的边强制带 derived=model-derived + verified=unverified 标记（永不静默取信模型断言）；
  RESTRICTED chunk 绝不喂给 LLM（隔离路由）；两个端点都经 SecurityGate 筛，任一是竞品/外部主体即
  丢弃该边（LLM 抽出的竞品关系不得入图）。

抽取 LLM 非确定，故仅作 opt-in 适配器（经 make_relation_extractor / RAGSPINE_RELATION_EXTRACTOR
选用，且必须注入 provider 才生效）；默认 None＝关，默认 build_relation_graph 输出字节不变。
"""

import json
import os
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from ragspine.agent.llm_provider import LLMProvider, ProviderError
from ragspine.agent.security_gate import SecurityGate
from ragspine.common.company_profile import DomainProfile
from ragspine.graph.store import RESTRICTED_SENSITIVITY, GraphEdge

# 抽取器选型读取的环境变量名（缺省 spec 时生效；范式同 RAGSPINE_NARRATIVE_GRAPH）。
RELATION_EXTRACTOR_ENV = "RAGSPINE_RELATION_EXTRACTOR"

# 抽取数量默认上限（有界，防发散 / 防 prompt-injection 撑爆）。
DEFAULT_MAX_RELATIONS = 64

# 血缘标记：LLM 抽出的边【必带】的两枚戳——它是模型派生、且未经核实（永不静默取信）。
PROVENANCE_MODEL_DERIVED = "model-derived"
PROVENANCE_UNVERIFIED = "unverified"
# 承载上述标记的 metadata 键名。
EDGE_META_DERIVED = "derived"
EDGE_META_VERIFIED = "verified"

# 关系种类缺省时的回退标签（绝不编造具体语义，只给中性占位）。
_DEFAULT_RELATION_KIND = "related_to"
# 规则共现边的关系种类。
CO_OCCURS_EDGE_TYPE = "co_occurs_with"

# 抽取提示：要求模型只输出 {"relations":[{"source","target","kind"}]} 的 JSON 对象，不要解释。
# 刻意【不】要求模型给血缘——血缘由调用方（chunk）传入值代码强制戳（反编造）。
_EXTRACT_SYSTEM = (
    "你是关系抽取器。从给定文本中抽取实体之间的关系，只输出一个 JSON 对象，"
    '形如 {"relations":[{"source":"...","target":"...","kind":"..."}]}，不要任何解释。'
    "实体名用文本中的原词；无可抽取内容时输出 relations 为空数组的对象。"
)

__all__ = [
    "RELATION_EXTRACTOR_ENV",
    "DEFAULT_MAX_RELATIONS",
    "PROVENANCE_MODEL_DERIVED",
    "PROVENANCE_UNVERIFIED",
    "EDGE_META_DERIVED",
    "EDGE_META_VERIFIED",
    "CO_OCCURS_EDGE_TYPE",
    "RelationExtractor",
    "DeterministicRelationExtractor",
    "LLMRelationExtractor",
    "make_relation_extractor",
]


def _is_restricted(chunk: object) -> bool:
    """chunk 是否 RESTRICTED 来源（隔离判据）：sensitivity 归一大写后 == RESTRICTED。"""
    return str(getattr(chunk, "sensitivity", "")).upper() == RESTRICTED_SENSITIVITY


def _str_field(value: Any, default: str = "") -> str:
    """取一个非空字符串字段；非字符串/空白 → default。"""
    return value.strip() if isinstance(value, str) and value.strip() else default


# ---------------------------------------------------------------------------
# 缝：RelationExtractor 协议
# ---------------------------------------------------------------------------
@runtime_checkable
class RelationExtractor(Protocol):
    """追加关系抽取缝：从 chunk 抽出【base 图之外】的实体↔实体关系边（带血缘）。

    实现可为确定（规则）或非确定（LLM）；返回值追加进 GraphStore，绝不替换 base 边。
    chunk 鸭子类型：getattr(chunk, "doc_id"/"entity"/"source_locator"/"sensitivity"/"text", ...)。
    """

    def extract(self, chunks: Iterable[object]) -> tuple[GraphEdge, ...]: ...


# ---------------------------------------------------------------------------
# DeterministicRelationExtractor：规则共现（默认，零 LLM，确定，清白血缘）
# ---------------------------------------------------------------------------
class DeterministicRelationExtractor:
    """默认规则共现抽取器：同一 doc 内共现的实体两两连 co_occurs_with 边（确定、零编造）。

    按 doc_id 分组；每个含 ≥2 个【不同非空】实体的 doc，对其实体升序两两配对（src<dst，故无向对
    canonical 且去重）发一条边，血缘【清白】——只记 doc 自身来源，绝无 derived/verified 标记
    （这是规则派生，非模型派生）。RESTRICTED chunk 在输入端即剔除（隔离）。同输入同输出（升序构造）。
    """

    def extract(self, chunks: Iterable[object]) -> tuple[GraphEdge, ...]:
        by_doc: dict[str, set[str]] = {}
        for chunk in chunks:
            if _is_restricted(chunk):
                continue
            doc_id = str(getattr(chunk, "doc_id", ""))
            entity = str(getattr(chunk, "entity", ""))
            if not doc_id or not entity:
                continue
            by_doc.setdefault(doc_id, set()).add(entity)

        edges: list[GraphEdge] = []
        for doc_id in sorted(by_doc):
            entities = sorted(by_doc[doc_id])
            if len(entities) < 2:
                continue
            for i in range(len(entities)):
                for j in range(i + 1, len(entities)):
                    edges.append(
                        GraphEdge(
                            src=entities[i],
                            dst=entities[j],
                            type=CO_OCCURS_EDGE_TYPE,
                            metadata={
                                "source_doc_id": doc_id,
                                "source_locator": f"{doc_id}#cooccur",
                            },
                        )
                    )
        edges.sort(key=lambda e: (e.src, e.dst, e.type))
        return tuple(edges)


# ---------------------------------------------------------------------------
# LLMRelationExtractor：提示 → JSON → 戳血缘 → 隔离/竞品筛 → 标记 → 有界 → 确定性降级
# ---------------------------------------------------------------------------
class LLMRelationExtractor:
    """LLM 驱动的关系抽取器（opt-in）：每条边带 model-derived/unverified 标记，血缘取自 chunk。

    对每个 chunk（按 (doc_id, source_locator) 确定性排序）：RESTRICTED 即跳过（绝不喂给 LLM）；
    否则单轮调 provider 产出 {"relations":[...]} JSON，鲁棒解析。每条解析出的关系：
    - 血缘从【chunk（调用方）】戳，绝不取信模型自报的任何 source_doc_id/locator；
    - 两个端点都经 SecurityGate 筛，任一是竞品/外部主体即丢弃该边（竞品关系不得入图）；
    - 强制打 derived=model-derived + verified=unverified 两枚标记（永不静默取信）。
    provider 故障 / 坏 JSON → 贡献空，绝不抛。总量按 max_relations 截断。返回按 (src,dst,type) 升序。
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        profile: DomainProfile | None = None,
        security_gate: SecurityGate | None = None,
        max_relations: int = DEFAULT_MAX_RELATIONS,
    ) -> None:
        self.provider = provider
        if security_gate is None and profile is not None:
            security_gate = SecurityGate(profile.external_entities, profile.home_company_name)
        # 无 gate（既未传 gate 也未传 profile）：无竞品清单可筛 → 不做竞品剔除（诚实降级，仍抽边）。
        self.security_gate = security_gate
        self.max_relations = max(0, max_relations)

    def extract(self, chunks: Iterable[object]) -> tuple[GraphEdge, ...]:
        ordered = sorted(
            chunks,
            key=lambda c: (
                str(getattr(c, "doc_id", "")),
                str(getattr(c, "source_locator", "")),
            ),
        )
        edges: list[GraphEdge] = []
        for chunk in ordered:
            if len(edges) >= self.max_relations:
                break
            if _is_restricted(chunk):
                continue  # RESTRICTED 绝不喂给 LLM（隔离路由）
            doc_id = str(getattr(chunk, "doc_id", ""))
            source_locator = str(getattr(chunk, "source_locator", ""))
            text = str(getattr(chunk, "text", ""))
            for source, target, kind in self._extract_chunk(text):
                if len(edges) >= self.max_relations:
                    break
                if self._is_external(source) or self._is_external(target):
                    continue  # 任一端点竞品/外部主体 → 丢弃（竞品关系不得入图）
                edges.append(
                    GraphEdge(
                        src=source,
                        dst=target,
                        type=kind,
                        metadata={
                            "source_doc_id": doc_id,
                            "source_locator": source_locator,
                            EDGE_META_DERIVED: PROVENANCE_MODEL_DERIVED,
                            EDGE_META_VERIFIED: PROVENANCE_UNVERIFIED,
                        },
                    )
                )
        edges.sort(key=lambda e: (e.src, e.dst, e.type))
        return tuple(edges)

    def _is_external(self, name: str) -> bool:
        """端点是否竞品/外部主体（有 gate 时经 SecurityGate 检测；无 gate → 一律 False）。"""
        if self.security_gate is None:
            return False
        return bool(self.security_gate.detect(name).external_entity)

    def _extract_chunk(self, text: str) -> tuple[tuple[str, str, str], ...]:
        """单 chunk 抽取：provider → JSON → (source,target,kind) 三元组；故障/坏 JSON → 空。"""
        try:
            resp = self.provider.chat(
                [
                    {"role": "system", "content": _EXTRACT_SYSTEM},
                    {"role": "user", "content": text},
                ]
            )
        except ProviderError:
            return ()
        out = resp.choices[0].message.content or ""
        return self._parse(out)

    @staticmethod
    def _parse(text: str) -> tuple[tuple[str, str, str], ...]:
        """从模型回文鲁棒解析 relations 数组；任何不合规一律降级（空 / 跳过该条），绝不抛、绝不编造。"""
        try:
            parsed = json.loads(text.strip())
        except (TypeError, ValueError):
            return ()
        if not isinstance(parsed, dict):
            return ()
        raw = parsed.get("relations")
        if not isinstance(raw, list):
            return ()
        out: list[tuple[str, str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            source = _str_field(item.get("source"))
            target = _str_field(item.get("target"))
            if not source or not target:
                continue
            out.append((source, target, _str_field(item.get("kind"), _DEFAULT_RELATION_KIND)))
        return tuple(out)


# ---------------------------------------------------------------------------
# 工厂（默认关）
# ---------------------------------------------------------------------------
def make_relation_extractor(
    spec: str | None = None,
    *,
    provider: LLMProvider | None = None,
    profile: DomainProfile | None = None,
    **kwargs: Any,
) -> RelationExtractor | None:
    """关系抽取器选型工厂：默认 None＝关（build_relation_graph 字节不变），LLM 抽取 opt-in。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_RELATION_EXTRACTOR）：
        - None / 'none'                           -> None（默认关；不追加任何关系边）
        - 'deterministic' / 'rule' / 'cooccurrence' -> DeterministicRelationExtractor（规则共现，确定）
        - 'llm' / 'on'                            -> 注入了 provider 则 LLMRelationExtractor；
                                                     未注入 provider 则 None（诚实降级为关）
        - 其他                                    -> ValueError（列清可用 spec）

    其余 kwargs（security_gate / max_relations）透传给 LLMRelationExtractor。
    """
    if spec is None:
        spec = os.environ.get(RELATION_EXTRACTOR_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "none":
        return None
    if normalized in {"deterministic", "rule", "cooccurrence"}:
        return DeterministicRelationExtractor()
    if normalized in {"llm", "on"}:
        if provider is None:
            return None
        return LLMRelationExtractor(provider, profile=profile, **kwargs)
    raise ValueError(
        f"未知 relation-extractor spec：{normalized!r}"
        "（可选 none / deterministic / rule / cooccurrence / llm / on；llm 需注入 provider）"
    )
