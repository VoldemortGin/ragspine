"""ColBERT 晚交互重排（W11）单元测试 + MaxSim 打分 + make_reranker 工厂语义 + 接线 + 真模型 conformance。

设计意图（docs/prd-quality-depth.md W11）：给 ⭐ 检索表示补一档 token 级晚交互——一个轻量、确定性、
permissive-license（fastembed，Apache-2.0；默认 colbert-ir/colbertv2.0，Apache-2.0）的本地 ColBERT
重排后端，实现既有 ListwiseJudge 协议（judge(query,candidates)->名次），注册进 make_reranker，可由
config 字符串选用；默认（"none"）行为不变（仍是 identity/RRF 或注入的 LLM judge）——字节不变，opt-in。

落地方式：ColBERT 作【重排器】（对 base 检索候选做 MaxSim 重打分，对标 LlamaIndex ColbertRerank），
最小改动、复用 W2 的 listwise_rerank 编排缝与 make_reranker 工厂，不需多向量索引（多向量检索后端 = follow-up）。

红色策略 / 离线性（同 W1/W2）：
- 单测一律用注入的 fake fastembed（fake_colbert 夹具，sys.modules 替身），零网络、零真装；
- 构造惰性：不装 fastembed 也能构造 ColbertReranker（模型在首次 judge 时才加载）；
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
from ragspine.retrieval.rerank.colbert import (
    DEFAULT_COLBERT_MODEL,
    ColbertReranker,
    maxsim,
)
from ragspine.retrieval.rerank.cross_encoder import make_reranker
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge


# ---------------------------------------------------------------------------
# maxsim：晚交互打分纯函数（sum over query tokens of max cosine to any doc token）
# ---------------------------------------------------------------------------

def test_maxsim_sum_of_max_cosine():
    """MaxSim = 逐 query token 取其对任一 doc token 的最大 cosine 之和。"""
    q = [[1.0, 0.0], [0.0, 1.0]]
    d = [[1.0, 0.0]]
    # q0 对 {[1,0]} 最大 cos=1；q1 对 {[1,0]} 最大 cos=0 -> 1.0
    assert maxsim(q, d) == pytest.approx(1.0)


def test_maxsim_all_tokens_matched():
    """两 query token 各在 doc 中找到正交匹配 -> 2.0。"""
    q = [[1.0, 0.0], [0.0, 1.0]]
    d = [[1.0, 0.0], [0.0, 1.0]]
    assert maxsim(q, d) == pytest.approx(2.0)


def test_maxsim_picks_best_doc_token():
    """单 query token 对多 doc token 取最大 -> 1.0（而非求和/求平均）。"""
    assert maxsim([[1.0, 0.0]], [[1.0, 0.0], [0.0, 1.0]]) == pytest.approx(1.0)


def test_maxsim_empty_returns_zero():
    """空 query 或空 doc token 矩阵 -> 0.0（不抛）。"""
    assert maxsim([], [[1.0, 0.0]]) == 0.0
    assert maxsim([[1.0, 0.0]], []) == 0.0


def test_maxsim_zero_vector_is_zero_cosine():
    """零向量 cosine 一律 0（口径同 retrieval.cosine_similarity）。"""
    assert maxsim([[0.0, 0.0]], [[1.0, 0.0]]) == 0.0


# ---------------------------------------------------------------------------
# 惰性构造 / 友好报错 / 参数校验
# ---------------------------------------------------------------------------

def test_ctor_is_lazy_no_fastembed_needed(monkeypatch):
    """构造惰性：模拟未装 fastembed 也能构造（模型在 judge 时才加载）。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    r = ColbertReranker()
    assert r.model_name == DEFAULT_COLBERT_MODEL


def test_judge_without_fastembed_raises_friendly(monkeypatch):
    """首次 judge 时缺 fastembed -> 友好提示（含 fastembed / colbert extra 名）。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    with pytest.raises(ImportError) as exc:
        ColbertReranker().judge("q", ["a"])
    msg = str(exc.value).lower()
    assert "fastembed" in msg
    assert "colbert" in msg


def test_empty_candidates_no_model_load(monkeypatch):
    """空候选直接返回 []，不触发任何 import / 模型加载。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)  # 若误加载会抛
    assert ColbertReranker().judge("q", []) == []


def test_default_model_constant_centralized():
    """默认模型名集中一处，且为 permissive-license 的 ColBERTv2（Apache-2.0，过 ADR 0009 门）。"""
    assert DEFAULT_COLBERT_MODEL == "colbert-ir/colbertv2.0"


def test_invalid_batch_size_rejected():
    """batch_size < 1 -> ValueError（构造期即拒，不延迟）。"""
    with pytest.raises(ValueError):
        ColbertReranker(batch_size=0)


def test_implements_listwise_judge_protocol():
    """ColbertReranker 满足 @runtime_checkable ListwiseJudge 协议（有 judge 方法）。"""
    assert isinstance(ColbertReranker(), ListwiseJudge)


# ---------------------------------------------------------------------------
# judge：MaxSim 打分 -> 名次（降序 / 平分稳定 / 确定性 / 校验 / 透传）
# ---------------------------------------------------------------------------

def test_judge_orders_by_maxsim_descending(fake_colbert):
    """晚交互打分降序给名次：query 'a b'，候选按共享字母词数排序。

    fake：token 首字母 one-hot -> MaxSim(query, doc)=doc 命中的 query 字母数。
    'a b' vs ['a b'(2) / 'a'(1) / 'z'(0)] -> 名次 [0,1,2]。"""
    fake_colbert()
    assert ColbertReranker().judge("a b", ["a b", "a", "z"]) == [0, 1, 2]


def test_judge_reranks_relevant_first(fake_colbert):
    """强相关候选（更多 query token 命中）被重排到最前——真晚交互重排。"""
    fake_colbert()
    # 'x'(0) 无关在首位，'a b c'(命中 a,b -> 2) 在末位；重排后强相关到第一。
    order = ColbertReranker().judge("a b", ["x", "a", "a b c"])
    assert order[0] == 2


def test_judge_ties_keep_input_order(fake_colbert):
    """平分时按原（RRF）序——稳定排序，确定性。"""
    fake_colbert()
    assert ColbertReranker().judge("a", ["a", "a", "a"]) == [0, 1, 2]


def test_judge_deterministic_two_instances(fake_colbert):
    """同输入两个独立实例给出完全一致的名次（确定性 conformance，fake 层面）。"""
    fake_colbert()
    a = ColbertReranker().judge("a b", ["a", "a b", "z"])
    b = ColbertReranker().judge("a b", ["a", "a b", "z"])
    assert a == b == [1, 0, 2]


def test_judge_returns_permutation(fake_colbert):
    """名次恒为 0..n-1 的一个排列（喂给 listwise_rerank 回填位置的契约）。"""
    fake_colbert()
    order = ColbertReranker().judge("a b", ["a", "b", "a b", "z"])
    assert sorted(order) == [0, 1, 2, 3]


def test_doc_count_mismatch_raises(fake_colbert):
    """embed 返回条数与候选不一致 -> 抛错（绝不静默给坏名次）。"""
    fake_colbert(drop_last=True)
    with pytest.raises((RuntimeError, ValueError)):
        ColbertReranker().judge("a", ["a", "b"])


def test_model_override_passed(fake_colbert):
    """model_name 可覆盖，并透传给 LateInteractionTextEmbedding。"""
    captured = fake_colbert()
    ColbertReranker(model_name="answerdotai/answerai-colbert-small-v1").judge("a", ["a"])
    assert captured["init_kwargs"]["model_name"] == "answerdotai/answerai-colbert-small-v1"


def test_cache_dir_threads_passthrough(fake_colbert):
    """cache_dir / threads 透传给 LateInteractionTextEmbedding（缺省不传，给了才传）。"""
    captured = fake_colbert()
    ColbertReranker(cache_dir="/tmp/cb", threads=2).judge("a", ["a"])
    assert captured["init_kwargs"]["cache_dir"] == "/tmp/cb"
    assert captured["init_kwargs"]["threads"] == 2


def test_model_loaded_once_and_cached(fake_colbert):
    """模型延迟加载且只构造一次（跨多次 judge 复用同一后端）。"""
    fake_colbert()
    fake_mod = sys.modules["fastembed"]
    calls = {"n": 0}
    orig = fake_mod.LateInteractionTextEmbedding

    class _Counting(orig):
        def __init__(self, *a, **k):
            calls["n"] += 1
            super().__init__(*a, **k)

    fake_mod.LateInteractionTextEmbedding = _Counting
    r = ColbertReranker()
    r.judge("a", ["a"])
    r.judge("b", ["b"])
    assert calls["n"] == 1


def test_query_uses_query_embed(fake_colbert):
    """query 走 query_embed（ColBERT query/doc 前缀有别），候选走 embed。"""
    captured = fake_colbert()
    ColbertReranker().judge("a b", ["a", "b"])
    assert captured["query_calls"] == ["a b"]
    assert captured["embed_calls"] == [["a", "b"]]


# ---------------------------------------------------------------------------
# 工厂 make_reranker：colbert 别名（复用 W2 reranker 缝）+ none/auto 不受影响
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", ["colbert", "colbertv2", "late_interaction", "COLBERT", " colbert "])
def test_factory_aliases_return_instance(spec):
    """'colbert' 及其别名（含大小写/留白归一）-> ColbertReranker（构造惰性）。"""
    assert isinstance(make_reranker(spec), ColbertReranker)


def test_factory_none_unaffected():
    """回归：注册 colbert 后 None / 'none' 仍 -> None（默认不重排，字节不变，opt-in 不污染默认）。"""
    assert make_reranker(None) is None
    assert make_reranker("none") is None


def test_factory_auto_still_cross_encoder(fake_colbert):
    """'auto' 仍解析为本地 cross-encoder（默认本地重排大脑），colbert 是显式命名 opt-in。"""
    from ragspine.retrieval.rerank.cross_encoder import CrossEncoderReranker

    fake_colbert()  # 装了 fastembed（探测可导入）
    assert isinstance(make_reranker("auto"), CrossEncoderReranker)


def test_factory_model_override_via_env(fake_colbert, monkeypatch):
    """缺省模型时读 RAGSPINE_COLBERT_MODEL 覆盖（仅对 colbert 系 spec）。"""
    captured = fake_colbert()
    monkeypatch.setenv("RAGSPINE_COLBERT_MODEL", "answerdotai/answerai-colbert-small-v1")
    make_reranker("colbert").judge("a", ["a"])
    assert captured["init_kwargs"]["model_name"] == "answerdotai/answerai-colbert-small-v1"


def test_factory_via_env(monkeypatch):
    """缺省 spec 读 env RAGSPINE_RERANKER=colbert -> ColbertReranker。"""
    monkeypatch.setenv("RAGSPINE_RERANKER", "colbert")
    assert isinstance(make_reranker(), ColbertReranker)


# ---------------------------------------------------------------------------
# 接线：build_narrative_retriever 的 reranker 注入（默认行为不变）
# ---------------------------------------------------------------------------

def test_wiring_colbert_overrides_provider_judge(tmp_path):
    """注入 ColbertReranker -> 它成为 NarrativeIndex 的 judge（晚交互替代 LLM judge）。"""
    cb = ColbertReranker()
    retriever, store = build_narrative_retriever(
        tmp_path / "c.db", provider=MockProvider(), reranker=cb
    )
    try:
        assert retriever.index.judge is cb
    finally:
        store.close()


def test_wiring_default_keeps_provider_judge(tmp_path):
    """默认（无 reranker）+ 给了 provider -> 仍是 ProviderListwiseJudge（W11 不改默认行为）。"""
    retriever, store = build_narrative_retriever(tmp_path / "c.db", provider=MockProvider())
    try:
        assert isinstance(retriever.index.judge, ProviderListwiseJudge)
    finally:
        store.close()


def test_service_config_reranker_default_is_none():
    """ServiceConfig.reranker 默认 'none'：默认 loop 不接 ColBERT（opt-in，字节不变）。"""
    from ragspine.service.config import ServiceConfig

    assert ServiceConfig(db_path="x").reranker == "none"
    assert make_reranker("none") is None


# ---------------------------------------------------------------------------
# 真模型确定性 + 相关性 conformance（联网首拉，CI 默认 `-m "not network"` 跳过）
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_colbert_real_deterministic_and_relevant():
    """真 ColBERT：同输入逐位一致（确定性）+ 相关候选名次靠前（真晚交互重排）。

    联网首次下载权重再缓存；pin 模型 + fastembed 版本保证 CPU onnxruntime 逐位可复现。
    """
    import warnings

    pytest.importorskip("fastembed", reason="fastembed 未装（pip install ragspine[colbert]）")

    query = "How does late interaction retrieval work?"
    docs = [
        "A recipe for chocolate cake with butter and sugar.",  # 无关
        "ColBERT scores query and document tokens by late interaction MaxSim.",  # 强相关
        "Weekend cricket match report.",  # 无关
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # fastembed 第三方 UserWarning，与确定性无关
        r1 = ColbertReranker().judge(query, docs)
        r2 = ColbertReranker().judge(query, docs)

    assert r1 == r2          # 确定性：两个独立实例同输入逐位一致
    assert r1[0] == 1        # 真重排：强相关候选被排到第一
