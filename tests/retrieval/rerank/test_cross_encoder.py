"""本地 cross-encoder 重排（W2）单元测试 + make_reranker 工厂语义 + 接线 + 真模型 conformance。

设计意图（docs/prd-quality-depth.md W2）：给离线 ⭐ 重排阶段一个真正的大脑——加一个轻量、确定性、
permissive-license（fastembed，Apache-2.0；默认 Xenova/ms-marco-MiniLM-L-6-v2，Apache-2.0）的本地
cross-encoder，实现既有 ListwiseJudge 协议（judge(query,candidates)->名次），注册进 make_reranker，
可由 config 字符串选用；默认（"none"）行为不变（仍是 identity/RRF 或注入的 LLM judge）。

红色策略 / 离线性（同 W1）：
- 单测一律用注入的 fake fastembed（fake_cross_encoder 夹具，sys.modules 替身），零网络、零真装；
- 构造惰性：不装 fastembed 也能构造 CrossEncoderReranker（模型在首次 judge 时才加载）；
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
from ragspine.retrieval.rerank.cross_encoder import (
    DEFAULT_CROSS_ENCODER_MODEL,
    CrossEncoderReranker,
    make_reranker,
)
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge


# ---------------------------------------------------------------------------
# 惰性构造 / 友好报错 / 参数校验
# ---------------------------------------------------------------------------

def test_ctor_is_lazy_no_fastembed_needed(monkeypatch):
    """构造惰性：模拟未装 fastembed 也能构造（模型在 judge 时才加载）。"""
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", None)
    r = CrossEncoderReranker()
    assert r.model_name == DEFAULT_CROSS_ENCODER_MODEL


def test_judge_without_fastembed_raises_friendly(monkeypatch):
    """首次 judge 时缺 fastembed -> 友好提示（含 fastembed / rerank extra 名）。"""
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", None)
    with pytest.raises(ImportError) as exc:
        CrossEncoderReranker().judge("q", ["a"])
    msg = str(exc.value).lower()
    assert "fastembed" in msg
    assert "rerank" in msg


def test_empty_candidates_no_model_load(monkeypatch):
    """空候选直接返回 []，不触发任何 import / 模型加载。"""
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", None)  # 若误加载会抛
    assert CrossEncoderReranker().judge("q", []) == []


def test_default_model_constant_centralized():
    """默认模型名集中一处，且为 permissive-license 的小 ms-marco（Apache-2.0，过 ADR 0009 门）。"""
    assert DEFAULT_CROSS_ENCODER_MODEL == "Xenova/ms-marco-MiniLM-L-6-v2"


def test_invalid_batch_size_rejected():
    """batch_size < 1 -> ValueError（构造期即拒，不延迟）。"""
    with pytest.raises(ValueError):
        CrossEncoderReranker(batch_size=0)


def test_implements_listwise_judge_protocol():
    """CrossEncoderReranker 满足 @runtime_checkable ListwiseJudge 协议（有 judge 方法）。"""
    assert isinstance(CrossEncoderReranker(), ListwiseJudge)


# ---------------------------------------------------------------------------
# judge：打分 -> 名次（降序 / 平分稳定 / 确定性 / 校验 / 透传）
# ---------------------------------------------------------------------------

def test_judge_orders_by_score_descending(fake_cross_encoder):
    """分数降序给名次：scores=[0.1,9.0,5.0] -> 名次 [1,2,0]。"""
    fake_cross_encoder(score_fn=lambda docs: [0.1, 9.0, 5.0])
    assert CrossEncoderReranker().judge("q", ["a", "b", "c"]) == [1, 2, 0]


def test_judge_ties_keep_input_order(fake_cross_encoder):
    """平分时按原（RRF）序——稳定排序，确定性。"""
    fake_cross_encoder(score_fn=lambda docs: [1.0, 1.0, 1.0])
    assert CrossEncoderReranker().judge("q", ["a", "b", "c"]) == [0, 1, 2]


def test_judge_deterministic_two_instances(fake_cross_encoder):
    """同输入两个独立实例给出完全一致的名次（确定性 conformance，fake 层面）。"""
    fake_cross_encoder(score_fn=lambda docs: [0.3, 0.9, 0.1])
    a = CrossEncoderReranker().judge("q", ["a", "b", "c"])
    b = CrossEncoderReranker().judge("q", ["a", "b", "c"])
    assert a == b == [1, 0, 2]


def test_judge_returns_permutation(fake_cross_encoder):
    """名次恒为 0..n-1 的一个排列（喂给 listwise_rerank 回填位置的契约）。"""
    fake_cross_encoder(score_fn=lambda docs: [5.0, 2.0, 9.0, 1.0])
    order = CrossEncoderReranker().judge("q", ["a", "b", "c", "d"])
    assert sorted(order) == [0, 1, 2, 3]


def test_score_count_mismatch_raises(fake_cross_encoder):
    """rerank 返回分数条数与候选不一致 -> 抛错（绝不静默给坏名次）。"""
    fake_cross_encoder(drop_last=True)
    with pytest.raises((RuntimeError, ValueError)):
        CrossEncoderReranker().judge("q", ["a", "b"])


def test_model_override_passed_to_cross_encoder(fake_cross_encoder):
    """model_name 可覆盖，并透传给 TextCrossEncoder。"""
    captured = fake_cross_encoder()
    CrossEncoderReranker(model_name="BAAI/bge-reranker-base").judge("q", ["a"])
    assert captured["init_kwargs"]["model_name"] == "BAAI/bge-reranker-base"


def test_cache_dir_threads_passthrough(fake_cross_encoder):
    """cache_dir / threads 透传给 TextCrossEncoder（缺省不传，给了才传）。"""
    captured = fake_cross_encoder()
    CrossEncoderReranker(cache_dir="/tmp/ce", threads=2).judge("q", ["a"])
    assert captured["init_kwargs"]["cache_dir"] == "/tmp/ce"
    assert captured["init_kwargs"]["threads"] == 2


def test_batch_size_passed_to_rerank(fake_cross_encoder):
    """batch_size 透传给 rerank 调用。"""
    captured = fake_cross_encoder()
    CrossEncoderReranker(batch_size=8).judge("q", ["a", "b"])
    assert captured["rerank_calls"][0].get("batch_size") == 8


def test_model_loaded_once_and_cached(fake_cross_encoder):
    """模型延迟加载且只构造一次（跨多次 judge 复用同一 TextCrossEncoder）。"""
    captured = fake_cross_encoder()
    fake_mod = sys.modules["fastembed.rerank.cross_encoder"]
    calls = {"n": 0}
    orig = fake_mod.TextCrossEncoder

    class _Counting(orig):
        def __init__(self, *a, **k):
            calls["n"] += 1
            super().__init__(*a, **k)

    fake_mod.TextCrossEncoder = _Counting
    r = CrossEncoderReranker()
    r.judge("q", ["a"])
    r.judge("q", ["b"])
    assert calls["n"] == 1
    assert captured["rerank_calls"]  # rerank 实际被调过


# ---------------------------------------------------------------------------
# 工厂 make_reranker：别名 + none + auto 语义 + env 覆盖
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", ["cross_encoder", "cross-encoder", "CROSS_ENCODER", " ce ", "ms_marco"])
def test_factory_aliases_return_instance(spec):
    """'cross_encoder' 及其别名（含大小写/留白/连字符归一）-> CrossEncoderReranker（构造惰性）。"""
    assert isinstance(make_reranker(spec), CrossEncoderReranker)


def test_factory_none_returns_none():
    """回归：None / 'none' -> None（默认不重排，行为不变，opt-in 不污染默认）。"""
    assert make_reranker(None) is None
    assert make_reranker("none") is None


def test_factory_auto_returns_ce_when_fastembed_present(fake_cross_encoder):
    """'auto' + fastembed 可导入 -> CrossEncoderReranker。"""
    fake_cross_encoder()
    assert isinstance(make_reranker("auto"), CrossEncoderReranker)


def test_factory_auto_falls_back_to_none_when_absent(monkeypatch):
    """'auto' + 未装 fastembed -> None（回落不重排，行为不变）。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    assert make_reranker("auto") is None


def test_factory_auto_via_env(fake_cross_encoder, monkeypatch):
    """缺省 spec 读 env=auto：fastembed 在场 -> 实例。"""
    fake_cross_encoder()
    monkeypatch.setenv("RAGSPINE_RERANKER", "auto")
    assert isinstance(make_reranker(), CrossEncoderReranker)


def test_factory_model_override_via_env(fake_cross_encoder, monkeypatch):
    """缺省模型时读 RAGSPINE_CROSS_ENCODER_MODEL 覆盖（仅对 cross_encoder 系 spec）。"""
    captured = fake_cross_encoder()
    monkeypatch.setenv("RAGSPINE_CROSS_ENCODER_MODEL", "BAAI/bge-reranker-base")
    make_reranker("cross_encoder").judge("q", ["a"])
    assert captured["init_kwargs"]["model_name"] == "BAAI/bge-reranker-base"


def test_factory_unknown_spec_raises():
    """未知 spec -> ValueError（Registry 列清可用名）。"""
    with pytest.raises(ValueError):
        make_reranker("definitely-not-a-reranker")


# ---------------------------------------------------------------------------
# 接线：build_narrative_retriever 的 reranker 注入（默认行为不变）
# ---------------------------------------------------------------------------

def test_wiring_reranker_overrides_provider_judge(tmp_path):
    """注入 reranker -> 它成为 NarrativeIndex 的 judge（本地大脑替代 LLM judge）。"""
    ce = CrossEncoderReranker()
    retriever, store = build_narrative_retriever(
        tmp_path / "c.db", provider=MockProvider(), reranker=ce
    )
    try:
        assert retriever.index.judge is ce
    finally:
        store.close()


def test_wiring_default_keeps_provider_judge(tmp_path):
    """默认（无 reranker）+ 给了 provider -> 仍是 ProviderListwiseJudge（行为不变）。"""
    retriever, store = build_narrative_retriever(tmp_path / "c.db", provider=MockProvider())
    try:
        assert isinstance(retriever.index.judge, ProviderListwiseJudge)
    finally:
        store.close()


def test_wiring_no_provider_no_reranker_judge_none(tmp_path):
    """默认（无 reranker、无 provider）-> judge 为 None（identity/RRF 退化，行为不变）。"""
    retriever, store = build_narrative_retriever(tmp_path / "c.db")
    try:
        assert retriever.index.judge is None
    finally:
        store.close()


def test_service_config_reranker_default_is_none():
    """ServiceConfig.reranker 默认 'none'：默认 loop 不接 cross-encoder（opt-in，字节不变）。"""
    from ragspine.service.config import ServiceConfig

    assert ServiceConfig(db_path="x").reranker == "none"
    assert make_reranker("none") is None


# ---------------------------------------------------------------------------
# 真模型确定性 + 相关性 conformance（联网首拉，CI 默认 `-m "not network"` 跳过）
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_cross_encoder_real_deterministic_and_relevant():
    """真 cross-encoder：同输入逐位一致（确定性）+ 相关候选名次靠前（真重排）。

    联网首次下载权重再缓存；pin 模型 + fastembed 版本保证 CPU onnxruntime 逐位可复现。
    """
    import warnings

    pytest.importorskip("fastembed", reason="fastembed 未装（pip install ragspine[rerank]）")

    query = "Hong Kong revenue growth"
    docs = [
        "Weekend cricket match report 板球比赛。",                          # 无关
        "Hong Kong revenue grew strongly this quarter, led by the agency channel.",  # 强相关
        "A recipe for chocolate cake.",                                    # 无关
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # fastembed 第三方 UserWarning，与确定性无关
        r1 = CrossEncoderReranker().judge(query, docs)
        r2 = CrossEncoderReranker().judge(query, docs)

    assert r1 == r2          # 确定性：两个独立实例同输入逐位一致
    assert r1[0] == 1        # 真重排：强相关候选被排到第一
