"""OnnxEmbeddingBackend（真语义 ONNX 句向量后端，fastembed 运行时）测试 + auto 工厂语义。

设计意图（W1，docs/prd-quality-depth.md）：默认检索 loop 真正语义化——加一个轻量、确定性、
permissive-license（fastembed，Apache-2.0；默认多语 MiniLM，Apache-2.0）的真语义句向量后端，
注册进 make_embedding_backend，并提供 'auto' spec：装了 [embed-onnx] 则真语义、否则回落纯 BM25
（守 ADR 0005 lean 契约——extra 不在时行为不变）。

红色策略 / 离线性：
- 单元测试一律用【注入的 fake fastembed 模块】（sys.modules 替身），零网络、零真实安装依赖——
  与 test_embedding_backends.py 的 fake openai 同范式；
- 构造【惰性】：不装 fastembed 也能构造 OnnxEmbeddingBackend（模型在首次 embed_texts 时才加载）；
- 真模型的【确定性 + 跨语义】只在 @pytest.mark.network 用例里跑（首次需联网下载权重再缓存，
  "首拉后离线"），CI 默认 `-m "not network"` 跳过，绝不让 make ci 跑网络。
"""

import os
import sys
import types

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.vector.embedding_backends import (
    DEFAULT_ONNX_EMBEDDING_MODEL,
    OnnxEmbeddingBackend,
    make_embedding_backend,
)


# ---------------------------------------------------------------------------
# fake fastembed SDK（零网络、零安装）
# ---------------------------------------------------------------------------

def _install_fake_fastembed(monkeypatch, *, dim=4, drop_last=False, dims_split=False):
    """注入 fake fastembed 模块，捕获 TextEmbedding 构造参数与 embed 调用。

    embed 产出确定性 fake 向量（按文本长度区分，便于断顺序对齐）；
    drop_last：少产出一条（测条数校验）；dims_split：第二条维度不同（测维度一致性校验）。
    """
    captured: dict = {"init_kwargs": None, "embed_calls": []}

    class _FakeTextEmbedding:
        def __init__(self, model_name=None, **kwargs):
            captured["init_kwargs"] = {"model_name": model_name, **kwargs}

        def embed(self, documents, **kwargs):
            docs = list(documents)
            captured["embed_calls"].append({"documents": docs, **kwargs})
            out = []
            for i, t in enumerate(docs):
                d = dim + 1 if (dims_split and i == 1) else dim
                out.append([float(len(t))] + [0.5] * (d - 1))
            if drop_last:
                out = out[:-1]
            return iter(out)

    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    return captured


# ---------------------------------------------------------------------------
# 惰性构造 / 友好报错
# ---------------------------------------------------------------------------

def test_ctor_is_lazy_no_fastembed_needed(monkeypatch):
    """构造惰性：模拟未装 fastembed 也能构造（模型在 embed 时才加载）。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)  # 模拟未安装
    backend = OnnxEmbeddingBackend()  # 不应抛错
    assert backend.model_name == DEFAULT_ONNX_EMBEDDING_MODEL


def test_embed_without_fastembed_raises_friendly(monkeypatch):
    """首次 embed_texts 时缺 fastembed -> 友好提示（含 fastembed / extra 名）。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    backend = OnnxEmbeddingBackend()
    with pytest.raises(ImportError) as exc:
        backend.embed_texts(["x"])
    msg = str(exc.value).lower()
    assert "fastembed" in msg
    assert "embed-onnx" in msg


def test_empty_input_no_model_load(monkeypatch):
    """空输入直接返回 []，不触发任何 import / 模型加载。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)  # 若误加载会抛
    assert OnnxEmbeddingBackend().embed_texts([]) == []


def test_default_model_constant_centralized():
    """默认模型名集中一处，且为 permissive-license 的多语小模型（Apache-2.0）。"""
    assert DEFAULT_ONNX_EMBEDDING_MODEL == (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )


def test_model_override(monkeypatch):
    """model_name 可覆盖，并透传给 fastembed.TextEmbedding。"""
    captured = _install_fake_fastembed(monkeypatch)
    backend = OnnxEmbeddingBackend(model_name="BAAI/bge-small-en-v1.5")
    backend.embed_texts(["a"])
    assert captured["init_kwargs"]["model_name"] == "BAAI/bge-small-en-v1.5"


def test_invalid_batch_size_rejected():
    """batch_size < 1 -> ValueError（构造期即拒，不延迟）。"""
    with pytest.raises(ValueError):
        OnnxEmbeddingBackend(batch_size=0)


# ---------------------------------------------------------------------------
# embed_texts：顺序 / 形状 / 校验 / 模型只加载一次
# ---------------------------------------------------------------------------

def test_embed_order_aligned_and_float(monkeypatch):
    """输出与输入顺序对齐，元素为 python float。"""
    _install_fake_fastembed(monkeypatch, dim=4)
    backend = OnnxEmbeddingBackend()
    vecs = backend.embed_texts(["a", "bb", "ccc"])
    assert [v[0] for v in vecs] == [1.0, 2.0, 3.0]
    assert all(isinstance(x, float) for v in vecs for x in v)


def test_model_loaded_once_and_cached(monkeypatch):
    """模型延迟加载且只构造一次（跨多次 embed 复用同一 TextEmbedding）。"""
    calls = {"n": 0}
    real_install = _install_fake_fastembed(monkeypatch)
    fake_mod = sys.modules["fastembed"]
    orig = fake_mod.TextEmbedding

    class _Counting(orig):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            calls["n"] += 1
            super().__init__(*a, **k)

    fake_mod.TextEmbedding = _Counting
    backend = OnnxEmbeddingBackend()
    backend.embed_texts(["a"])
    backend.embed_texts(["b"])
    assert calls["n"] == 1
    assert real_install["embed_calls"]  # embed 实际被调过


def test_count_mismatch_raises(monkeypatch):
    """返回条数与输入不一致 -> 抛错（绝不静默给坏向量）。"""
    _install_fake_fastembed(monkeypatch, drop_last=True)
    with pytest.raises((RuntimeError, ValueError)):
        OnnxEmbeddingBackend().embed_texts(["a", "b"])


def test_dimension_consistency_check(monkeypatch):
    """各条向量维度不一致 -> ValueError。"""
    _install_fake_fastembed(monkeypatch, dims_split=True)
    with pytest.raises(ValueError):
        OnnxEmbeddingBackend().embed_texts(["a", "bb"])


# ---------------------------------------------------------------------------
# 工厂 make_embedding_backend：onnx 别名 + auto 语义
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", ["onnx", "fastembed", "minilm", "ONNX", " onnx "])
def test_factory_onnx_aliases_return_instance(spec):
    """'onnx' 及其别名（含大小写/留白归一）-> OnnxEmbeddingBackend（构造惰性，无需 fastembed 在场）。"""
    backend = make_embedding_backend(spec)
    assert isinstance(backend, OnnxEmbeddingBackend)


def test_factory_auto_returns_onnx_when_fastembed_present(monkeypatch):
    """'auto' + fastembed 可导入 -> 真语义 OnnxEmbeddingBackend。"""
    _install_fake_fastembed(monkeypatch)
    backend = make_embedding_backend("auto")
    assert isinstance(backend, OnnxEmbeddingBackend)


def test_factory_auto_falls_back_to_none_when_absent(monkeypatch):
    """'auto' + 未装 fastembed -> None（lean 回落纯 BM25，守 ADR 0005，行为不变）。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)  # 模拟未安装
    assert make_embedding_backend("auto") is None


def test_factory_auto_via_env(monkeypatch):
    """缺省 spec 读 env=auto：fastembed 在场 -> 实例；缺省 None。"""
    _install_fake_fastembed(monkeypatch)
    monkeypatch.setenv("RAGSPINE_EMBEDDING_BACKEND", "auto")
    assert isinstance(make_embedding_backend(), OnnxEmbeddingBackend)


def test_factory_none_still_pure_bm25():
    """回归：None / 'none' 仍 -> None（lean 默认纯 BM25 契约不变，不被 onnx/auto 改动污染）。"""
    assert make_embedding_backend(None) is None
    assert make_embedding_backend("none") is None


# ---------------------------------------------------------------------------
# 真模型确定性 + 跨语义 conformance（联网首拉，CI 默认 `-m "not network"` 跳过）
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_onnx_real_deterministic_and_crosslingual():
    """真 ONNX 后端：同输入逐位一致（确定性 conformance）+ 跨语言语义可比（真语义）。

    联网首次下载权重再缓存；pin 模型 + fastembed 版本保证 CPU onnxruntime 逐位可复现。
    """
    import warnings

    pytest.importorskip("fastembed", reason="fastembed 未装（pip install ragspine[embed-onnx]）")

    from ragspine.retrieval.lexical.retrieval import cosine_similarity

    texts = [
        "香港业务营收持续增长，主要由代理渠道贡献。",  # zh 文档
        "revenue growth in Hong Kong",                # en 同义查询
        "weekend cricket match report 板球比赛",        # 无关
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # fastembed 第三方 pooling UserWarning，与确定性无关
        v1 = OnnxEmbeddingBackend().embed_texts(texts)
        v2 = OnnxEmbeddingBackend().embed_texts(texts)

    # 确定性：两个独立实例对同输入逐位一致。
    assert v1 == v2
    assert len(v1[0]) == 384  # 多语 MiniLM 维度
    # 真语义：zh 文档与其 en 同义查询的相似度 > 与无关文本（散列后端做不到的跨语言可比）。
    assert cosine_similarity(v1[0], v1[1]) > cosine_similarity(v1[0], v1[2])
