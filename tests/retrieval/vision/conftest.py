"""视觉文档检索测试夹具（W12 ColPali）：注入 fake 视觉 embedder（零网络、零安装、零 GPU）。

两类替身：
- fake_visual_embedder：直接实现 VisualEmbedder 协议的确定性 stub（注入给 ColPaliVisualRetriever
  编排/隔离测试）。语义同 rerank/conftest.py 的 fake_colbert：query 与「图像内容」都按空白切 token、
  每个 token 取首字母映射成 26 维正交 one-hot，故 MaxSim(query, page)=共享字母词数——可控可复现。
  注意：这里「图像」用可解码成 ASCII 的 bytes 承载伪内容（stub 把 bytes.decode() 当文本），真实
  ColPali 才对 PNG patch 做视觉编码——这是 stub，编排/隔离逻辑与真实模型解耦。
- fake_colpali：sys.modules 替身，顶替 `fastembed.LateInteractionMultimodalEmbedding`（真 ColPali
  视觉晚交互后端），供 ColPaliVisualEmbedder 的惰性构造 / 透传 / 条数校验单测（范式同 fake_colbert）。
"""

import io
import sys
import types

import pytest


def _char_onehot(token: str) -> list[float]:
    """token（取首字符）确定性映射到 26 维字母 one-hot；非字母 -> 零向量（同 rerank fake_colbert）。"""
    v = [0.0] * 26
    c = token[:1].lower()
    if "a" <= c <= "z":
        v[ord(c) - 97] = 1.0
    return v


def _matrix(text: str) -> list[list[float]]:
    """把一段文本按空白切 token，每 token -> 26 维正交 one-hot，得到一个 (n_token, 26) 多向量矩阵。"""
    return [_char_onehot(t) for t in text.split()] or [[0.0] * 26]


@pytest.fixture
def fake_visual_embedder():
    """返回工厂：fake_visual_embedder() -> (embedder, captured)。

    embedder 实现 VisualEmbedder 协议（embed_query / embed_images），确定性、无依赖。
    captured["query_calls"]：每次 embed_query(query) 的 query。
    captured["image_calls"]：每次 embed_images(images) 的图像 bytes 列表（用于断 RESTRICTED 图像
    绝不进入编码）。fake 语义：query / 图像内容（bytes.decode）都做 token 首字母 one-hot -> MaxSim
    可控。
    """

    def _make(*, drop_last: bool = False):
        captured: dict = {"query_calls": [], "image_calls": []}

        class _FakeVisualEmbedder:
            def embed_query(self, query: str) -> list[list[float]]:
                captured["query_calls"].append(query)
                return _matrix(query)

            def embed_images(self, images) -> list[list[list[float]]]:
                imgs = list(images)
                captured["image_calls"].append(imgs)
                out = [_matrix(b.decode("utf-8", "ignore")) for b in imgs]
                if drop_last:
                    out = out[:-1]
                return out

        return _FakeVisualEmbedder(), captured

    return _make


def _tiny_png() -> bytes:
    """生成一张最小 PNG（4x4 纯色）字节串——用于 fake_colpali 单测里真实的 bytes->PIL 转换路径。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def tiny_png() -> bytes:
    """一张最小合法 PNG 字节串（供 ColPaliVisualEmbedder.embed_images 的 bytes->PIL 路径单测）。"""
    return _tiny_png()


@pytest.fixture
def fake_colpali(monkeypatch):
    """返回安装器：fake_colpali(drop_last=...) -> captured（注入 fake LateInteractionMultimodalEmbedding）。

    captured["init_kwargs"]：LateInteractionMultimodalEmbedding 构造参数（model_name + cache_dir/threads）。
    captured["image_calls"]：每次 embed_image(images) 传入的图像对象列表（真实实现里为 PIL.Image）。
    captured["text_calls"]：每次 embed_text(query) 的 query。
    fake 语义：每张图像给一个固定 2-patch 矩阵，query 给一个固定 token 矩阵——只验证透传/条数/惰性，
    不验证打分数值（数值语义由 fake_visual_embedder + maxsim 单测覆盖）。drop_last：embed_image 少产一条。
    """

    def _install(*, drop_last: bool = False):
        captured: dict = {"init_kwargs": None, "image_calls": [], "text_calls": []}

        class _FakeMultimodal:
            def __init__(self, model_name=None, **kwargs):
                captured["init_kwargs"] = {"model_name": model_name, **kwargs}

            def embed_text(self, documents, **kwargs):
                captured["text_calls"].append(documents)
                return iter([[[1.0, 0.0], [0.0, 1.0]]])

            def embed_image(self, images, **kwargs):
                imgs = list(images)
                captured["image_calls"].append(imgs)
                out = [[[1.0, 0.0], [0.0, 1.0]] for _ in imgs]
                if drop_last:
                    out = out[:-1]
                return iter(out)

        fake = types.ModuleType("fastembed")
        fake.LateInteractionMultimodalEmbedding = _FakeMultimodal
        monkeypatch.setitem(sys.modules, "fastembed", fake)
        return captured

    return _install
