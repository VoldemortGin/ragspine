"""OpenAIEmbeddingBackend 测试（TDD 红色阶段）：fake SDK 注入，零网络零安装依赖。

覆盖：
- SDK 延迟 import：不装 openai 也能 import 本模块；缺 SDK 时构造报带提示的错误；
- 默认模型集中一处（text-embedding-3-large）、model 可配；
- api_key / base_url（企业网关）透传给 client 构造；
- 批量分批：batch_size 参数化、跨批次顺序对齐、返回乱序（index）也能恢复；
- 维度一致性校验、返回条数校验、空输入零请求；
- B 线协议接入：build_narrative_retriever 可选 embedding_backend 参数
  （默认 None＝纯 BM25 现状，既有调用零影响）。
"""

import os
import sys
import types
from types import SimpleNamespace

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.vector.embedding_backends import (
    DEFAULT_OPENAI_EMBEDDING_MODEL,
    OpenAIEmbeddingBackend,
)
from ragspine.retrieval.link.narrative_link import build_narrative_retriever


# ---------------------------------------------------------------------------
# fake openai SDK
# ---------------------------------------------------------------------------

def _install_fake_openai(monkeypatch, *, dim=4, dims_per_call=None,
                         reverse_data=False, drop_last=False):
    """注入 fake openai 模块，捕获 client 构造参数与每次 embeddings.create 调用。

    dims_per_call：依次指定每个 create 调用返回的向量维度（测维度一致性校验）；
    reverse_data：data 按 index 倒序返回（测按 index 恢复顺序）；
    drop_last：少返回一条（测条数校验）。
    """
    captured: dict = {"client_kwargs": None, "create_calls": []}

    class _Embeddings:
        def create(self, **kwargs):
            captured["create_calls"].append(kwargs)
            call_no = len(captured["create_calls"]) - 1
            d = dims_per_call[call_no] if dims_per_call else dim
            inputs = kwargs["input"]
            data = [
                SimpleNamespace(index=i, embedding=[float(hash(text) % 97)] * d)
                for i, text in enumerate(inputs)
            ]
            if reverse_data:
                data = list(reversed(data))
            if drop_last:
                data = data[:-1]
            return SimpleNamespace(data=data)

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.embeddings = _Embeddings()

    fake = types.ModuleType("openai")
    fake.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)
    return captured


# ---------------------------------------------------------------------------
# 延迟 import / 构造
# ---------------------------------------------------------------------------

def test_module_importable_and_ctor_fails_without_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)  # 模拟未安装
    with pytest.raises((ImportError, RuntimeError)) as exc_info:
        OpenAIEmbeddingBackend(api_key="k")
    assert "openai" in str(exc_info.value).lower()


def test_default_model_constant_centralized(monkeypatch):
    assert DEFAULT_OPENAI_EMBEDDING_MODEL == "text-embedding-3-large"
    captured = _install_fake_openai(monkeypatch)
    backend = OpenAIEmbeddingBackend(api_key="k")
    backend.embed_texts(["a"])
    assert captured["create_calls"][0]["model"] == DEFAULT_OPENAI_EMBEDDING_MODEL


def test_model_override(monkeypatch):
    captured = _install_fake_openai(monkeypatch)
    backend = OpenAIEmbeddingBackend(api_key="k", model="text-embedding-3-small")
    backend.embed_texts(["a"])
    assert captured["create_calls"][0]["model"] == "text-embedding-3-small"


def test_api_key_and_base_url_passed_to_client(monkeypatch):
    captured = _install_fake_openai(monkeypatch)
    OpenAIEmbeddingBackend(api_key="sk-test", base_url="https://genai-hub.acme.internal/v1")
    assert captured["client_kwargs"]["api_key"] == "sk-test"
    assert captured["client_kwargs"]["base_url"] == "https://genai-hub.acme.internal/v1"


def test_client_kwargs_omitted_when_not_given(monkeypatch):
    """不传 api_key/base_url 时不注入 None（让 SDK 走环境变量默认）。

    timeout/max_retries 为韧性透传，始终在场（默认值），不属于 None 注入范畴。
    """
    captured = _install_fake_openai(monkeypatch)
    OpenAIEmbeddingBackend()
    assert "api_key" not in captured["client_kwargs"]
    assert "base_url" not in captured["client_kwargs"]
    assert captured["client_kwargs"] == {"timeout": 30.0, "max_retries": 2}


def test_invalid_batch_size_rejected(monkeypatch):
    _install_fake_openai(monkeypatch)
    with pytest.raises(ValueError):
        OpenAIEmbeddingBackend(api_key="k", batch_size=0)


# ---------------------------------------------------------------------------
# embed_texts：分批 / 顺序 / 校验
# ---------------------------------------------------------------------------

def test_batching_splits_requests(monkeypatch):
    captured = _install_fake_openai(monkeypatch)
    backend = OpenAIEmbeddingBackend(api_key="k", batch_size=2)
    texts = ["t1", "t2", "t3", "t4", "t5"]
    vectors = backend.embed_texts(texts)
    assert [c["input"] for c in captured["create_calls"]] == [
        ["t1", "t2"], ["t3", "t4"], ["t5"],
    ]
    assert len(vectors) == 5


def test_order_alignment_across_batches_and_shuffled_data(monkeypatch):
    """返回 data 即使按 index 乱序，输出向量也必须与输入文本顺序对齐。"""
    _install_fake_openai(monkeypatch, reverse_data=True)
    backend = OpenAIEmbeddingBackend(api_key="k", batch_size=2)
    texts = ["alpha", "beta", "gamma"]
    vectors = backend.embed_texts(texts)
    expected = [[float(hash(t) % 97)] * 4 for t in texts]
    assert vectors == expected


def test_dimension_consistency_check(monkeypatch):
    _install_fake_openai(monkeypatch, dims_per_call=[4, 8])
    backend = OpenAIEmbeddingBackend(api_key="k", batch_size=2)
    with pytest.raises(ValueError):
        backend.embed_texts(["a", "b", "c"])


def test_count_mismatch_raises(monkeypatch):
    _install_fake_openai(monkeypatch, drop_last=True)
    backend = OpenAIEmbeddingBackend(api_key="k")
    with pytest.raises((RuntimeError, ValueError)):
        backend.embed_texts(["a", "b"])


def test_empty_input_no_request(monkeypatch):
    captured = _install_fake_openai(monkeypatch)
    backend = OpenAIEmbeddingBackend(api_key="k")
    assert backend.embed_texts([]) == []
    assert captured["create_calls"] == []


# ---------------------------------------------------------------------------
# B 线接入：build_narrative_retriever 的 embedding_backend 参数
# ---------------------------------------------------------------------------

class _FakeBackend:
    """实现 EmbeddingBackend 协议的极简替身。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def test_build_narrative_retriever_default_is_pure_bm25(tmp_path):
    retriever, store = build_narrative_retriever(tmp_path / "chunks.db")
    try:
        assert retriever.index.embedding_backend is None
    finally:
        store.close()


def test_build_narrative_retriever_accepts_embedding_backend(tmp_path):
    backend = _FakeBackend()
    retriever, store = build_narrative_retriever(
        tmp_path / "chunks.db", embedding_backend=backend
    )
    try:
        assert retriever.index.embedding_backend is backend
    finally:
        store.close()
