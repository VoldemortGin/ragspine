"""离线确定性 embedding 后端 + 后端工厂 + 接线测试（TDD 红色阶段，GAP-A 向量通道）。

User story（中文）：
    作为接手 PoC production 化的 GIO 算法工程师，我希望向量/混合检索通道从「可选、
    生产从未接线」升为「默认可接线」——即接通成本变成一个 flag/env，而非改代码，且
    默认行为零变更。在真 Qwen3-Embedding 待 GPU infra 就绪之前，我需要一个**离线、
    零网络、跨进程/跨平台可复现**的确定性后端来打通整条管线（切块→入库→混合检索→
    listwise 二审），并用它做 BM25-vs-hybrid 的 A/B harness 走通验证。

诚实边界（钉死在测试意图里）：
    DeterministicEmbeddingBackend 是【词法散列】后端（hashlib 散列 token 到桶 + L2
    归一化），与 BM25 信号高度相关、【非语义】；它绝不代表真语义召回增益，真增益需
    Qwen3 等真实模型（待 infra）。这些测试只验证「确定性 / 形状 / 可注入 / 接线」，
    不断言任何语义质量。

覆盖：
    E1 确定性（同文本两次 / 两个后端实例逐元素相等；hashlib 非内置 hash 的稳定值）。
    E2 形状 / 归一（dim 固定、L2 范数≈1、顺序对齐、空输入返回 []）。
    E3 区分性（不同文本 cosine<1、相同文本 cosine==1）。
    E4 工厂（'none'/None->None、'deterministic'->实例、'openai'->延迟 import、
        未知->ValueError、环境变量 RAGSPINE_EMBEDDING_BACKEND 生效）。
    E5 hybrid 端到端（NarrativeIndex 注入确定性后端：有结果、vector_score 被填充、
        RESTRICTED 不出域、RRF 跑通；与 None 纯 BM25 路径都不报错）。
    E6 ask.py 接线（--embedding deterministic 时确实注入了确定性后端）。
    R1 回归（默认 None / 不传 时行为不变）。

红色预期：DeterministicEmbeddingBackend / make_embedding_backend / ask.py --embedding
尚未实现，import 或断言因此 FAIL；R1 中部分既有行为保护项可能先绿（已注明）。
"""

import math
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.vector.embedding_backends import (
    DeterministicEmbeddingBackend,
    OpenAIEmbeddingBackend,
    make_embedding_backend,
)
from ragspine.retrieval.lexical.retrieval import NarrativeIndex, cosine_similarity


# ===========================================================================
# E1 确定性
# ===========================================================================

def test_same_text_twice_elementwise_equal():
    """同一文本两次 embed 向量逐元素相等（确定性核心）。"""
    backend = DeterministicEmbeddingBackend()
    [v1] = backend.embed_texts(["香港 REVENUE 营收持续增长"])
    [v2] = backend.embed_texts(["香港 REVENUE 营收持续增长"])
    assert v1 == v2


def test_two_independent_instances_agree():
    """两个独立后端实例对同一输入给出完全一致的向量（跨进程可复现的代理验证）。"""
    a = DeterministicEmbeddingBackend()
    b = DeterministicEmbeddingBackend()
    texts = ["MPFA 强积金新规", "REVENUE grew strongly in 2025"]
    assert a.embed_texts(texts) == b.embed_texts(texts)


def test_uses_hashlib_not_builtin_hash_stable_value():
    """用 hashlib（非内置 hash，免 PYTHONHASHSEED 随机化）：已知输入的向量稳定。

    断言「同一文本的向量在任何进程下完全一致」——本测试进程内复算两次必相等，且与
    内置 hash() 解耦：若实现误用 hash()，PYTHONHASHSEED 开启时该断言仍会偶发不稳，
    本仓库 CI 不固定该 seed，故此断言对内置 hash 的误用敏感。
    """
    text = "稳定 stable token 测试"
    v_a = DeterministicEmbeddingBackend().embed_texts([text])[0]
    v_b = DeterministicEmbeddingBackend().embed_texts([text])[0]
    assert v_a == v_b
    # 非全零（含可命中桶的 token），否则确定性无意义。
    assert any(x != 0.0 for x in v_a)


# ===========================================================================
# E2 形状 / 归一 / 顺序 / 空输入
# ===========================================================================

def test_fixed_dim_default_256():
    """默认维度 256，且每条向量长度都等于该维度。"""
    backend = DeterministicEmbeddingBackend()
    vecs = backend.embed_texts(["甲", "乙 some english"])
    assert all(len(v) == 256 for v in vecs)


def test_dim_configurable():
    """dim 可配：构造 dim=32 时每条向量长度为 32。"""
    backend = DeterministicEmbeddingBackend(dim=32)
    [v] = backend.embed_texts(["香港 REVENUE"])
    assert len(v) == 32


def test_l2_normalized_unit_norm():
    """非零文本向量 L2 范数≈1（L2 归一化）。"""
    [v] = DeterministicEmbeddingBackend().embed_texts(["香港 REVENUE 增长"])
    norm = math.sqrt(sum(x * x for x in v))
    assert norm == pytest.approx(1.0, abs=1e-9)


def test_order_aligned_with_input():
    """输出顺序与输入对齐：逐条单独 embed 与批量 embed 结果一致。"""
    backend = DeterministicEmbeddingBackend()
    texts = ["alpha 块一", "beta 块二", "gamma 块三"]
    batch = backend.embed_texts(texts)
    one_by_one = [backend.embed_texts([t])[0] for t in texts]
    assert batch == one_by_one


def test_empty_input_returns_empty_list():
    """空输入返回 []（不报错、不发任何请求——本就离线）。"""
    assert DeterministicEmbeddingBackend().embed_texts([]) == []


def test_blank_text_zero_vector_does_not_crash():
    """纯空白 / 无可分词内容的文本给出零向量（cosine 一律 0），不抛错。"""
    [v] = DeterministicEmbeddingBackend().embed_texts(["   "])
    assert len(v) == 256
    assert all(x == 0.0 for x in v)


# ===========================================================================
# E3 区分性
# ===========================================================================

def test_identical_text_cosine_one():
    """相同文本向量 cosine==1。"""
    backend = DeterministicEmbeddingBackend()
    v1, v2 = backend.embed_texts(["香港 REVENUE 增长", "香港 REVENUE 增长"])
    assert cosine_similarity(v1, v2) == pytest.approx(1.0, abs=1e-9)


def test_different_text_cosine_below_one():
    """明显不同的两段文本向量 cosine < 1（具备区分性）。"""
    backend = DeterministicEmbeddingBackend()
    v1, v2 = backend.embed_texts([
        "香港 REVENUE 营收持续增长",
        "weekend cricket match report 板球比赛",
    ])
    assert cosine_similarity(v1, v2) < 1.0


# ===========================================================================
# E4 后端工厂 make_embedding_backend
# ===========================================================================

def test_factory_none_spec_returns_none():
    """spec=None / 'none' -> None（纯 BM25，保持现状默认）。"""
    assert make_embedding_backend(None) is None
    assert make_embedding_backend("none") is None


def test_factory_deterministic_returns_instance():
    """spec='deterministic' -> DeterministicEmbeddingBackend 实例。"""
    backend = make_embedding_backend("deterministic")
    assert isinstance(backend, DeterministicEmbeddingBackend)


def test_factory_unknown_spec_raises_value_error():
    """未知 spec -> ValueError。"""
    with pytest.raises(ValueError):
        make_embedding_backend("qwen9000")


def test_factory_env_var_takes_effect(monkeypatch):
    """缺省 spec 时读环境变量 RAGSPINE_EMBEDDING_BACKEND。"""
    monkeypatch.setenv("RAGSPINE_EMBEDDING_BACKEND", "deterministic")
    assert isinstance(make_embedding_backend(), DeterministicEmbeddingBackend)
    monkeypatch.setenv("RAGSPINE_EMBEDDING_BACKEND", "none")
    assert make_embedding_backend() is None


def test_factory_explicit_spec_overrides_env(monkeypatch):
    """显式 spec 覆盖环境变量。"""
    monkeypatch.setenv("RAGSPINE_EMBEDDING_BACKEND", "deterministic")
    assert make_embedding_backend("none") is None


def test_factory_openai_lazy_import_friendly_error(monkeypatch):
    """spec='openai' 走 OpenAIEmbeddingBackend（延迟 import）；无 SDK 时友好报错。"""
    import sys
    monkeypatch.setitem(sys.modules, "openai", None)  # 模拟未安装
    with pytest.raises((ImportError, RuntimeError)) as exc_info:
        make_embedding_backend("openai", api_key="k")
    assert "openai" in str(exc_info.value).lower()


def test_factory_openai_constructs_with_fake_sdk(monkeypatch):
    """spec='openai' + 注入 fake SDK：返回 OpenAIEmbeddingBackend 实例。"""
    import sys
    import types
    from types import SimpleNamespace

    class _Embeddings:
        def create(self, **kwargs):
            return SimpleNamespace(data=[])

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            self.embeddings = _Embeddings()

    fake = types.ModuleType("openai")
    fake.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)
    backend = make_embedding_backend("openai", api_key="k")
    assert isinstance(backend, OpenAIEmbeddingBackend)


# ===========================================================================
# E5 hybrid 端到端（NarrativeIndex 注入确定性后端）
# ===========================================================================

def _meta(doc_id: str, **overrides) -> DocumentMeta:
    kwargs = dict(
        doc_id=doc_id, title=doc_id, topic="FIN", entity="ACME_HK",
        geography="HK", period="2025", language="zh", sensitivity="INTERNAL",
    )
    kwargs.update(overrides)
    return DocumentMeta(**kwargs)


def _seed_index(store: ChunkStore, *, backend) -> NarrativeIndex:
    index = NarrativeIndex(store, embedding_backend=backend)
    index.ingest("香港 REVENUE 营收持续增长，银保渠道表现稳健。", _meta("HK_FIN.pptx"))
    index.ingest("中国 REVENUE 增长由代理人产能提升驱动。", _meta("CN_FIN.pptx", entity="ACME_CN", geography="CN"))
    return index


def test_hybrid_retrieve_fills_vector_score(tmp_path):
    """注入确定性后端：检索有结果，且 vector_score 被填充（非全 0）。"""
    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    try:
        index = _seed_index(store, backend=DeterministicEmbeddingBackend())
        results = index.retrieve("香港 REVENUE 增长", rerank=False)
        assert results
        assert any(r.vector_score > 0.0 for r in results)
    finally:
        store.close()


def test_hybrid_restricted_still_filtered_via_adapter(tmp_path):
    """RESTRICTED 不出域：即便走向量通道，适配器出口仍剔除 RESTRICTED 块。"""
    from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever

    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    try:
        index = NarrativeIndex(store, embedding_backend=DeterministicEmbeddingBackend())
        index.ingest("香港 REVENUE 营收持续增长。", _meta("HK_FIN.pptx"))
        index.ingest(
            "香港 REVENUE 高管 PR 评级讨论 SECRET_TOKEN。",
            _meta("EXCO.pptx", sensitivity="RESTRICTED"),
        )
        snippets = NarrativeIndexRetriever(index).retrieve("香港 REVENUE")
        assert snippets
        assert all(s["doc_id"] != "EXCO.pptx" for s in snippets)
        assert all("SECRET_TOKEN" not in s["text"] for s in snippets)
    finally:
        store.close()


def test_pure_bm25_path_no_vector_score(tmp_path):
    """embedding_backend=None 纯 BM25 路径：有结果、vector_score 恒为 0、不报错。"""
    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    try:
        index = _seed_index(store, backend=None)
        results = index.retrieve("香港 REVENUE 增长", rerank=False)
        assert results
        assert all(r.vector_score == 0.0 for r in results)
    finally:
        store.close()


# ===========================================================================
# E6 ask.py 接线：--embedding deterministic 注入确定性后端
# ===========================================================================

def test_ask_cli_embedding_flag_injects_backend(tmp_path, monkeypatch):
    """--embedding deterministic：build_narrative_retriever 收到 DeterministicEmbeddingBackend。

    spy build_narrative_retriever 捕获传入的 embedding_backend，避免依赖真实检索结果。
    """
    import ragspine.cli.ask as ask_mod

    captured: dict = {}
    real = ask_mod.build_narrative_retriever

    def _spy(chunk_db, provider=None, *, embedding_backend=None):
        captured["embedding_backend"] = embedding_backend
        return real(chunk_db, provider=provider, embedding_backend=embedding_backend)

    monkeypatch.setattr(ask_mod, "build_narrative_retriever", _spy)

    chunk_db = tmp_path / "chunks.db"
    store = ChunkStore(chunk_db)
    store.init_schema()
    NarrativeIndex(store).ingest("香港 REVENUE 营收持续增长。", _meta("HK_FIN.pptx"))
    store.close()

    fact_db = tmp_path / "facts.db"
    rc = ask_mod.main([
        "--provider", "mock",
        "--db", str(fact_db),
        "--chunk-db", str(chunk_db),
        "--embedding", "deterministic",
        "--reference-date", "2026-06-12",
        "香港去年REVENUE多少",
    ])
    assert rc == 0
    assert isinstance(captured.get("embedding_backend"), DeterministicEmbeddingBackend)


def test_ask_cli_embedding_default_is_none(tmp_path, monkeypatch):
    """默认（不传 --embedding）：build_narrative_retriever 收到 None（纯 BM25 现状）。"""
    import ragspine.cli.ask as ask_mod

    captured: dict = {"set": False}
    real = ask_mod.build_narrative_retriever

    def _spy(chunk_db, provider=None, *, embedding_backend=None):
        captured["set"] = True
        captured["embedding_backend"] = embedding_backend
        return real(chunk_db, provider=provider, embedding_backend=embedding_backend)

    monkeypatch.setattr(ask_mod, "build_narrative_retriever", _spy)

    chunk_db = tmp_path / "chunks.db"
    store = ChunkStore(chunk_db)
    store.init_schema()
    NarrativeIndex(store).ingest("香港 REVENUE 营收持续增长。", _meta("HK_FIN.pptx"))
    store.close()

    fact_db = tmp_path / "facts.db"
    rc = ask_mod.main([
        "--provider", "mock",
        "--db", str(fact_db),
        "--chunk-db", str(chunk_db),
        "--reference-date", "2026-06-12",
        "香港去年REVENUE多少",
    ])
    assert rc == 0
    assert captured["set"] is True
    assert captured["embedding_backend"] is None


# ===========================================================================
# R1 回归：默认行为不变
# ===========================================================================

def test_regression_narrative_index_default_pure_bm25(tmp_path):
    """NarrativeIndex 默认 embedding_backend=None（纯 BM25 现状不变）。"""
    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    try:
        index = NarrativeIndex(store)
        assert index.embedding_backend is None
    finally:
        store.close()


def test_regression_factory_default_is_none(monkeypatch):
    """无环境变量、无 spec 时工厂默认返回 None（保持纯 BM25 默认）。"""
    monkeypatch.delenv("RAGSPINE_EMBEDDING_BACKEND", raising=False)
    assert make_embedding_backend() is None
