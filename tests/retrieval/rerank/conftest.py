"""W2 cross-encoder 重排测试夹具：注入 fake fastembed TextCrossEncoder（零网络、零安装）。

范式同 tests/retrieval/vector/test_embedding_onnx.py 的 fake fastembed：用 sys.modules 替身把
`fastembed.rerank.cross_encoder` 顶替掉，捕获 TextCrossEncoder 构造参数与 rerank 调用，产出
确定性 fake 分数——单测既不联网也不需要真装 [rerank]。
"""

import sys
import types

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
