"""W8 lost-in-the-middle 重排序 postprocessor：最相关落两端、次要居中（确定性）。"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.postprocess import LostInTheMiddlePostprocessor


def _snip(chunk_id: str) -> dict[str, object]:
    return {"text": chunk_id, "chunk_id": chunk_id, "sensitivity": "INTERNAL"}


QUERY = "任意查询"


def test_lost_in_middle_puts_top_two_at_the_two_ends():
    """输入按相关性降序：最相关落头、次相关落尾，最不相关居中。"""
    items = [_snip(c) for c in ("d0", "d1", "d2", "d3", "d4")]  # d0 最相关
    out = [s["chunk_id"] for s in LostInTheMiddlePostprocessor().postprocess(QUERY, items)]
    # 规范 LITM：head=[d0,d2,d4] + reversed tail=[d3,d1] => [d0,d2,d4,d3,d1]
    assert out == ["d0", "d2", "d4", "d3", "d1"]
    assert out[0] == "d0"       # 最相关在头端
    assert out[-1] == "d1"      # 次相关在尾端
    assert "d4" in out[1:-1]    # 最不相关落中段，不在两端


def test_lost_in_middle_reorder_preserves_membership():
    items = [_snip(c) for c in ("a", "b", "c", "d")]
    out = LostInTheMiddlePostprocessor().postprocess(QUERY, items)
    assert {s["chunk_id"] for s in out} == {"a", "b", "c", "d"}
    assert len(out) == 4
    assert {id(s) for s in out} <= {id(s) for s in items}  # 纯重排，不造片段


def test_lost_in_middle_deterministic():
    items = [_snip(c) for c in ("a", "b", "c", "d", "e", "f")]
    o1 = [s["chunk_id"] for s in LostInTheMiddlePostprocessor().postprocess(QUERY, items)]
    o2 = [s["chunk_id"] for s in LostInTheMiddlePostprocessor().postprocess(QUERY, items)]
    assert o1 == o2


def test_lost_in_middle_empty_and_singleton():
    assert LostInTheMiddlePostprocessor().postprocess(QUERY, []) == []
    one = [_snip("a")]
    assert [s["chunk_id"] for s in LostInTheMiddlePostprocessor().postprocess(QUERY, one)] == ["a"]
