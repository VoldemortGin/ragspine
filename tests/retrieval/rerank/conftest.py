"""rerank 重排测试夹具：注入 fake fastembed 后端（零网络、零安装）。

范式同 tests/retrieval/vector/test_embedding_onnx.py 的 fake fastembed：用 sys.modules 替身把
fastembed（及其子模块）顶替掉，捕获后端构造参数与打分调用，产出确定性 fake 输出——单测既不
联网也不需要真装对应 extra。

夹具：
- fake_cross_encoder（W2）：fake `fastembed.rerank.cross_encoder.TextCrossEncoder`。
- fake_colbert（W11）：fake `fastembed.LateInteractionTextEmbedding`（token 级多向量晚交互）。
- fake_splade（W11）：fake `fastembed.SparseTextEmbedding`（学习稀疏 term-expansion 向量）。
"""

import sys
import types
from collections import Counter

import pytest


@pytest.fixture
def fake_cross_encoder(monkeypatch):
    """返回一个安装器：fake_cross_encoder(score_fn=..., drop_last=...) -> captured dict。

    captured["init_kwargs"]：TextCrossEncoder 构造参数（model_name + cache_dir/threads 透传）。
    captured["rerank_calls"]：每次 rerank(query, documents, **kwargs) 的入参（用于断 RESTRICTED
    文本绝不进入打分文档、batch_size 透传等）。
    score_fn(docs)->list[float]：自定义每条候选的分数（缺省按文本长度）；drop_last 少产出一条
    （测条数校验）。
    """

    def _install(*, score_fn=None, drop_last=False):
        captured: dict = {"init_kwargs": None, "rerank_calls": []}

        class _FakeTextCrossEncoder:
            def __init__(self, model_name=None, **kwargs):
                captured["init_kwargs"] = {"model_name": model_name, **kwargs}

            def rerank(self, query, documents, **kwargs):
                docs = list(documents)
                captured["rerank_calls"].append({"query": query, "documents": docs, **kwargs})
                out = list(score_fn(docs)) if score_fn is not None else [float(len(d)) for d in docs]
                if drop_last:
                    out = out[:-1]
                return iter(out)

        ce_mod = types.ModuleType("fastembed.rerank.cross_encoder")
        ce_mod.TextCrossEncoder = _FakeTextCrossEncoder
        monkeypatch.setitem(sys.modules, "fastembed", types.ModuleType("fastembed"))
        monkeypatch.setitem(sys.modules, "fastembed.rerank", types.ModuleType("fastembed.rerank"))
        monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", ce_mod)
        return captured

    return _install


def _char_onehot(token: str) -> list[float]:
    """把一个 token（取首字符）确定性映射到 26 维字母 one-hot；非字母 -> 零向量。

    让 fake ColBERT 的每个 token 向量正交可控：MaxSim(query, doc) 即"共享字母词数"，
    故测试可用字母词精确控制晚交互打分与名次。"""
    v = [0.0] * 26
    c = token[:1].lower()
    if "a" <= c <= "z":
        v[ord(c) - 97] = 1.0
    return v


@pytest.fixture
def fake_colbert(monkeypatch):
    """返回安装器：fake_colbert(drop_last=...) -> captured dict（注入 fake LateInteractionTextEmbedding）。

    captured["init_kwargs"]：LateInteractionTextEmbedding 构造参数（model_name + cache_dir/threads 透传）。
    captured["embed_calls"]：每次 embed(documents) 的文档列表（用于断 RESTRICTED 文本绝不进入打分）。
    captured["query_calls"]：每次 query_embed(query) 的 query。
    fake 语义：每篇文本按空白切成 token，每个 token 取首字母映射成 26 维正交 one-hot，故一篇文本
    是一个 (n_token, 26) 的 token 矩阵；query 亦然——真实的 token 级晚交互结构，MaxSim 可控可复现。
    drop_last：embed 少产出一条（测条数校验）。"""

    def _install(*, drop_last=False):
        captured: dict = {"init_kwargs": None, "embed_calls": [], "query_calls": []}

        def _matrix(text):
            return [_char_onehot(t) for t in text.split()] or [[0.0] * 26]

        class _FakeLateInteraction:
            def __init__(self, model_name=None, **kwargs):
                captured["init_kwargs"] = {"model_name": model_name, **kwargs}

            def query_embed(self, query, **kwargs):
                captured["query_calls"].append(query)
                return iter([_matrix(query)])

            def embed(self, documents, **kwargs):
                docs = list(documents)
                captured["embed_calls"].append(docs)
                out = [_matrix(d) for d in docs]
                if drop_last:
                    out = out[:-1]
                return iter(out)

        fake = types.ModuleType("fastembed")
        fake.LateInteractionTextEmbedding = _FakeLateInteraction
        monkeypatch.setitem(sys.modules, "fastembed", fake)
        return captured

    return _install


class _FakeSparseEmbedding:
    """fake fastembed.SparseEmbedding：indices + values 两个平行序列（真实 API 同构）。"""

    def __init__(self, indices, values):
        self.indices = indices
        self.values = values


@pytest.fixture
def fake_splade(monkeypatch):
    """返回安装器：fake_splade(drop_last=...) -> captured dict（注入 fake SparseTextEmbedding）。

    captured 同 fake_colbert（init_kwargs / embed_calls / query_calls）。
    fake 语义：每篇文本按空白切成 token，term id = 首字符 ord、value = 该 term 词频，得到一个
    稀疏向量（真实 SPLADE 学习稀疏的同构替身）；SPLADE 打分即两稀疏向量共享维度的点积，可控可复现。
    drop_last：embed 少产出一条（测条数校验）。"""

    def _install(*, drop_last=False):
        captured: dict = {"init_kwargs": None, "embed_calls": [], "query_calls": []}

        def _sparse(text):
            counts = Counter(text.split())
            indices = [ord(t[:1]) for t in counts]
            values = [float(counts[t]) for t in counts]
            return _FakeSparseEmbedding(indices, values)

        class _FakeSparseText:
            def __init__(self, model_name=None, **kwargs):
                captured["init_kwargs"] = {"model_name": model_name, **kwargs}

            def query_embed(self, query, **kwargs):
                captured["query_calls"].append(query)
                return iter([_sparse(query)])

            def embed(self, documents, **kwargs):
                docs = list(documents)
                captured["embed_calls"].append(docs)
                out = [_sparse(d) for d in docs]
                if drop_last:
                    out = out[:-1]
                return iter(out)

        fake = types.ModuleType("fastembed")
        fake.SparseTextEmbedding = _FakeSparseText
        monkeypatch.setitem(sys.modules, "fastembed", fake)
        return captured

    return _install
