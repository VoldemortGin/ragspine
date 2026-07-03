"""W8 MMR 多样性去冗余 postprocessor：去冗余正确性 + 确定性 + 子集/重排语义。"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.postprocess import MMRPostprocessor


def _snip(chunk_id: str, text: str, fused: float = 0.0) -> dict[str, object]:
    return {
        "text": text,
        "doc_id": f"{chunk_id}.pptx",
        "source_locator": f"p.{chunk_id}",
        "chunk_id": chunk_id,
        "sensitivity": "INTERNAL",
        "scores": {"bm25": 0.0, "vector": 0.0, "fused": fused},
    }


QUERY = "香港 REVENUE 下降 原因"


def test_mmr_pushes_near_duplicate_below_a_distinct_chunk():
    """近重复块被相似度惩罚压到不同块之后（去冗余的核心行为）。"""
    a = _snip("A", "香港 REVENUE 下降 因 MCV 客群 收缩")
    a_dup = _snip("A2", "香港 REVENUE 下降 因 MCV 客群 收缩")  # 与 A 词面等同
    b = _snip("B", "银保 渠道 结构 调整 与 新单 价值")  # 与 A 无重叠
    out = MMRPostprocessor(lambda_=0.5).postprocess(QUERY, [a, a_dup, b])
    ids = [s["chunk_id"] for s in out]
    # A 最先选中；A2 因与 A 全重叠被惩罚，B 虽相关性稍低但更多样，排到 A2 之前。
    assert ids == ["A", "B", "A2"]


def test_mmr_top_n_drops_the_redundant_tail():
    """top_n 截断后近重复块被真正丢弃（省上下文窗口）。"""
    a = _snip("A", "香港 REVENUE 下降 因 MCV 客群 收缩")
    a_dup = _snip("A2", "香港 REVENUE 下降 因 MCV 客群 收缩")
    b = _snip("B", "银保 渠道 结构 调整 与 新单 价值")
    out = MMRPostprocessor(lambda_=0.5, top_n=2).postprocess(QUERY, [a, a_dup, b])
    ids = [s["chunk_id"] for s in out]
    assert ids == ["A", "B"]
    assert all(s["chunk_id"] != "A2" for s in out)


def test_mmr_is_a_pure_reorder_subset_never_fabricates():
    """输出是输入的子集/重排——不新增、不篡改任一片段对象。"""
    items = [_snip("A", "alpha beta"), _snip("B", "gamma delta"), _snip("C", "beta gamma")]
    out = MMRPostprocessor().postprocess(QUERY, items)
    assert {id(s) for s in out} <= {id(s) for s in items}
    assert len(out) == len(items)


def test_mmr_deterministic_same_input_same_output():
    items = [_snip("A", "alpha beta"), _snip("B", "gamma delta"), _snip("C", "beta gamma")]
    o1 = [s["chunk_id"] for s in MMRPostprocessor(lambda_=0.6).postprocess(QUERY, items)]
    o2 = [s["chunk_id"] for s in MMRPostprocessor(lambda_=0.6).postprocess(QUERY, items)]
    assert o1 == o2


def test_mmr_first_pick_honours_upstream_relevance_order():
    """首选恒为输入首位（继承上游 rerank/RRF 序作相关性）。"""
    items = [_snip("A", "unrelated tokens here"), _snip("B", "香港 REVENUE 下降")]
    out = MMRPostprocessor().postprocess(QUERY, items)
    assert out[0]["chunk_id"] == "A"


def test_mmr_ties_break_by_original_order():
    """相似度/相关性全平时按原序稳定（确定性平分）。"""
    items = [_snip(c, "x y z") for c in ("A", "B", "C")]  # 完全相同文本
    out = [s["chunk_id"] for s in MMRPostprocessor().postprocess(QUERY, items)]
    assert out == ["A", "B", "C"]


def test_mmr_empty_and_singleton():
    assert MMRPostprocessor().postprocess(QUERY, []) == []
    one = [_snip("A", "香港")]
    assert [s["chunk_id"] for s in MMRPostprocessor().postprocess(QUERY, one)] == ["A"]
