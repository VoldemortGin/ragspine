"""SPLADE 学习稀疏重排（W11）单元测试 + 稀疏点积打分 + make_reranker 工厂语义 + 接线 + 真模型 conformance。

设计意图（docs/prd-quality-depth.md W11）：给 ⭐ 检索表示补一档神经学习稀疏——一个轻量、确定性、
permissive-license（fastembed，Apache-2.0；默认 prithivida/Splade_PP_en_v1，Apache-2.0）的本地 SPLADE
稀疏打分后端，实现既有 ListwiseJudge 协议（judge(query,candidates)->名次），注册进 make_reranker，可由
config 字符串选用；默认（"none"）行为不变（仍是 identity/RRF 或注入的 LLM judge）——字节不变，opt-in。

落地方式：SPLADE 作【稀疏打分信号】重排候选（query 稀疏向量 vs 候选稀疏向量点积，比 BM25 强且可解释）——
最小改动、复用 W2 的 listwise_rerank 编排缝与 make_reranker 工厂，不需稀疏倒排索引（稀疏检索后端 = follow-up）。

红色策略 / 离线性（同 W1/W2）：
- 单测一律用注入的 fake fastembed（fake_splade 夹具，sys.modules 替身），零网络、零真装；
- 构造惰性：不装 fastembed 也能构造 SpladeReranker（模型在首次 judge 时才加载）；
- 真模型的确定性 + 相关性只在 @pytest.mark.network 用例里跑（首拉后离线），CI 默认 `-m "not network"` 跳过。
"""

import os
import sys

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.retrieval.link.narrative_link import (
    ProviderListwiseJudge,
    build_narrative_retriever,
)
from ragspine.retrieval.rerank.cross_encoder import make_reranker
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge
from ragspine.retrieval.rerank.splade import (
    DEFAULT_SPLADE_MODEL,
    SpladeReranker,
    sparse_dot,
)


# ---------------------------------------------------------------------------
# sparse_dot：学习稀疏打分纯函数（共享维度点积）
# ---------------------------------------------------------------------------

def test_sparse_dot_shared_dimensions():
    """稀疏点积 = 两向量共享 term 维度上 value 乘积之和。"""
    q = {1: 0.5, 2: 0.3}
    d = {2: 2.0, 3: 1.0}
    assert sparse_dot(q, d) == pytest.approx(0.6)  # 仅共享维度 2：0.3*2.0


def test_sparse_dot_disjoint_is_zero():
    """无共享维度 -> 0.0。"""
    assert sparse_dot({1: 1.0}, {2: 1.0}) == 0.0


def test_sparse_dot_empty_is_zero():
    """任一空稀疏向量 -> 0.0。"""
    assert sparse_dot({}, {1: 1.0}) == 0.0
    assert sparse_dot({1: 1.0}, {}) == 0.0


def test_sparse_dot_symmetric():
    """点积对称：sparse_dot(a,b) == sparse_dot(b,a)（迭代较小者不改结果）。"""
    a = {1: 0.5, 2: 0.3, 5: 0.9}
    b = {2: 2.0, 5: 0.1}
    assert sparse_dot(a, b) == pytest.approx(sparse_dot(b, a))


# ---------------------------------------------------------------------------
# 惰性构造 / 友好报错 / 参数校验
# ---------------------------------------------------------------------------

def test_ctor_is_lazy_no_fastembed_needed(monkeypatch):
    """构造惰性：模拟未装 fastembed 也能构造（模型在 judge 时才加载）。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    r = SpladeReranker()
    assert r.model_name == DEFAULT_SPLADE_MODEL


def test_judge_without_fastembed_raises_friendly(monkeypatch):
    """首次 judge 时缺 fastembed -> 友好提示（含 fastembed / splade extra 名）。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    with pytest.raises(ImportError) as exc:
        SpladeReranker().judge("q", ["a"])
    msg = str(exc.value).lower()
    assert "fastembed" in msg
    assert "splade" in msg


def test_empty_candidates_no_model_load(monkeypatch):
    """空候选直接返回 []，不触发任何 import / 模型加载。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)  # 若误加载会抛
    assert SpladeReranker().judge("q", []) == []


def test_default_model_constant_centralized():
    """默认模型名集中一处，且为 permissive-license 的 Splade++（Apache-2.0，过 ADR 0009 门）。"""
    assert DEFAULT_SPLADE_MODEL == "prithivida/Splade_PP_en_v1"


def test_invalid_batch_size_rejected():
    """batch_size < 1 -> ValueError（构造期即拒，不延迟）。"""
    with pytest.raises(ValueError):
        SpladeReranker(batch_size=0)


def test_implements_listwise_judge_protocol():
    """SpladeReranker 满足 @runtime_checkable ListwiseJudge 协议（有 judge 方法）。"""
    assert isinstance(SpladeReranker(), ListwiseJudge)


# ---------------------------------------------------------------------------
# judge：稀疏点积打分 -> 名次（降序 / 平分稳定 / 确定性 / 校验 / 透传）
# ---------------------------------------------------------------------------

def test_judge_orders_by_sparse_dot_descending(fake_splade):
    """稀疏点积降序给名次：query 'a b'，候选按共享 term 加权和排序。

    fake：term id=首字符 ord、value=词频 -> 点积=共享 term 词频乘积和。
    'a b' vs ['a b'(1*1+1*1=2) / 'a'(1) / 'z'(0)] -> 名次 [0,1,2]。"""
    fake_splade()
    assert SpladeReranker().judge("a b", ["a b", "a", "z"]) == [0, 1, 2]


def test_judge_reranks_relevant_first(fake_splade):
    """强相关候选（更多共享 term）被重排到最前——真稀疏打分重排。"""
    fake_splade()
    order = SpladeReranker().judge("a b", ["z", "a", "a b b"])
    assert order[0] == 2  # 'a b b' 共享 a(1)+b(2) 点积最高


def test_judge_ties_keep_input_order(fake_splade):
    """平分时按原（RRF）序——稳定排序，确定性。"""
    fake_splade()
    assert SpladeReranker().judge("a", ["a", "a", "a"]) == [0, 1, 2]


def test_judge_deterministic_two_instances(fake_splade):
    """同输入两个独立实例给出完全一致的名次（确定性 conformance，fake 层面）。"""
    fake_splade()
    a = SpladeReranker().judge("a b", ["a", "a b", "z"])
    b = SpladeReranker().judge("a b", ["a", "a b", "z"])
    assert a == b == [1, 0, 2]


def test_judge_returns_permutation(fake_splade):
    """名次恒为 0..n-1 的一个排列（喂给 listwise_rerank 回填位置的契约）。"""
    fake_splade()
    order = SpladeReranker().judge("a b", ["a", "b", "a b", "z"])
    assert sorted(order) == [0, 1, 2, 3]


def test_doc_count_mismatch_raises(fake_splade):
    """embed 返回条数与候选不一致 -> 抛错（绝不静默给坏名次）。"""
    fake_splade(drop_last=True)
    with pytest.raises((RuntimeError, ValueError)):
        SpladeReranker().judge("a", ["a", "b"])


def test_model_override_passed(fake_splade):
    """model_name 可覆盖，并透传给 SparseTextEmbedding。"""
    captured = fake_splade()
    SpladeReranker(model_name="naver/splade-v3").judge("a", ["a"])
    assert captured["init_kwargs"]["model_name"] == "naver/splade-v3"


def test_cache_dir_threads_passthrough(fake_splade):
    """cache_dir / threads 透传给 SparseTextEmbedding（缺省不传，给了才传）。"""
    captured = fake_splade()
    SpladeReranker(cache_dir="/tmp/sp", threads=2).judge("a", ["a"])
    assert captured["init_kwargs"]["cache_dir"] == "/tmp/sp"
    assert captured["init_kwargs"]["threads"] == 2


def test_model_loaded_once_and_cached(fake_splade):
    """模型延迟加载且只构造一次（跨多次 judge 复用同一后端）。"""
    fake_splade()
    fake_mod = sys.modules["fastembed"]
    calls = {"n": 0}
    orig = fake_mod.SparseTextEmbedding

    class _Counting(orig):
        def __init__(self, *a, **k):
            calls["n"] += 1
            super().__init__(*a, **k)

    fake_mod.SparseTextEmbedding = _Counting
    r = SpladeReranker()
    r.judge("a", ["a"])
    r.judge("b", ["b"])
    assert calls["n"] == 1


def test_query_uses_query_embed(fake_splade):
    """query 走 query_embed，候选走 embed。"""
    captured = fake_splade()
    SpladeReranker().judge("a b", ["a", "b"])
    assert captured["query_calls"] == ["a b"]
    assert captured["embed_calls"] == [["a", "b"]]


# ---------------------------------------------------------------------------
# 工厂 make_reranker：splade 别名（复用 W2 reranker 缝）+ none/auto 不受影响
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", ["splade", "splade_pp", "learned_sparse", "SPLADE", " splade "])
def test_factory_aliases_return_instance(spec):
    """'splade' 及其别名（含大小写/留白归一）-> SpladeReranker（构造惰性）。"""
    assert isinstance(make_reranker(spec), SpladeReranker)


def test_factory_none_unaffected():
    """回归：注册 splade 后 None / 'none' 仍 -> None（默认不重排，字节不变，opt-in 不污染默认）。"""
    assert make_reranker(None) is None
    assert make_reranker("none") is None


def test_factory_model_override_via_env(fake_splade, monkeypatch):
    """缺省模型时读 RAGSPINE_SPLADE_MODEL 覆盖（仅对 splade 系 spec）。"""
    captured = fake_splade()
    monkeypatch.setenv("RAGSPINE_SPLADE_MODEL", "naver/splade-v3")
    make_reranker("splade").judge("a", ["a"])
    assert captured["init_kwargs"]["model_name"] == "naver/splade-v3"


def test_factory_via_env(monkeypatch):
    """缺省 spec 读 env RAGSPINE_RERANKER=splade -> SpladeReranker。"""
    monkeypatch.setenv("RAGSPINE_RERANKER", "splade")
    assert isinstance(make_reranker(), SpladeReranker)


# ---------------------------------------------------------------------------
# 接线：build_narrative_retriever 的 reranker 注入（默认行为不变）
# ---------------------------------------------------------------------------

def test_wiring_splade_overrides_provider_judge(tmp_path):
    """注入 SpladeReranker -> 它成为 NarrativeIndex 的 judge（稀疏打分替代 LLM judge）。"""
    sp = SpladeReranker()
    retriever, store = build_narrative_retriever(
        tmp_path / "c.db", provider=MockProvider(), reranker=sp
    )
    try:
        assert retriever.index.judge is sp
    finally:
        store.close()


def test_wiring_default_keeps_provider_judge(tmp_path):
    """默认（无 reranker）+ 给了 provider -> 仍是 ProviderListwiseJudge（W11 不改默认行为）。"""
    retriever, store = build_narrative_retriever(tmp_path / "c.db", provider=MockProvider())
    try:
        assert isinstance(retriever.index.judge, ProviderListwiseJudge)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 真模型确定性 + 相关性 conformance（联网首拉，CI 默认 `-m "not network"` 跳过）
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_splade_real_deterministic_and_relevant():
    """真 SPLADE：同输入逐位一致（确定性）+ 相关候选名次靠前（真学习稀疏打分）。

    联网首次下载权重再缓存；pin 模型 + fastembed 版本保证 CPU onnxruntime 逐位可复现。
    """
    import warnings

    pytest.importorskip("fastembed", reason="fastembed 未装（pip install ragspine[splade]）")

    query = "revenue growth in Hong Kong"
    docs = [
        "A recipe for chocolate cake.",  # 无关
        "Hong Kong revenue grew strongly this quarter.",  # 强相关
        "Weekend cricket match report.",  # 无关
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # fastembed 第三方 UserWarning，与确定性无关
        r1 = SpladeReranker().judge(query, docs)
        r2 = SpladeReranker().judge(query, docs)

    assert r1 == r2          # 确定性：两个独立实例同输入逐位一致
    assert r1[0] == 1        # 真重排：强相关候选被排到第一
