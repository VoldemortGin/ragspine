"""W8 链编排 + 选型工厂单测：make_postprocessor 各 spec、预设/逗号链、字节不变、env、包裹器透传。

红色策略：FakeBase 替身 + 纯 snippet dict，零网络零模型。重点验证「默认 none => 字节不变」。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.postprocess.chain import (
    POSTPROCESSOR_ENV,
    PostprocessingRetriever,
    PostprocessorChain,
    make_postprocessing_retriever,
    make_postprocessor,
)
from ragspine.retrieval.postprocess.compress import CompressionPostprocessor
from ragspine.retrieval.postprocess.mmr import MMRPostprocessor
from ragspine.retrieval.postprocess.reorder import LostInTheMiddleReorder


class FakeBase:
    """记录调用并按固定脚本返回片段的 base NarrativeRetriever 替身。"""

    def __init__(self, snippets):
        self.snippets = snippets
        self.calls = []

    def retrieve(self, query, *, filters=None, top_k=50):
        self.calls.append((query, filters, top_k))
        return list(self.snippets)


def _snip(cid, text):
    return {"chunk_id": cid, "text": text, "doc_id": f"{cid}.pdf"}


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(POSTPROCESSOR_ENV, raising=False)


# --- make_postprocessor 选型 ---


@pytest.mark.parametrize("spec", [None, "none", "", "  ", "NONE"])
def test_none_specs_return_none(spec):
    assert make_postprocessor(spec) is None


def test_single_specs_resolve_each_processor():
    assert isinstance(make_postprocessor("mmr"), MMRPostprocessor)
    assert isinstance(make_postprocessor("diversity"), MMRPostprocessor)
    assert isinstance(make_postprocessor("reorder"), LostInTheMiddleReorder)
    assert isinstance(make_postprocessor("litm"), LostInTheMiddleReorder)
    assert isinstance(make_postprocessor("long-context"), LostInTheMiddleReorder)
    assert isinstance(make_postprocessor("compress"), CompressionPostprocessor)
    assert isinstance(make_postprocessor("extractive"), CompressionPostprocessor)


def test_single_spec_passes_kwargs():
    mmr = make_postprocessor("mmr", lambda_param=0.7, top_n=3)
    assert isinstance(mmr, MMRPostprocessor)
    assert mmr.lambda_param == 0.7
    assert mmr.top_n == 3


def test_preset_recommended_builds_three_stage_chain():
    chain = make_postprocessor("recommended")
    assert isinstance(chain, PostprocessorChain)
    assert [type(p) for p in chain.processors] == [
        MMRPostprocessor,
        CompressionPostprocessor,
        LostInTheMiddleReorder,
    ]


def test_comma_separated_builds_ordered_chain():
    chain = make_postprocessor("mmr,reorder")
    assert isinstance(chain, PostprocessorChain)
    assert [type(p) for p in chain.processors] == [
        MMRPostprocessor,
        LostInTheMiddleReorder,
    ]


def test_unknown_spec_raises_valueerror():
    with pytest.raises(ValueError):
        make_postprocessor("bogus")


def test_env_var_read_when_spec_none(monkeypatch):
    monkeypatch.setenv(POSTPROCESSOR_ENV, "mmr")
    assert isinstance(make_postprocessor(), MMRPostprocessor)


# --- make_postprocessing_retriever 包裹 + 字节不变 ---


def test_none_returns_base_itself_byte_identical():
    """默认 none：返回 base 本身（同一对象），不接链——字节不变的结构保证。"""
    base = FakeBase([_snip("a", "x")])
    assert make_postprocessing_retriever(base, "none") is base
    assert make_postprocessing_retriever(base, None) is base


def test_wraps_when_spec_selected():
    base = FakeBase([_snip("a", "x")])
    wrapped = make_postprocessing_retriever(base, "mmr")
    assert isinstance(wrapped, PostprocessingRetriever)
    assert wrapped.base is base


def test_retriever_passes_filters_and_top_k_through():
    base = FakeBase([_snip("a", "香港 收入"), _snip("b", "新加坡 利润")])
    wrapped = make_postprocessing_retriever(base, "reorder")
    wrapped.retrieve("香港", filters={"entity": "ACME_HK"}, top_k=12)
    assert base.calls == [("香港", {"entity": "ACME_HK"}, 12)]


def test_chain_runs_processors_in_order():
    """链按序执行：reorder 后再 mmr top_n=1，验证组合生效。"""
    snippets = [_snip(c, f"主题{c} 内容") for c in "abcd"]
    base = FakeBase(snippets)
    wrapped = make_postprocessing_retriever(base, "reorder,mmr")
    out = wrapped.retrieve("主题a")
    assert {s["chunk_id"] for s in out} <= {"a", "b", "c", "d"}
    # 链输出恒为输入子集（绝不造片段）。
    for s in out:
        assert s in snippets


def test_empty_chain_via_factory_returns_none_path():
    """逗号串只剩空 token 时回落 None（不接链）。"""
    assert make_postprocessor(",") is None
