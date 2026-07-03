"""W12 隔离 conformance：视觉检索路径下 RESTRICTED 页绝不进视觉索引/编码/输出（+ reverse-proof）。

拍板（docs/invariants.md「RESTRICTED isolation」）：视觉检索是一条【新的可达 prompt 的路径】（同 W7
GraphStore / W10 RAPTOR「isolation at the door」），故 ColPaliVisualRetriever 在【建索引即门口】剔除
sensitivity==RESTRICTED 的页——RESTRICTED 页从不进入视觉索引、从不喂给视觉 embedder、从不出现在检索
输出。本文件断言该保护，并给出 reverse-proof（绕过检索器直喂 embedder 则会编码 -> 证明断言有牙）。
范式同 W11 rerank 隔离测试。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY
from ragspine.retrieval.vision.colpali import ColPaliVisualRetriever, VisualPage

SECRET = b"secret exec pay ratings restricted"


def _pages(restricted_sensitivity: str = RESTRICTED_SENSITIVITY):
    return [
        VisualPage(doc_id="d0", page_no=1, image=b"a b public"),
        VisualPage(doc_id="d1", page_no=2, image=SECRET, sensitivity=restricted_sensitivity),
        VisualPage(doc_id="d2", page_no=3, image=b"a public"),
    ]


def _embedded_images(captured) -> list[bytes]:
    """所有进入视觉 embedder.embed_images 的图像（跨调用展平）。"""
    return [img for call in captured["image_calls"] for img in call]


def test_restricted_page_never_enters_visual_index(fake_visual_embedder):
    """RESTRICTED 页不进视觉索引：检索器只索引非 RESTRICTED 页。"""
    embedder, _ = fake_visual_embedder()
    retriever = ColPaliVisualRetriever(embedder, _pages())
    assert [p.doc_id for p in retriever.pages] == ["d0", "d2"]


def test_restricted_image_never_embedded(fake_visual_embedder):
    """RESTRICTED 页图像绝不进入视觉编码（embed_images 从未见到 SECRET）。"""
    embedder, captured = fake_visual_embedder()
    ColPaliVisualRetriever(embedder, _pages()).retrieve("a b", top_k=10)
    embedded = _embedded_images(captured)
    assert SECRET not in embedded
    assert embedded == [b"a b public", b"a public"]


def test_restricted_page_never_in_output(fake_visual_embedder):
    """RESTRICTED 页绝不出现在检索输出（不出域）。"""
    embedder, _ = fake_visual_embedder()
    out = ColPaliVisualRetriever(embedder, _pages()).retrieve("a b", top_k=10)
    assert all(r["doc_id"] != "d1" for r in out)
    assert all(r["source_locator"] != "d1#page2" for r in out)


def test_restricted_case_insensitive(fake_visual_embedder):
    """sensitivity 大小写不敏感：'restricted' 同样不进视觉索引/编码。"""
    embedder, captured = fake_visual_embedder()
    retriever = ColPaliVisualRetriever(embedder, _pages(restricted_sensitivity="restricted"))
    retriever.retrieve("a b")
    assert all(p.doc_id != "d1" for p in retriever.pages)
    assert SECRET not in _embedded_images(captured)


def test_all_restricted_embedder_not_called(fake_visual_embedder):
    """全 RESTRICTED -> 视觉索引为空 -> retrieve 返回 [] 且 embedder 完全不被调用。"""
    embedder, captured = fake_visual_embedder()
    pages = [
        VisualPage(doc_id=f"d{i}", page_no=i + 1, image=b"secret", sensitivity=RESTRICTED_SENSITIVITY)
        for i in range(3)
    ]
    out = ColPaliVisualRetriever(embedder, pages).retrieve("q")
    assert out == []
    assert captured["query_calls"] == []
    assert captured["image_calls"] == []


def test_reverse_proof_direct_embed_would_encode_restricted(fake_visual_embedder):
    """reverse-proof：上面的断言不是空断言。

    若 RESTRICTED 页图像【绕过】ColPaliVisualRetriever 的门口剔除、被直接喂给 embedder.embed_images，
    视觉 embedder【确实】会把它编码——这证明隔离来自检索器的门口（建索引即剔除），而非 embedder 本身；
    上面的 conformance 断言能抓住任何绕过该门口的回归（若检索器漏过 RESTRICTED，embedded 里就会出现
    SECRET，断言即红）。
    """
    embedder, captured = fake_visual_embedder()
    embedder.embed_images([SECRET])
    assert SECRET in _embedded_images(captured)
