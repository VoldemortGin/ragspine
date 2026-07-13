"""RelationExtractor 缝（Item ④）单测（TDD 红 → 绿）。

钉死合约（存在的全部理由）：
- 默认确定性：DeterministicRelationExtractor 从 chunk 共现【规则】建 co_occurs_with 边，
  零 LLM、清白血缘（无 derived/verified 标记）、跨两次跑逐位一致、RESTRICTED chunk 剔除。
- opt-in LLM：LLMRelationExtractor 从 LLM 抽关系，每条边【代码强制】戳
  derived=model-derived + verified=unverified，血缘取自【chunk（调用方）】而非模型自报；
  坏/空 JSON / provider 故障 → 空；有界截断；RESTRICTED chunk 绝不喂给 provider；
  竞品端点经 SecurityGate 剔除（模型抽出的竞品关系不得入图）。
- make_relation_extractor：none→None；deterministic/rule→确定性；llm 无 provider→None；
  llm+provider→LLM；读 RAGSPINE_RELATION_EXTRACTOR 环境变量；未知 spec→ValueError。
- build_relation_graph 追加且默认字节不变：relation_extractor=None 时输出与不传该 kwarg 完全一致；
  注入抽取器则在【不动】base 边之上追加标记边。

红：extractor.py 落地前 import 即 ModuleNotFoundError。
"""

import json
import os
from dataclasses import dataclass

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import (
    ChatCompletion,
    Choice,
    ProviderError,
    ResponseMessage,
)

from ragspine.common.company_profile import DimensionSpec, DomainProfile
from ragspine.graph.extractor import (
    CO_OCCURS_EDGE_TYPE,
    EDGE_META_DERIVED,
    EDGE_META_VERIFIED,
    PROVENANCE_MODEL_DERIVED,
    PROVENANCE_UNVERIFIED,
    RELATION_EXTRACTOR_ENV,
    DeterministicRelationExtractor,
    LLMRelationExtractor,
    RelationExtractor,
    make_relation_extractor,
)
from ragspine.graph.relation import build_relation_graph
from ragspine.storage.fact_store import Fact


# ---------------------------------------------------------------------------
# 鸭子类型 chunk + 测试 provider（零网络）
# ---------------------------------------------------------------------------
@dataclass
class FakeChunk:
    """鸭子类型 chunk：仅暴露抽取所读字段。"""

    doc_id: str
    entity: str = ""
    source_locator: str = ""
    sensitivity: str = "INTERNAL"
    text: str = ""


class ConstantProvider:
    """对每次 chat 都返回同一段文本（实现 corespine chat 缝）；记录被喂入的 user 文本。"""

    def __init__(self, text: str):
        self._text = text
        self.seen_user_texts: list[str] = []
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        for m in reversed(messages):
            if m.get("role") == "user":
                self.seen_user_texts.append(str(m.get("content") or ""))
                break
        msg = ResponseMessage(role="assistant", content=self._text)
        return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="stop"),))


class BoomProvider:
    """chat 一律抛 ProviderError（网络/API 故障模拟）。"""

    def chat(self, messages, *, tools=None):
        raise ProviderError("boom")


def _relations_json(*triples: tuple[str, str, str], extra: dict | None = None) -> str:
    rels = []
    for source, target, kind in triples:
        item = {"source": source, "target": target, "kind": kind}
        if extra:
            item.update(extra)
        rels.append(item)
    return json.dumps({"relations": rels}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# DeterministicRelationExtractor：规则共现（清白血缘、确定、隔离）
# ---------------------------------------------------------------------------
def test_deterministic_emits_cooccurs_for_multi_entity_doc():
    """同一 doc 内 ≥2 个不同实体 → 每对生成一条 co_occurs_with 边（canonical src<dst）。"""
    chunks = [
        FakeChunk("d1.pdf", entity="B", source_locator="d1.pdf#c1"),
        FakeChunk("d1.pdf", entity="A", source_locator="d1.pdf#c2"),
        FakeChunk("d1.pdf", entity="C", source_locator="d1.pdf#c3"),
    ]
    edges = DeterministicRelationExtractor().extract(chunks)
    pairs = {(e.src, e.dst) for e in edges}
    assert pairs == {("A", "B"), ("A", "C"), ("B", "C")}
    assert all(e.type == CO_OCCURS_EDGE_TYPE for e in edges)
    # canonical：src < dst（无 (B,A) 这类反向重复）。
    assert all(e.src < e.dst for e in edges)


def test_deterministic_metadata_is_clean_no_model_markers():
    """规则边血缘清白：只带 source_doc_id + source_locator，绝无 derived/verified 标记。"""
    chunks = [
        FakeChunk("d1.pdf", entity="A"),
        FakeChunk("d1.pdf", entity="B"),
    ]
    [edge] = DeterministicRelationExtractor().extract(chunks)
    assert edge.metadata["source_doc_id"] == "d1.pdf"
    assert edge.metadata["source_locator"] == "d1.pdf#cooccur"
    assert EDGE_META_DERIVED not in edge.metadata
    assert EDGE_META_VERIFIED not in edge.metadata


def test_deterministic_single_entity_doc_yields_no_edges():
    """单实体 doc（不足 2 个不同实体）→ 无共现边。"""
    chunks = [
        FakeChunk("d1.pdf", entity="A", source_locator="d1.pdf#c1"),
        FakeChunk("d1.pdf", entity="A", source_locator="d1.pdf#c2"),
    ]
    assert DeterministicRelationExtractor().extract(chunks) == ()


def test_deterministic_excludes_restricted_chunk():
    """RESTRICTED chunk 在输入端即被剔除，绝不参与共现（隔离）。"""
    chunks = [
        FakeChunk("d1.pdf", entity="A", source_locator="d1.pdf#c1"),
        FakeChunk("d1.pdf", entity="SECRET", sensitivity="RESTRICTED", source_locator="d1.pdf#c2"),
    ]
    # 只剩一个可见实体 → 无边；SECRET 绝不出现。
    edges = DeterministicRelationExtractor().extract(chunks)
    assert edges == ()
    names = {n for e in edges for n in (e.src, e.dst)}
    assert "SECRET" not in names


def test_deterministic_is_byte_identical_across_runs():
    """同输入两次跑逐位一致（确定性）。"""
    chunks = [
        FakeChunk("d2.pdf", entity="Y"),
        FakeChunk("d1.pdf", entity="B"),
        FakeChunk("d1.pdf", entity="A"),
        FakeChunk("d2.pdf", entity="X"),
    ]
    first = DeterministicRelationExtractor().extract(chunks)
    second = DeterministicRelationExtractor().extract(chunks)
    assert first == second
    # 边按 (src,dst,type) 升序。
    assert list(first) == sorted(first, key=lambda e: (e.src, e.dst, e.type))


def test_deterministic_is_runtime_checkable_protocol():
    assert isinstance(DeterministicRelationExtractor(), RelationExtractor)


# ---------------------------------------------------------------------------
# LLMRelationExtractor：标记 + 血缘戳 + 降级 + 有界 + 隔离 + 竞品剔除
# ---------------------------------------------------------------------------
def _profile_with_competitor() -> DomainProfile:
    return DomainProfile(
        home_company_name="Synth Group",
        home_entity_code="GRP",
        home_entity_synonyms={"grp": "GRP", "suba": "SUBA"},
        entity_geography={"GRP": "GLOBAL", "SUBA": "NORTH"},
        external_entities={"rivalcorp": "RivalCorp"},
        home_entity_labels={"GRP": "Group", "SUBA": "Sub A"},
        dimensions=(
            DimensionSpec("metric", label="Metric", synonyms={"rev": "REV"}, labels={"REV": "Revenue"}),
        ),
    )


def test_llm_edges_carry_model_derived_unverified_markers():
    """LLM 抽出的边强制带 derived=model-derived + verified=unverified（永不静默取信）。"""
    provider = ConstantProvider(_relations_json(("A", "B", "partners_with")))
    extractor = LLMRelationExtractor(provider)
    chunks = [FakeChunk("d1.pdf", source_locator="d1.pdf#p1", text="A 与 B 合作。")]
    [edge] = extractor.extract(chunks)
    assert edge.src == "A" and edge.dst == "B" and edge.type == "partners_with"
    assert edge.metadata[EDGE_META_DERIVED] == PROVENANCE_MODEL_DERIVED
    assert edge.metadata[EDGE_META_VERIFIED] == PROVENANCE_UNVERIFIED


def test_llm_lineage_from_chunk_not_model_self_report():
    """模型即便自报 source_doc_id，也被忽略——血缘只认 chunk（调用方）传入值。"""
    provider = ConstantProvider(
        _relations_json(("A", "B", "k"), extra={"source_doc_id": "FAKE", "source_locator": "FAKE#x"})
    )
    extractor = LLMRelationExtractor(provider)
    chunks = [FakeChunk("real.pdf", source_locator="real.pdf#7", text="t")]
    [edge] = extractor.extract(chunks)
    assert edge.metadata["source_doc_id"] == "real.pdf"
    assert edge.metadata["source_locator"] == "real.pdf#7"


def test_llm_degrades_on_bad_json():
    extractor = LLMRelationExtractor(ConstantProvider("这不是 JSON"))
    assert extractor.extract([FakeChunk("d", text="t")]) == ()


def test_llm_degrades_on_empty_output():
    extractor = LLMRelationExtractor(ConstantProvider(""))
    assert extractor.extract([FakeChunk("d", text="t")]) == ()


def test_llm_degrades_on_provider_error():
    extractor = LLMRelationExtractor(BoomProvider())
    assert extractor.extract([FakeChunk("d", text="t")]) == ()


def test_llm_bounded_by_max_relations():
    """总量有界：max_relations 截断（防发散）。"""
    provider = ConstantProvider(
        _relations_json(("A", "B", "k"), ("C", "D", "k"), ("E", "F", "k"))
    )
    extractor = LLMRelationExtractor(provider, max_relations=2)
    edges = extractor.extract([FakeChunk("d1.pdf", source_locator="d1.pdf#p1", text="t")])
    assert len(edges) == 2


def test_llm_never_sends_restricted_chunk_to_provider():
    """RESTRICTED chunk 绝不喂给 provider（隔离路由 —— 用 spy 记录调用证明）。"""
    provider = ConstantProvider(_relations_json(("A", "B", "k")))
    extractor = LLMRelationExtractor(provider)
    chunks = [
        FakeChunk("open.pdf", source_locator="open.pdf#1", text="OPEN_TEXT"),
        FakeChunk("secret.pdf", sensitivity="RESTRICTED", source_locator="secret.pdf#1", text="SECRET_TEXT"),
    ]
    extractor.extract(chunks)
    assert "OPEN_TEXT" in provider.seen_user_texts
    assert "SECRET_TEXT" not in provider.seen_user_texts
    assert provider.calls == 1


def test_llm_drops_competitor_endpoint():
    """任一端点是竞品/外部主体 → 该边被 SecurityGate 剔除（竞品关系不得入图）。"""
    provider = ConstantProvider(
        _relations_json(("SUBA", "RivalCorp", "beats"), ("SUBA", "GRP", "part_of"))
    )
    extractor = LLMRelationExtractor(provider, profile=_profile_with_competitor())
    edges = extractor.extract([FakeChunk("d1.pdf", source_locator="d1.pdf#p1", text="t")])
    pairs = {(e.src, e.dst) for e in edges}
    assert ("SUBA", "GRP") in pairs
    assert all("RivalCorp" not in (e.src, e.dst) for e in edges)


def test_llm_without_gate_emits_without_screening():
    """无 profile 无 gate 时优雅处理：仍能抽边（无竞品筛可用）。"""
    provider = ConstantProvider(_relations_json(("A", "B", "k")))
    extractor = LLMRelationExtractor(provider)  # 无 gate
    edges = extractor.extract([FakeChunk("d1.pdf", source_locator="d1.pdf#p1", text="t")])
    assert len(edges) == 1


# ---------------------------------------------------------------------------
# make_relation_extractor 工厂 + env 选型（默认关）
# ---------------------------------------------------------------------------
def test_make_relation_extractor_default_off():
    assert make_relation_extractor(None) is None
    assert make_relation_extractor("none") is None


def test_make_relation_extractor_deterministic_variants():
    assert isinstance(make_relation_extractor("deterministic"), DeterministicRelationExtractor)
    assert isinstance(make_relation_extractor("rule"), DeterministicRelationExtractor)
    assert isinstance(make_relation_extractor("cooccurrence"), DeterministicRelationExtractor)


def test_make_relation_extractor_llm_needs_provider():
    assert make_relation_extractor("llm", provider=None) is None


def test_make_relation_extractor_llm_with_provider():
    ext = make_relation_extractor("llm", provider=ConstantProvider("{}"))
    assert isinstance(ext, LLMRelationExtractor)
    assert isinstance(make_relation_extractor("on", provider=ConstantProvider("{}")), LLMRelationExtractor)


def test_make_relation_extractor_reads_env(monkeypatch):
    monkeypatch.setenv(RELATION_EXTRACTOR_ENV, "deterministic")
    assert isinstance(make_relation_extractor(), DeterministicRelationExtractor)
    monkeypatch.setenv(RELATION_EXTRACTOR_ENV, "none")
    assert make_relation_extractor() is None


def test_make_relation_extractor_unknown_spec_raises():
    with pytest.raises(ValueError):
        make_relation_extractor("nope")


# ---------------------------------------------------------------------------
# build_relation_graph 追加 + 默认字节不变
# ---------------------------------------------------------------------------
def _base_profile() -> DomainProfile:
    return _profile_with_competitor()


def _base_facts() -> list[Fact]:
    return [
        Fact("REV", "SUBA", "NORTH", "TOTAL", "FY", "2024", 10.0, "USD_M", "suba.pdf", "suba.pdf#p1"),
    ]


def _base_chunks() -> list[FakeChunk]:
    return [
        FakeChunk("news.pdf", entity="SUBA", source_locator="news.pdf#c1"),
        FakeChunk("news.pdf", entity="GRP", source_locator="news.pdf#c2"),
    ]


def test_build_default_byte_identical_without_kwarg():
    """relation_extractor=None 与不传该 kwarg：节点/边集完全一致，且无 co_occurs/model-derived 边。"""
    without = build_relation_graph(_base_profile(), facts=_base_facts(), chunks=_base_chunks())
    with_none = build_relation_graph(
        _base_profile(), facts=_base_facts(), chunks=_base_chunks(), relation_extractor=None
    )
    seeds = ["GRP", "SUBA", "REV", "RivalCorp", "GLOBAL", "NORTH", "CompX", "suba.pdf", "news.pdf"]
    a = without.subgraph(seeds, depth=2)
    b = with_none.subgraph(seeds, depth=2)
    assert a.nodes == b.nodes
    assert a.edges == b.edges
    # 默认路径绝无共现 / 模型派生边。
    assert all(e.type != CO_OCCURS_EDGE_TYPE for e in a.edges)
    assert all(e.metadata.get(EDGE_META_DERIVED) != PROVENANCE_MODEL_DERIVED for e in a.edges)


def test_build_with_deterministic_extractor_adds_cooccurs_on_top():
    """注入 DeterministicRelationExtractor：追加 co_occurs_with 边，base mentions 边不受扰。"""
    base = build_relation_graph(_base_profile(), facts=_base_facts(), chunks=_base_chunks())
    augmented = build_relation_graph(
        _base_profile(),
        facts=_base_facts(),
        chunks=_base_chunks(),
        relation_extractor=DeterministicRelationExtractor(),
    )
    base_mentions = {n.id for n in base.neighbors("news.pdf", edge_type="mentions")}
    aug_mentions = {n.id for n in augmented.neighbors("news.pdf", edge_type="mentions")}
    assert base_mentions == aug_mentions  # base mentions 边不受扰
    # GRP 与 SUBA 在 news.pdf 共现 → co_occurs_with 边追加。
    cooccurs = {n.id for n in augmented.neighbors("GRP", edge_type=CO_OCCURS_EDGE_TYPE, direction="both")}
    assert "SUBA" in cooccurs
    # base 图无此边。
    assert not base.neighbors("GRP", edge_type=CO_OCCURS_EDGE_TYPE, direction="both")


def test_build_with_llm_extractor_adds_marked_edges_on_top():
    """注入 LLMRelationExtractor（脚本 provider）：在不动 base 边之上追加带标记的实体→实体边。"""
    provider = ConstantProvider(_relations_json(("SUBA", "GRP", "reports_to")))
    extractor = LLMRelationExtractor(provider, profile=_base_profile())
    base = build_relation_graph(_base_profile(), facts=_base_facts(), chunks=_base_chunks())
    augmented = build_relation_graph(
        _base_profile(),
        facts=_base_facts(),
        chunks=_base_chunks(),
        relation_extractor=extractor,
    )
    # base parent_of 边不受扰。
    assert {n.id for n in base.neighbors("GRP", edge_type="parent_of")} == {
        n.id for n in augmented.neighbors("GRP", edge_type="parent_of")
    }
    # 新增 reports_to 边，且带 model-derived / unverified 标记。
    sub = augmented.subgraph(["SUBA", "GRP"], depth=1)
    marked = [e for e in sub.edges if e.type == "reports_to"]
    assert marked
    assert all(e.metadata.get(EDGE_META_DERIVED) == PROVENANCE_MODEL_DERIVED for e in marked)
    assert all(e.metadata.get(EDGE_META_VERIFIED) == PROVENANCE_UNVERIFIED for e in marked)
