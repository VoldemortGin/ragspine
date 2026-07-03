"""W8 链组合 + make_postprocessor 工厂：spec/env 选型、逗号成链、默认 None。"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.postprocess import (
    POSTPROCESSOR_ENV,
    ChainPostprocessor,
    CompressionPostprocessor,
    LostInTheMiddlePostprocessor,
    MMRPostprocessor,
    NodePostprocessor,
    make_postprocessor,
)


def _snip(chunk_id: str, text: str) -> dict[str, object]:
    return {"text": text, "chunk_id": chunk_id, "sensitivity": "INTERNAL"}


def test_make_none_returns_none_default_no_chain():
    assert make_postprocessor(None) is None
    assert make_postprocessor("none") is None
    assert make_postprocessor("") is None


def test_make_single_specs():
    assert isinstance(make_postprocessor("mmr"), MMRPostprocessor)
    assert isinstance(make_postprocessor("lost_in_middle"), LostInTheMiddlePostprocessor)
    assert isinstance(make_postprocessor("litm"), LostInTheMiddlePostprocessor)
    assert isinstance(make_postprocessor("compress"), CompressionPostprocessor)


def test_make_spec_is_normalized_case_space_hyphen():
    assert isinstance(make_postprocessor(" MMR "), MMRPostprocessor)
    assert isinstance(make_postprocessor("Lost-In-Middle"), LostInTheMiddlePostprocessor)


def test_make_comma_spec_builds_ordered_chain():
    pp = make_postprocessor("mmr,lost_in_middle")
    assert isinstance(pp, ChainPostprocessor)
    assert isinstance(pp.processors[0], MMRPostprocessor)
    assert isinstance(pp.processors[1], LostInTheMiddlePostprocessor)


def test_make_unknown_spec_raises_listing_available():
    with pytest.raises(ValueError):
        make_postprocessor("does_not_exist")


def test_make_reads_env(monkeypatch):
    monkeypatch.setenv(POSTPROCESSOR_ENV, "mmr")
    assert isinstance(make_postprocessor(None), MMRPostprocessor)
    monkeypatch.setenv(POSTPROCESSOR_ENV, "none")
    assert make_postprocessor(None) is None


def test_chain_applies_processors_in_order():
    """链按序把上一处理器输出喂给下一处理器。"""
    items = [_snip(c, c) for c in ("d0", "d1", "d2", "d3")]
    chain = ChainPostprocessor([LostInTheMiddlePostprocessor()])
    out = [s["chunk_id"] for s in chain.postprocess("q", items)]
    litm = [s["chunk_id"] for s in LostInTheMiddlePostprocessor().postprocess("q", items)]
    assert out == litm


def test_chain_is_a_node_postprocessor():
    chain = make_postprocessor("mmr,compress")
    assert isinstance(chain, NodePostprocessor)


def test_chain_empty_passthrough():
    items = [_snip("a", "x")]
    out = ChainPostprocessor([]).postprocess("q", items)
    assert [s["chunk_id"] for s in out] == ["a"]
