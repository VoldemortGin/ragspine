"""W8 MMR 多样性去重单测：确定性、零模型、子集/重排、硬去重阈值。

红色策略：纯 snippet dict 构造，零网络零模型。断言去重、重排、top_n、确定性、参数校验。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.postprocess.mmr import MMRPostprocessor


def _snip(chunk_id: str, text: str) -> dict[str, object]:
    return {"chunk_id": chunk_id, "text": text, "doc_id": f"{chunk_id}.pdf"}


def test_single_and_empty_passthrough():
    """0/1 条片段：原样返回（无可重排/去重）。"""
    mmr = MMRPostprocessor()
    assert mmr.postprocess("q", []) == []
    one = [_snip("a", "香港 收入 下降")]
    assert mmr.postprocess("q", one) == one


def test_hard_dedup_drops_near_duplicate():
    """similarity_threshold 设低：内容近重复的片段被丢弃（去重语义），不进结果。"""
    snippets = [
        _snip("a", "香港 收入 下降 银保 渠道 调整"),
        _snip("b", "香港 收入 下降 银保 渠道 调整"),  # 与 a 完全重复（Jaccard=1.0）
        _snip("c", "新加坡 利润 增长 代理人 扩张"),  # 与 a/b 不同主题
    ]
    out = MMRPostprocessor(similarity_threshold=0.9).postprocess("香港 收入", snippets)
    ids = [s["chunk_id"] for s in out]
    assert "a" in ids and "c" in ids
    assert "b" not in ids, "近重复片段应被硬去重丢弃"
    assert len(out) == 2


def test_no_dedup_by_default_keeps_all_but_reorders():
    """默认无硬阈值：全保留（只重排）；重复项不被删，但多样项被往前提。"""
    snippets = [
        _snip("a", "香港 收入 下降"),
        _snip("b", "香港 收入 下降"),  # 与 a 重复
        _snip("c", "新加坡 利润 增长"),  # 多样
    ]
    out = MMRPostprocessor(lambda_param=0.5).postprocess("收入", snippets)
    assert {s["chunk_id"] for s in out} == {"a", "b", "c"}, "默认不删任何片段"
    ids = [s["chunk_id"] for s in out]
    # a 名次最高先选；下一步多样的 c 应优先于与 a 重复的 b。
    assert ids[0] == "a"
    assert ids.index("c") < ids.index("b"), "多样片段应被提前于近重复片段"


def test_top_n_limits_count():
    """top_n 截断结果条数。"""
    snippets = [_snip(c, f"主题{c} 内容 {c}") for c in "abcde"]
    out = MMRPostprocessor(top_n=2).postprocess("主题a", snippets)
    assert len(out) == 2


def test_deterministic_across_two_runs():
    """同输入两次运行 -> 逐位一致（确定性）。"""
    snippets = [_snip(c, f"主题 {c} 收入 增长 下降") for c in "abcdef"]
    mmr = MMRPostprocessor(lambda_param=0.6, similarity_threshold=0.95)
    r1 = [s["chunk_id"] for s in mmr.postprocess("收入 增长", snippets)]
    r2 = [s["chunk_id"] for s in mmr.postprocess("收入 增长", snippets)]
    assert r1 == r2


def test_invalid_params_raise():
    with pytest.raises(ValueError):
        MMRPostprocessor(lambda_param=1.5)
    with pytest.raises(ValueError):
        MMRPostprocessor(top_n=-1)
    with pytest.raises(ValueError):
        MMRPostprocessor(similarity_threshold=2.0)


def test_returns_subset_only_never_fabricates():
    """输出片段必是输入片段对象的子集（绝不造片段）。"""
    snippets = [_snip(c, f"内容 {c}") for c in "abc"]
    out = MMRPostprocessor().postprocess("内容", snippets)
    for s in out:
        assert s in snippets
