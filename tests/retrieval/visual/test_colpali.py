"""W12 ColPali 视觉检索单测：patch MaxSim 打分 + 血缘 + RESTRICTED 出口剔除 + 工厂。

全部用 FakeVisualBackend 替身（零 GPU、零模型、零网络）。重点验证：按 MaxSim 排序、provenance、
RESTRICTED 页绝不嵌入/返回、确定性、构造极轻（不加载 fastembed）。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY
from ragspine.retrieval.visual.colpali import (
    ColPaliRetriever,
    FastEmbedColPaliBackend,
    PageImage,
    VisualMultiVectorBackend,
    make_colpali_retriever,
)


class FakeVisualBackend:
    """按 image-key -> patch 向量映射的替身，记录被嵌入的图像（供隔离断言）。"""

    def __init__(self, query_vecs, image_map):
        self.query_vecs = query_vecs
        self.image_map = image_map
        self.embedded_images: list = []

    def embed_query(self, query):
        return self.query_vecs

    def embed_images(self, images):
        self.embedded_images.extend(images)
        return [self.image_map[img] for img in images]


def _page(doc_id, page, image, sensitivity="INTERNAL"):
    return PageImage(
        doc_id=doc_id, page=page, image=image,
        source_locator=f"{doc_id}#page{page}", sensitivity=sensitivity,
    )


def test_ranks_pages_by_maxsim():
    backend = FakeVisualBackend(
        query_vecs=[[1.0, 0.0]],
        image_map={
            "imgA": [[1.0, 0.0]],   # MaxSim 1.0
            "imgB": [[0.0, 1.0]],   # MaxSim 0.0
        },
    )
    pages = [_page("d1.pdf", 1, "imgB"), _page("d1.pdf", 2, "imgA")]
    out = ColPaliRetriever(pages, backend).retrieve("q")
    assert [r["page"] for r in out] == [2, 1], "高分页（page2/imgA）排前"
    assert out[0]["doc_id"] == "d1.pdf"
    assert out[0]["source_locator"] == "d1.pdf#page2"
    assert out[0]["is_visual"] is True
    assert "colpali_maxsim" in out[0]["scores"]


def test_restricted_page_never_embedded_or_returned():
    """RESTRICTED 页在入口剔除：绝不进 embed_images、绝不出现在结果。"""
    backend = FakeVisualBackend(
        query_vecs=[[1.0, 0.0]],
        image_map={"pub": [[1.0, 0.0]]},  # 注意：未给 secret 的向量——若被嵌入会 KeyError
    )
    pages = [
        _page("pub.pdf", 1, "pub"),
        _page("secret.pdf", 1, "secret", sensitivity=RESTRICTED_SENSITIVITY),
    ]
    out = ColPaliRetriever(pages, backend).retrieve("q")
    assert "secret" not in backend.embedded_images
    assert all(r["doc_id"] != "secret.pdf" for r in out)
    assert [r["doc_id"] for r in out] == ["pub.pdf"]


def test_top_k_truncates():
    backend = FakeVisualBackend(
        [[1.0]], {f"i{n}": [[1.0]] for n in range(5)}
    )
    pages = [_page("d.pdf", n, f"i{n}") for n in range(5)]
    out = ColPaliRetriever(pages, backend).retrieve("q", top_k=2)
    assert len(out) == 2


def test_empty_pages_returns_empty():
    backend = FakeVisualBackend([[1.0]], {})
    assert ColPaliRetriever([], backend).retrieve("q") == []
    assert backend.embedded_images == []


def test_deterministic():
    backend = FakeVisualBackend(
        [[1.0, 0.0]], {"a": [[1.0, 0.0]], "b": [[0.0, 1.0]]}
    )
    pages = [_page("d.pdf", 1, "a"), _page("d.pdf", 2, "b")]
    r1 = [r["page"] for r in ColPaliRetriever(pages, backend).retrieve("q")]
    r2 = [r["page"] for r in ColPaliRetriever(pages, backend).retrieve("q")]
    assert r1 == r2


def test_page_vectors_embedded_once_cached():
    backend = FakeVisualBackend([[1.0]], {"a": [[1.0]]})
    r = ColPaliRetriever([_page("d.pdf", 1, "a")], backend)
    r.retrieve("q1")
    r.retrieve("q2")
    assert backend.embedded_images == ["a"], "页图像只嵌入一次并缓存"


def test_fastembed_backend_constructs_without_fastembed():
    """适配器构造极轻：不 import fastembed、不加载模型（没装 [colpali] / 无 GPU 也能构造）。"""
    b = FastEmbedColPaliBackend()
    assert b.model_name
    assert b._model is None


def test_make_colpali_retriever_factory():
    backend = FakeVisualBackend([[1.0]], {})
    r = make_colpali_retriever([], backend)
    assert isinstance(r, ColPaliRetriever)


def test_make_reads_model_env(monkeypatch):
    monkeypatch.setenv("RAGSPINE_COLPALI_MODEL", "vidore/colqwen2-v0.1")
    r = make_colpali_retriever([])
    assert r.model_name == "vidore/colqwen2-v0.1"


def test_runtime_checkable_protocol():
    assert isinstance(FakeVisualBackend([], {}), VisualMultiVectorBackend)
