"""RAPTOR 递归聚类+摘要树（W10，opt-in 默认关）单测（TDD 红 → 绿）。

钉死合约（宪章要点 = 本能力存在的全部理由，对标 W7b 叙事 GraphRAG 的反编造纪律）：
- 聚类【确定性】：确定性阈值聚类（余弦≥阈值建边 + 连通分量），同输入同树（无随机 UMAP/GMM）。
- 每个上层节点是【合成】（is_synthesis=True），绝不可引为 fact；LLM 摘要 opt-in，degrade 到确定性
  extractive；聚类本身零 LLM 可跑。
- 每个合成节点带【provenance】：综合了哪些底层 chunk（source_doc_id/locator 并集，不编造血缘）。
- 多粒度检索：可命中细节叶片，也可命中高层摘要（补全局/多跳综合）。
- isolation 继承：RESTRICTED chunk 绝不进树/摘要（+ 反证）。
- make_* 工厂 + RAGSPINE_ 旋钮：默认关（返回 base 本身 / None），不改默认检索（字节不变）。

红：ragspine.retrieval.raptor 落地前 import 即 ModuleNotFoundError。
"""

import os
from dataclasses import replace

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion, Choice, ProviderError, ResponseMessage

from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.raptor import (
    RAPTOR_ENV,
    RAPTOR_SUMMARIZER_ENV,
    ExtractiveRaptorSummarizer,
    LLMRaptorSummarizer,
    RaptorHit,
    RaptorNode,
    RaptorRetriever,
    RaptorTree,
    build_raptor_tree,
    cluster_by_similarity,
    make_raptor_retriever,
    make_raptor_summarizer,
)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class FakeEmbedder:
    """确定性 fake：按内容标记映射到正交向量组（水果=[1,0,0]、汽车=[0,1,0]、其余=[0,0,1]）。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            if "水果" in t:
                out.append([1.0, 0.0, 0.0])
            elif "汽车" in t:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out


class ScriptedProvider:
    def __init__(self, responses: list[ChatCompletion]):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        return self._responses.pop(0)


class BoomProvider:
    def chat(self, messages, *, tools=None):
        raise ProviderError("boom")


def _text_response(text: str) -> ChatCompletion:
    return ChatCompletion(
        choices=(Choice(index=0, message=ResponseMessage(role="assistant", content=text), finish_reason="stop"),)
    )


def _chunk(seq: int, text: str, doc_id: str, sensitivity: str = "INTERNAL") -> Chunk:
    return Chunk(
        chunk_id=f"{doc_id}#c{seq}",
        doc_id=doc_id,
        seq=seq,
        text=text,
        source_locator=f"{doc_id}#para{seq + 1}",
        para_start=seq + 1,
        para_end=seq + 1,
        sensitivity=sensitivity,
    )


def _four_chunks() -> list[Chunk]:
    return [
        _chunk(0, "苹果 水果 红色 香甜", "fruit_a.pdf"),
        _chunk(1, "香蕉 水果 黄色 软糯", "fruit_b.pdf"),
        _chunk(2, "轿车 汽车 快速 舒适", "car_a.pdf"),
        _chunk(3, "卡车 汽车 载重 强劲", "car_b.pdf"),
    ]


# ---------------------------------------------------------------------------
# 确定性聚类
# ---------------------------------------------------------------------------
def test_cluster_groups_similar_separates_dissimilar():
    clusters = cluster_by_similarity(
        ["a", "b", "c"], [(1.0, 0.0), (1.0, 0.0), (0.0, 1.0)], threshold=0.9
    )
    assert ("a", "b") in clusters
    assert ("c",) in clusters


def test_cluster_is_deterministic_and_sorted():
    ids, embs = ["z", "y", "x"], [(1.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
    first = cluster_by_similarity(ids, embs, threshold=0.9)
    second = cluster_by_similarity(ids, embs, threshold=0.9)
    assert first == second
    # 成员升序、簇整体有序（确定性）。
    for members in first:
        assert list(members) == sorted(members)
    assert list(first) == sorted(first)


# ---------------------------------------------------------------------------
# 建树：叶片 + 合成摘要层 + provenance + 确定性
# ---------------------------------------------------------------------------
def test_tree_has_leaves_and_synthesis_summaries():
    tree = build_raptor_tree(_four_chunks(), embedder=FakeEmbedder())
    assert isinstance(tree, RaptorTree)
    leaves = tree.leaves
    assert len(leaves) == 4
    assert all(leaf.is_synthesis is False and leaf.level == 0 for leaf in leaves)
    summaries = tree.summaries
    assert summaries, "应至少建一层合成摘要节点"
    assert all(isinstance(s, RaptorNode) and s.is_synthesis is True and s.level >= 1 for s in summaries)


def test_summary_nodes_carry_provenance():
    """每个合成节点带 provenance：综合了哪些底层 chunk 的 doc_id + locator。"""
    tree = build_raptor_tree(_four_chunks(), embedder=FakeEmbedder())
    for s in tree.summaries:
        assert s.source_doc_ids, "合成节点必须记录其综合的来源 doc"
        assert s.source_locators, "合成节点必须记录其综合的来源 locator"
        assert len(s.member_ids) >= 2  # 合成节点由多个成员聚出


def test_summary_provenance_never_fabricated():
    """合成节点血缘 ⊆ 叶片血缘并集（绝不编造出处）。"""
    tree = build_raptor_tree(_four_chunks(), embedder=FakeEmbedder())
    leaf_docs = {d for leaf in tree.leaves for d in leaf.source_doc_ids}
    leaf_locs = {loc for leaf in tree.leaves for loc in leaf.source_locators}
    for s in tree.summaries:
        assert set(s.source_doc_ids) <= leaf_docs
        assert set(s.source_locators) <= leaf_locs


def test_fruit_and_car_form_separate_clusters():
    """水果两片聚一簇、汽车两片聚一簇 → 各生一个合成节点。"""
    tree = build_raptor_tree(_four_chunks(), embedder=FakeEmbedder())
    level1 = [s for s in tree.summaries if s.level == 1]
    assert len(level1) == 2
    docsets = sorted(tuple(s.source_doc_ids) for s in level1)
    assert docsets == [("car_a.pdf", "car_b.pdf"), ("fruit_a.pdf", "fruit_b.pdf")]


def test_build_tree_is_deterministic():
    a = build_raptor_tree(_four_chunks(), embedder=FakeEmbedder())
    b = build_raptor_tree(_four_chunks(), embedder=FakeEmbedder())
    assert a == b


def test_build_tree_no_embedder_is_leaves_only():
    """无 embedder → 无法聚类 → 只有叶片层（诚实降级，不建摘要层）。"""
    tree = build_raptor_tree(_four_chunks(), embedder=None)
    assert len(tree.leaves) == 4
    assert tree.summaries == []


# ---------------------------------------------------------------------------
# isolation：RESTRICTED 不进树 + 反证
# ---------------------------------------------------------------------------
def test_restricted_never_enters_tree():
    chunks = _four_chunks() + [_chunk(4, "机密 水果 内部数字 42", "secret.pdf", sensitivity="RESTRICTED")]
    tree = build_raptor_tree(chunks, embedder=FakeEmbedder())
    assert all("机密" not in n.text for n in tree.nodes)
    assert all("secret.pdf" not in n.source_doc_ids for n in tree.nodes)
    assert all("secret.pdf" != leaf.doc_id for leaf in tree.leaves)


def test_restricted_exclusion_reverse_proof():
    """反证：同一块若非 RESTRICTED，则确实进树——证明是隔离在起作用，非文本恰好缺席。"""
    internal = _chunk(4, "机密 水果 内部数字 42", "secret.pdf", sensitivity="INTERNAL")
    tree = build_raptor_tree(_four_chunks() + [internal], embedder=FakeEmbedder())
    assert any(leaf.doc_id == "secret.pdf" for leaf in tree.leaves)


# ---------------------------------------------------------------------------
# 多粒度检索
# ---------------------------------------------------------------------------
def test_retrieve_multi_granularity():
    emb = FakeEmbedder()
    tree = build_raptor_tree(_four_chunks(), embedder=emb)
    hits = tree.retrieve("哪些是水果", emb, top_k=10)
    assert hits and all(isinstance(h, RaptorHit) for h in hits)
    assert any(not h.node.is_synthesis for h in hits)  # 细节叶片
    assert any(h.node.is_synthesis for h in hits)       # 高层摘要
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)       # 打分降序确定


def test_retrieve_granularity_filter():
    emb = FakeEmbedder()
    tree = build_raptor_tree(_four_chunks(), embedder=emb)
    only_summaries = tree.retrieve("水果", emb, top_k=10, granularity="summaries")
    assert only_summaries and all(h.node.is_synthesis for h in only_summaries)
    only_leaves = tree.retrieve("水果", emb, top_k=10, granularity="leaves")
    assert only_leaves and all(not h.node.is_synthesis for h in only_leaves)


# ---------------------------------------------------------------------------
# 摘要器：extractive 默认（确定性） + LLM opt-in（degrade）
# ---------------------------------------------------------------------------
def test_extractive_summarizer_is_deterministic():
    s = ExtractiveRaptorSummarizer()
    a = s.summarize(["苹果 水果 红色。", "香蕉 水果 黄色。"])
    b = s.summarize(["苹果 水果 红色。", "香蕉 水果 黄色。"])
    assert a and a == b


def test_llm_summarizer_uses_provider():
    s = LLMRaptorSummarizer(ScriptedProvider([_text_response("水果类主题综述")]))
    assert "水果" in s.summarize(["苹果 水果", "香蕉 水果"])


def test_llm_summarizer_degrades_to_extractive():
    """provider 故障 → 确定性降级到 extractive（非空、可复现，绝不崩、绝不编造）。"""
    out = LLMRaptorSummarizer(BoomProvider()).summarize(["苹果 水果 红色", "香蕉 水果 黄色"])
    assert out
    again = LLMRaptorSummarizer(BoomProvider()).summarize(["苹果 水果 红色", "香蕉 水果 黄色"])
    assert out == again


def test_llm_summarizer_degrades_on_empty_output():
    out = LLMRaptorSummarizer(ScriptedProvider([_text_response("   ")])).summarize(["苹果 水果"])
    assert out  # 空回文降级到 extractive


# ---------------------------------------------------------------------------
# make_raptor_summarizer 工厂 + env（默认关）
# ---------------------------------------------------------------------------
def test_make_summarizer_default_off():
    assert make_raptor_summarizer(None) is None
    assert make_raptor_summarizer("none") is None


def test_make_summarizer_extractive():
    assert isinstance(make_raptor_summarizer("extractive"), ExtractiveRaptorSummarizer)


def test_make_summarizer_llm_needs_provider():
    assert make_raptor_summarizer("llm", provider=None) is None
    assert isinstance(make_raptor_summarizer("llm", provider=ScriptedProvider([])), LLMRaptorSummarizer)


def test_make_summarizer_unknown_raises():
    with pytest.raises(ValueError):
        make_raptor_summarizer("nope")


def test_make_summarizer_reads_env(monkeypatch):
    monkeypatch.setenv(RAPTOR_SUMMARIZER_ENV, "extractive")
    assert isinstance(make_raptor_summarizer(), ExtractiveRaptorSummarizer)
    monkeypatch.setenv(RAPTOR_SUMMARIZER_ENV, "none")
    assert make_raptor_summarizer() is None


# ---------------------------------------------------------------------------
# RaptorRetriever 包装件 + make_raptor_retriever（默认关，字节不变）
# ---------------------------------------------------------------------------
class FakeBase:
    """A 线 NarrativeRetriever 替身：回定死的（已 RESTRICTED-剥离的）叶片 snippet。"""

    def __init__(self, snippets: list[dict[str, object]]):
        self.snippets = snippets
        self.calls = 0

    def retrieve(self, query, *, filters=None, top_k=50):
        self.calls += 1
        return list(self.snippets)


def _leaf_snippet(doc_id: str, text: str) -> dict[str, object]:
    return {"text": text, "doc_id": doc_id, "source_locator": f"{doc_id}#para1", "sensitivity": "INTERNAL"}


def test_make_raptor_retriever_default_off_byte_identical():
    base = FakeBase([_leaf_snippet("fruit_a.pdf", "苹果 水果")])
    assert make_raptor_retriever(base, None) is base
    assert make_raptor_retriever(base, "none") is base


def test_make_raptor_retriever_missing_deps_returns_base():
    """'on' 但缺 tree/embedder → 返回 base（诚实降级为关）。"""
    base = FakeBase([])
    assert make_raptor_retriever(base, "on", tree=None, embedder=None) is base


def test_raptor_retriever_appends_synthesis_snippets():
    emb = FakeEmbedder()
    tree = build_raptor_tree(_four_chunks(), embedder=emb)
    base = FakeBase([_leaf_snippet("fruit_a.pdf", "苹果 水果 红色")])
    retriever = make_raptor_retriever(base, "on", tree=tree, embedder=emb)
    assert isinstance(retriever, RaptorRetriever)
    out = retriever.retrieve("哪些是水果")
    # base 叶片原样在前（可引、检索不丢）。
    assert out[: len(base.snippets)] == base.snippets
    # 追加的合成节点【明确标注 is_synthesis】（绝不当 citable fact）。
    synth = [s for s in out if s.get("is_synthesis")]
    assert synth
    assert all(s["is_synthesis"] is True for s in synth)


def test_raptor_retriever_synthesis_provenance_honest():
    """合成 snippet 的血缘 ⊆ 真实叶片血缘（不编造出处）。"""
    emb = FakeEmbedder()
    tree = build_raptor_tree(_four_chunks(), embedder=emb)
    base = FakeBase([])
    out = make_raptor_retriever(base, "on", tree=tree, embedder=emb).retrieve("水果")
    real_docs = {leaf.doc_id for leaf in tree.leaves}
    for s in out:
        if s.get("is_synthesis"):
            for d in str(s.get("doc_id", "")).split(","):
                if d:
                    assert d in real_docs


def test_raptor_retriever_reads_env(monkeypatch):
    emb = FakeEmbedder()
    tree = build_raptor_tree(_four_chunks(), embedder=emb)
    base = FakeBase([])
    monkeypatch.setenv(RAPTOR_ENV, "on")
    assert isinstance(make_raptor_retriever(base, tree=tree, embedder=emb), RaptorRetriever)
    monkeypatch.setenv(RAPTOR_ENV, "none")
    assert make_raptor_retriever(base, tree=tree, embedder=emb) is base
