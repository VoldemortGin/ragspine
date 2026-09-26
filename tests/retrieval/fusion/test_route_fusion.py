"""W12-B 多路融合单测：RRF 交错 + 同页跨腿合并（视觉确认 boost）+ 透传 + 隔离 + 确定性。"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.fusion.route_fusion import (
    RESTRICTED_SENSITIVITY,
    FusedRetriever,
    make_fused_retriever,
)


class FakeText:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def retrieve(self, query, *, filters=None, top_k=50):
        self.calls.append((query, filters, top_k))
        return [dict(h) for h in self.hits]


class FakeVisual:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def retrieve(self, query, *, top_k=10):
        self.calls.append((query, top_k))
        return [dict(h) for h in self.hits]


def _text(cid, doc, page=None, text="正文"):
    h = {
        "chunk_id": cid,
        "doc_id": doc,
        "text": text,
        "source_locator": f"{doc}#page{page}" if page else f"{doc}#para1",
    }
    return h


def _visual(doc, page, score=0.9):
    return {
        "doc_id": doc,
        "page": page,
        "source_locator": f"{doc}#page{page}",
        "is_visual": True,
        "scores": {"colpali_maxsim": score},
    }


# --- 透传（visual=None）---


def test_passthrough_when_no_visual_leg():
    text = FakeText([_text("a", "d.pdf"), _text("b", "d.pdf")])
    fr = FusedRetriever(text, None)
    out = fr.retrieve("q")
    assert [h["chunk_id"] for h in out] == ["a", "b"]
    assert all("fused_score" not in h for h in out), "透传不加注"


def test_make_fused_retriever_none_returns_text_itself():
    text = FakeText([])
    assert make_fused_retriever(text, None) is text
    assert isinstance(make_fused_retriever(text, FakeVisual([])), FusedRetriever)


# --- RRF 交错（无重叠页）---


def test_rrf_interleaves_disjoint_routes():
    text = FakeText([_text("t1", "a.pdf"), _text("t2", "a.pdf")])
    visual = FakeVisual([_visual("b.pdf", 1)])
    out = FusedRetriever(text, visual).retrieve("q")
    keys = [(h.get("chunk_id"), h.get("is_visual")) for h in out]
    # t1 与 视觉页 各自 rank1 -> RRF 同分；t1 先出现 -> 稳定排前；视觉页次之；t2 末。
    assert keys[0] == ("t1", None)
    assert any(h.get("is_visual") for h in out), "视觉命中进入融合结果"
    assert {h.get("chunk_id") for h in out if not h.get("is_visual")} == {"t1", "t2"}
    for h in out:
        assert "fused_score" in h and "retrieval_routes" in h


# --- 同页跨腿合并 + 视觉确认 boost ---


def test_same_page_merges_and_visual_confirmation_boosts_rank():
    # 文本：A(page1) rank1，B(page2) rank2；视觉：page2 rank1（与 B 同页）。
    text = FakeText([_text("A", "r.pdf", page=1), _text("B", "r.pdf", page=2)])
    visual = FakeVisual([_visual("r.pdf", 2, score=0.88)])
    out = FusedRetriever(text, visual).retrieve("q")

    # B 被视觉确认（text rank2 + visual rank1），分数应高于仅文本 rank1 的 A -> B 排在 A 前。
    ids = [h.get("chunk_id") for h in out]
    assert ids.index("B") < ids.index("A"), "视觉确认的页应被 boost 到更前"

    b = next(h for h in out if h.get("chunk_id") == "B")
    assert b["text"] == "正文", "代表是文本命中（带 text，LLM 可消费）"
    assert b["retrieval_routes"] == ["text", "visual"]
    assert b.get("visual_score") == 0.88
    assert not b.get("is_visual"), "合并后代表非纯视觉"
    # 合并后页不重复出现两条。
    assert len([h for h in out if h.get("doc_id") == "r.pdf" and _safe_page(h) == 2]) == 1


def _safe_page(h):
    p = h.get("page")
    if isinstance(p, int):
        return p
    import re

    m = re.search(r"page(\d+)", str(h.get("source_locator") or ""))
    return int(m.group(1)) if m else None


def test_para_locator_not_parsed_as_page():
    """'#para1' 不应被当成 page（否则会错误跨腿对齐）。"""
    text = FakeText([_text("t", "d.pdf")])  # source_locator d.pdf#para1
    visual = FakeVisual([_visual("d.pdf", 1)])  # page 1
    out = FusedRetriever(text, visual).retrieve("q")
    # 两者不同键 -> 都在，不合并（文本块 + 视觉页各一条）。
    assert any(h.get("chunk_id") == "t" for h in out)
    assert any(h.get("is_visual") for h in out)
    assert len(out) == 2


# --- top_k / 确定性 ---


def test_top_k_truncates():
    text = FakeText([_text(f"t{i}", "d.pdf") for i in range(5)])
    visual = FakeVisual([_visual("d.pdf", n) for n in range(5)])
    out = FusedRetriever(text, visual).retrieve("q", top_k=3)
    assert len(out) == 3


def test_deterministic():
    text = FakeText([_text("a", "d.pdf", page=1), _text("b", "d.pdf", page=2)])
    visual = FakeVisual([_visual("d.pdf", 2)])
    r1 = [
        h.get("chunk_id") or h.get("source_locator")
        for h in FusedRetriever(text, visual).retrieve("q")
    ]
    r2 = [
        h.get("chunk_id") or h.get("source_locator")
        for h in FusedRetriever(text, visual).retrieve("q")
    ]
    assert r1 == r2


def test_filters_passed_to_text_leg_only():
    text = FakeText([_text("a", "d.pdf")])
    visual = FakeVisual([])
    FusedRetriever(text, visual).retrieve("q", filters={"entity": "ACME"}, top_k=8)
    assert text.calls[0][1] == {"entity": "ACME"}  # filters 透传给文本腿
    assert visual.calls[0] == ("q", 10)  # 视觉腿无 filters


# --- 隔离继承：输出恒为两腿输出子集，绝不造命中 ---


def test_output_is_subset_of_legs_no_fabrication():
    text_hits = [_text("a", "d.pdf"), _text("b", "d.pdf")]
    visual_hits = [_visual("e.pdf", 1)]
    out = FusedRetriever(FakeText(text_hits), FakeVisual(visual_hits)).retrieve("q")
    in_chunk_ids = {"a", "b"}
    in_visual = {("e.pdf", 1)}
    for h in out:
        if h.get("is_visual"):
            assert (h["doc_id"], h["page"]) in in_visual
        else:
            assert h["chunk_id"] in in_chunk_ids


def test_no_restricted_in_output_when_legs_strip():
    """两腿（按契约）已剔除 RESTRICTED -> 融合输出也无 RESTRICTED（继承）。"""
    text = FakeText([_text("a", "d.pdf")])  # 已是 link 出口剔除后的干净输出
    visual = FakeVisual([_visual("d.pdf", 9)])
    out = FusedRetriever(text, visual).retrieve("q")
    assert all(str(h.get("sensitivity", "")).upper() != RESTRICTED_SENSITIVITY for h in out)
