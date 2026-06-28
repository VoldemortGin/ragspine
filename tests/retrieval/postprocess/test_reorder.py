"""W8 lost-in-the-middle 重排单测：最相关置首尾、最不相关居中；确定性、集合不变。"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.postprocess.reorder import LostInTheMiddleReorder


def _snip(rank: int) -> dict[str, object]:
    # rank 0 = 最相关（输入降序）。
    return {"chunk_id": f"r{rank}", "text": f"片段{rank}"}


def test_short_inputs_unchanged():
    """<=2 条：原样返回（无中部可言）。"""
    r = LostInTheMiddleReorder()
    assert r.postprocess("q", []) == []
    one = [_snip(0)]
    assert r.postprocess("q", one) == one
    two = [_snip(0), _snip(1)]
    assert r.postprocess("q", two) == two


def test_most_relevant_at_head_and_tail():
    """输入按相关性降序 [r0..r4]：最相关 r0 在头、次相关 r1 在尾、最不相关 r4 居中。"""
    snippets = [_snip(i) for i in range(5)]
    out = LostInTheMiddleReorder().postprocess("q", snippets)
    ids = [s["chunk_id"] for s in out]
    # 期望 [r0, r2, r4, r3, r1]（翻转升序后交替插头/追尾）。
    assert ids == ["r0", "r2", "r4", "r3", "r1"]
    assert ids[0] == "r0", "最相关在头"
    assert ids[-1] == "r1", "次相关在尾"
    assert ids[len(ids) // 2] == "r4", "最不相关居中"


def test_set_preserved_no_add_or_drop():
    """重排前后集合一致（只重排，不增删）。"""
    snippets = [_snip(i) for i in range(6)]
    out = LostInTheMiddleReorder().postprocess("q", snippets)
    assert {s["chunk_id"] for s in out} == {s["chunk_id"] for s in snippets}
    assert len(out) == len(snippets)


def test_deterministic():
    snippets = [_snip(i) for i in range(7)]
    r = LostInTheMiddleReorder()
    a = [s["chunk_id"] for s in r.postprocess("q", snippets)]
    b = [s["chunk_id"] for s in r.postprocess("q", snippets)]
    assert a == b
