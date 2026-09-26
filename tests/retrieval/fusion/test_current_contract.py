"""融合历史分支时固定当前视觉字段、过滤隔离与默认路径合同。"""

from copy import deepcopy

import pytest
from ragspine.retrieval.fusion.route_fusion import FusedRetriever

from ragspine.retrieval.vision.colpali import ColPaliVisualRetriever, VisualPage


class Text:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def retrieve(self, query, *, filters=None, top_k=50):
        self.calls.append((query, filters, top_k))
        return self.hits[:top_k]


class Visual:
    def __init__(self, hits):
        self.hits = hits

    def retrieve(self, query, *, top_k=10):
        return self.hits[:top_k]


def hit(chunk_id, page, doc="d"):
    return {
        "chunk_id": chunk_id,
        "doc_id": doc,
        "page_no": page,
        "text": chunk_id,
        "source_locator": f"{doc}@page={page}#block=1",
    }


def visual(page, doc="d"):
    return {
        "doc_id": doc,
        "page_no": page,
        "text": "",
        "is_visual": True,
        "source_locator": f"{doc}#custom",
        "scores": {"visual_maxsim": 0.75},
    }


def test_direct_passthrough_does_not_expand_limit():
    text = Text([hit(str(i), i) for i in range(10)])
    out = FusedRetriever(text).retrieve("q", filters={"entity": "ACME"}, top_k=2)
    assert text.calls == [("q", {"entity": "ACME"}, 2)]
    assert out == text.hits[:2]


def test_page_no_and_current_score_merge_without_mutating_sources():
    text_hits = [hit("a", 1), hit("b", 2)]
    visual_hits = [visual(2)]
    saved = deepcopy((text_hits, visual_hits))
    out = FusedRetriever(Text(text_hits), Visual(visual_hits)).retrieve("q")
    assert len(out) == 2
    assert out[0]["chunk_id"] == "b"
    assert out[0]["visual_score"] == 0.75
    assert out[0]["retrieval_routes"] == ["text", "visual"]
    assert (text_hits, visual_hits) == saved


def test_same_leg_duplicate_page_does_not_vote_twice():
    text = Text([hit("a", 1), hit("a-child", 1), hit("b", 2)])
    out = FusedRetriever(text, Visual([])).retrieve("q")
    assert len(out) == 2
    assert out[0]["fused_score"] == pytest.approx(1 / 61)
    assert out[1]["fused_score"] == pytest.approx(1 / 62)


def test_filters_allow_only_visual_confirmation_of_filtered_text_pages():
    text = Text([hit("allowed", 1)])
    out = FusedRetriever(text, Visual([visual(2, "other"), visual(1)])).retrieve(
        "q", filters={"entity": "ACME"}
    )
    assert len(out) == 1
    assert out[0]["chunk_id"] == "allowed"
    assert out[0]["retrieval_routes"] == ["text", "visual"]


def test_restricted_hits_are_not_fused():
    text = Text([hit("allowed", 1), hit("secret", 2) | {"sensitivity": "RESTRICTED"}])
    out = FusedRetriever(text, Visual([visual(3) | {"sensitivity": "RESTRICTED"}])).retrieve("q")
    assert [row["chunk_id"] for row in out] == ["allowed"]


def test_zero_limit_is_empty():
    assert FusedRetriever(Text([hit("a", 1)]), Visual([visual(2)])).retrieve("q", top_k=0) == []


def test_current_colpali_retriever_integrates_without_gpu():
    class Embedder:
        def embed_query(self, query):
            return [[1.0, 0.0]]

        def embed_images(self, images):
            return [[[1.0, 0.0]] for _ in images]

    retriever = ColPaliVisualRetriever(
        [VisualPage(doc_id="d", page_no=2, image=b"synthetic", source_locator="custom")],
        Embedder(),
    )
    out = FusedRetriever(Text([hit("a", 1), hit("b", 2)]), retriever).retrieve("q")
    assert len(out) == 2
    assert out[0]["chunk_id"] == "b"
    assert out[0]["visual_score"] == 1.0
    assert out[0]["text"] == "b"
