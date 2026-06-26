"""W7b 叙事 GraphRAG 骨架（opt-in，默认关，behind [graph]+[llm]）单测（TDD 红 → 绿）。

钉死合约（宪章要点 = 本骨架存在的全部理由）：
- LLM 抽取实体/关系：每条实体/关系都【代码强制】带 source_doc_id + source_locator（血缘），
  且血缘是【调用方传入】的、不取信模型自报。
- 确定性降级：坏/空 JSON / provider 故障 → 空 ExtractedGraph，不崩、不编造。
- 社区发现【确定】：对关系无向投影做连通分量（union-find），成员/社区均升序，跑两次逐位一致。
- 社区摘要是【明确标注的合成】（is_synthesis=True），绝不可引为 fact；携带贡献 source_doc_ids；
  provider 故障仍降级为确定性占位（仍 is_synthesis=True，绝不编造数字）。
- make_narrative_graph(None) / ('none') → None（默认关）；'llm'/'on'+provider → 非 None；
  未知 spec → ValueError；读 RAGSPINE_NARRATIVE_GRAPH 环境变量。

红：narrative.py 落地前 import 即 ModuleNotFoundError。
"""

import json
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import (
    ChatCompletion,
    Choice,
    ProviderError,
    ResponseMessage,
)

from ragspine.graph.narrative import (
    NARRATIVE_GRAPH_ENV,
    Community,
    CommunitySummary,
    ExtractedEntity,
    ExtractedGraph,
    ExtractedRelation,
    LLMCommunitySummarizer,
    LLMGraphExtractor,
    detect_communities,
    make_narrative_graph,
)


# ---------------------------------------------------------------------------
# 测试 provider（零网络）
# ---------------------------------------------------------------------------
class ScriptedProvider:
    """按脚本依次吐响应的测试 provider（实现 corespine chat 缝）。"""

    def __init__(self, responses: list[ChatCompletion]):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        return self._responses.pop(0)


class BoomProvider:
    """chat 一律抛 ProviderError（网络/API 故障模拟）。"""

    def chat(self, messages, *, tools=None):
        raise ProviderError("boom")


def _text_response(text: str) -> ChatCompletion:
    msg = ResponseMessage(role="assistant", content=text)
    return ChatCompletion(choices=(Choice(index=0, message=msg, finish_reason="stop"),))


# 一份合规的抽取回文：两个实体 + 一条关系（模型【不】自报血缘，由调用方戳）。
_EXTRACT_JSON = json.dumps(
    {
        "entities": [
            {"name": "区域A", "type": "region"},
            {"name": "产品X", "type": "product"},
        ],
        "relations": [
            {"source": "区域A", "target": "产品X", "kind": "sells"},
        ],
    },
    ensure_ascii=False,
)


# ---------------------------------------------------------------------------
# LLMGraphExtractor：解析 + 血缘 + 有界 + 确定性降级
# ---------------------------------------------------------------------------
def test_extractor_parses_entities_and_relations():
    provider = ScriptedProvider([_text_response(_EXTRACT_JSON)])
    extractor = LLMGraphExtractor(provider)
    graph = extractor.extract("一些叙事文本", source_doc_id="doc1.pdf", source_locator="doc1.pdf#p1")
    assert isinstance(graph, ExtractedGraph)
    assert {(e.name, e.type) for e in graph.entities} == {("区域A", "region"), ("产品X", "product")}
    assert {(r.source, r.target, r.kind) for r in graph.relations} == {("区域A", "产品X", "sells")}


def test_every_entity_and_relation_carries_passed_provenance():
    """血缘是代码强制 + 调用方传入：每条实体/关系都带【传入的】 source_doc_id + source_locator。"""
    provider = ScriptedProvider([_text_response(_EXTRACT_JSON)])
    extractor = LLMGraphExtractor(provider)
    graph = extractor.extract("文本", source_doc_id="annual.pdf", source_locator="annual.pdf#sec3")
    assert graph.entities and graph.relations
    for ent in graph.entities:
        assert ent.source_doc_id == "annual.pdf"
        assert ent.source_locator == "annual.pdf#sec3"
    for rel in graph.relations:
        assert rel.source_doc_id == "annual.pdf"
        assert rel.source_locator == "annual.pdf#sec3"


def test_extractor_ignores_model_self_reported_provenance():
    """模型即便在 JSON 里塞自报血缘，也必须被忽略——血缘只认调用方传入值（反编造）。"""
    poisoned = json.dumps(
        {
            "entities": [
                {"name": "E", "type": "t", "source_doc_id": "FAKE", "source_locator": "FAKE#x"}
            ],
            "relations": [
                {"source": "E", "target": "F", "kind": "k", "source_doc_id": "FAKE"}
            ],
        },
        ensure_ascii=False,
    )
    extractor = LLMGraphExtractor(ScriptedProvider([_text_response(poisoned)]))
    graph = extractor.extract("t", source_doc_id="real.pdf", source_locator="real.pdf#1")
    assert all(e.source_doc_id == "real.pdf" for e in graph.entities)
    assert all(r.source_doc_id == "real.pdf" for r in graph.relations)


def test_extractor_degrades_on_bad_json():
    extractor = LLMGraphExtractor(ScriptedProvider([_text_response("这不是 JSON")]))
    graph = extractor.extract("t", source_doc_id="d", source_locator="l")
    assert graph == ExtractedGraph(entities=(), relations=())


def test_extractor_degrades_on_empty_output():
    extractor = LLMGraphExtractor(ScriptedProvider([_text_response("")]))
    graph = extractor.extract("t", source_doc_id="d", source_locator="l")
    assert graph.entities == () and graph.relations == ()


def test_extractor_degrades_on_provider_error():
    """provider 故障 → 空 ExtractedGraph，不崩、不编造。"""
    extractor = LLMGraphExtractor(BoomProvider())
    graph = extractor.extract("t", source_doc_id="d", source_locator="l")
    assert graph == ExtractedGraph(entities=(), relations=())


def test_extractor_degrades_on_non_object_json():
    """合法 JSON 但非对象（数组/标量）→ 空图（结构不符即降级）。"""
    extractor = LLMGraphExtractor(ScriptedProvider([_text_response("[1, 2, 3]")]))
    graph = extractor.extract("t", source_doc_id="d", source_locator="l")
    assert graph == ExtractedGraph(entities=(), relations=())


def test_extractor_is_bounded():
    """实体/关系数量有界（防发散）：超上限截断。"""
    big = json.dumps(
        {
            "entities": [{"name": f"E{i}", "type": "t"} for i in range(50)],
            "relations": [{"source": f"E{i}", "target": f"E{i + 1}", "kind": "k"} for i in range(50)],
        },
        ensure_ascii=False,
    )
    extractor = LLMGraphExtractor(ScriptedProvider([_text_response(big)]), max_entities=5, max_relations=3)
    graph = extractor.extract("t", source_doc_id="d", source_locator="l")
    assert len(graph.entities) == 5
    assert len(graph.relations) == 3


# ---------------------------------------------------------------------------
# detect_communities：确定性连通分量
# ---------------------------------------------------------------------------
def _rel(source: str, target: str, doc: str = "d", loc: str = "l") -> ExtractedRelation:
    return ExtractedRelation(source=source, target=target, kind="rel", source_doc_id=doc, source_locator=loc)


def test_detect_two_disconnected_clusters():
    """两簇互不相连的关系 → 两个社区，成员升序。"""
    graph = ExtractedGraph(
        entities=(),
        relations=(_rel("B", "A"), _rel("X", "Y")),
    )
    communities = detect_communities(graph)
    assert len(communities) == 2
    members = [c.member_names for c in communities]
    assert ("A", "B") in members
    assert ("X", "Y") in members
    # 社区按成员元组升序排列（确定性）。
    assert members == sorted(members)


def test_detect_connected_component_is_one_community():
    """链式 A-B-C-D（含一条桥）应并成一个连通分量。"""
    graph = ExtractedGraph(
        entities=(),
        relations=(_rel("A", "B"), _rel("B", "C"), _rel("C", "D")),
    )
    communities = detect_communities(graph)
    assert len(communities) == 1
    assert communities[0].member_names == ("A", "B", "C", "D")
    assert communities[0].relation_count == 3


def test_detect_communities_is_deterministic():
    """同输入跑两次逐位一致（确定性，可复现）。"""
    graph = ExtractedGraph(
        entities=(),
        relations=(_rel("m", "n"), _rel("a", "b"), _rel("b", "c"), _rel("z", "y")),
    )
    first = detect_communities(graph)
    second = detect_communities(graph)
    assert first == second


# ---------------------------------------------------------------------------
# LLMCommunitySummarizer：合成（is_synthesis）+ 血缘 + 确定性降级
# ---------------------------------------------------------------------------
def _community_graph() -> tuple[Community, ExtractedGraph]:
    graph = ExtractedGraph(
        entities=(
            ExtractedEntity(name="区域A", type="region", source_doc_id="r.pdf", source_locator="r.pdf#1"),
            ExtractedEntity(name="产品X", type="product", source_doc_id="r.pdf", source_locator="r.pdf#1"),
        ),
        relations=(
            ExtractedRelation(source="区域A", target="产品X", kind="sells", source_doc_id="r.pdf", source_locator="r.pdf#1"),
        ),
    )
    [community] = detect_communities(graph)
    return community, graph


def test_summarizer_flags_synthesis_and_carries_provenance():
    """摘要明确标注为合成（is_synthesis=True，非可引 fact）且携带贡献 source_doc_ids。"""
    community, graph = _community_graph()
    summarizer = LLMCommunitySummarizer(ScriptedProvider([_text_response("区域A 与产品X 形成销售主题。")]))
    summary = summarizer.summarize(community, graph)
    assert isinstance(summary, CommunitySummary)
    assert summary.is_synthesis is True
    assert summary.community_id == community.id
    assert summary.member_names == community.member_names
    assert "r.pdf" in summary.source_doc_ids
    assert summary.text  # 有合成正文


def test_summarizer_degrades_to_synthesis_placeholder():
    """provider 故障 → 确定性占位摘要，仍 is_synthesis=True，仍带血缘，绝不编造数字。"""
    community, graph = _community_graph()
    summarizer = LLMCommunitySummarizer(BoomProvider())
    summary = summarizer.summarize(community, graph)
    assert summary.is_synthesis is True
    assert "r.pdf" in summary.source_doc_ids
    assert summary.text  # 占位文案非空
    # 占位文案确定性：再跑一次逐字一致。
    again = LLMCommunitySummarizer(BoomProvider()).summarize(community, graph)
    assert again.text == summary.text


def test_summarizer_degrades_on_empty_output():
    """空回文也降级为占位（仍 synthesis）。"""
    community, graph = _community_graph()
    summarizer = LLMCommunitySummarizer(ScriptedProvider([_text_response("   ")]))
    summary = summarizer.summarize(community, graph)
    assert summary.is_synthesis is True
    assert summary.text


# ---------------------------------------------------------------------------
# make_narrative_graph 工厂 + env 选型（默认关）
# ---------------------------------------------------------------------------
def test_make_narrative_graph_default_off():
    """默认关：None / 'none' → None（绝不上默认路径）。"""
    assert make_narrative_graph(None) is None
    assert make_narrative_graph("none") is None


def test_make_narrative_graph_llm_needs_provider():
    """'llm' 未注入 provider → None（注入 provider 才生效，诚实降级）。"""
    assert make_narrative_graph("llm", provider=None) is None


def test_make_narrative_graph_on_returns_pipeline():
    pipeline = make_narrative_graph("llm", provider=ScriptedProvider([]))
    assert pipeline is not None
    assert isinstance(pipeline.extractor, LLMGraphExtractor)
    assert isinstance(pipeline.summarizer, LLMCommunitySummarizer)
    # 'on' 同义。
    assert make_narrative_graph("on", provider=ScriptedProvider([])) is not None


def test_make_narrative_graph_unknown_spec_raises():
    with pytest.raises(ValueError):
        make_narrative_graph("nope", provider=ScriptedProvider([]))


def test_make_narrative_graph_reads_env(monkeypatch):
    monkeypatch.setenv(NARRATIVE_GRAPH_ENV, "llm")
    assert make_narrative_graph(provider=ScriptedProvider([])) is not None
    monkeypatch.setenv(NARRATIVE_GRAPH_ENV, "none")
    assert make_narrative_graph(provider=ScriptedProvider([])) is None


# ---------------------------------------------------------------------------
# 端到端骨架：extract → detect_communities → summarize（全程带血缘 + 合成标注）
# ---------------------------------------------------------------------------
def test_end_to_end_extract_detect_summarize_chain():
    provider = ScriptedProvider([
        _text_response(_EXTRACT_JSON),          # extract
        _text_response("区域A 销售产品X 的主题综述。"),  # summarize
    ])
    pipeline = make_narrative_graph("llm", provider=provider)
    assert pipeline is not None

    graph = pipeline.extractor.extract(
        "区域A 销售产品X 的叙事。", source_doc_id="story.pdf", source_locator="story.pdf#p2"
    )
    # 全程血缘
    assert all(r.source_doc_id == "story.pdf" for r in graph.relations)

    communities = pipeline.detect_communities(graph)
    assert len(communities) == 1
    assert communities[0].member_names == ("产品X", "区域A")

    summary = pipeline.summarizer.summarize(communities[0], graph)
    assert summary.is_synthesis is True          # 合成、永不可引为 fact
    assert "story.pdf" in summary.source_doc_ids  # 血缘可溯
    assert summary.text
