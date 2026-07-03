"""ColPali 视觉文档检索（W12）单元测试：视觉 MaxSim 复用 + page→image 编排 + 工厂/接线 + 真模型骨架。

设计意图（docs/prd-quality-depth.md W12）：给检索补上一条与家族 OCR→文本（W3a）**平行**的强路线——
直接对文档【页图像】做晚交互（视觉 patch 多向量 vs query token 多向量 MaxSim），不经 OCR→文本，保
版面/图表视觉结构。落地方式（务实，同 W11）：一个 VisualEmbedder 缝 + ColPaliVisualRetriever 确定性
可测编排（视觉 MaxSim **复用 W11 colbert.maxsim**、page→image 渲染复用 pypdfium2），真 ColPali 权重
加载/编码打 @pytest.mark.gpu（见 test_colpali_gpu.py）。默认 opt-in，不接默认 loop → 字节不变。

红色策略 / 离线性（同 W11）：
- 编排/隔离单测用注入的 fake 视觉 embedder（fake_visual_embedder 夹具），零网络、零 GPU、零真装；
- ColPaliVisualEmbedder 的惰性构造 / 透传 / 条数校验用 fake fastembed（fake_colpali，sys.modules 替身）；
- 真 ColPali 的加载/编码/相关性只在 @pytest.mark.gpu 用例里跑，CI 默认 `-m "not gpu"` 跳过。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.rerank.colbert import maxsim as colbert_maxsim
from ragspine.retrieval.vision.colpali import (
    DEFAULT_COLPALI_MODEL,
    ColPaliVisualEmbedder,
    ColPaliVisualRetriever,
    VisualEmbedder,
    VisualPage,
    make_visual_embedder,
    maxsim,
    render_pdf_pages,
)


# ---------------------------------------------------------------------------
# 视觉 MaxSim 复用 W11 colbert.maxsim（同一 sum-of-max-cosine 逻辑，不重复造轮子）
# ---------------------------------------------------------------------------

def test_visual_maxsim_reuses_colbert_maxsim():
    """W12 视觉 MaxSim 打分【复用】W11 colbert.py 的 maxsim（同一函数对象，非复制）。"""
    assert maxsim is colbert_maxsim


def test_visual_maxsim_sum_of_max_cosine():
    """视觉 patch 多向量 vs query token 多向量的 MaxSim = 逐 query token 取最大 cosine 之和。"""
    q = [[1.0, 0.0], [0.0, 1.0]]
    patches = [[1.0, 0.0]]
    assert maxsim(q, patches) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# VisualPage：页图像候选 + provenance locator
# ---------------------------------------------------------------------------

def test_visual_page_default_locator():
    """未显式给 source_locator 时，locator 取 '{doc_id}#page{N}'（provenance 缺省）。"""
    page = VisualPage(doc_id="rep2024", page_no=7, image=b"a b")
    assert page.locator == "rep2024#page7"


def test_visual_page_explicit_locator_wins():
    """显式 source_locator 覆盖缺省（保源引用可控）。"""
    page = VisualPage(doc_id="d", page_no=1, image=b"x", source_locator="d#custom")
    assert page.locator == "d#custom"


def test_visual_page_default_sensitivity_internal():
    """缺省 sensitivity 为 INTERNAL（非 RESTRICTED，正常可检索）。"""
    assert VisualPage(doc_id="d", page_no=1, image=b"x").sensitivity == "INTERNAL"


# ---------------------------------------------------------------------------
# render_pdf_pages：page→image 渲染复用 pypdfium2（已是基础依赖，离线可测）
# ---------------------------------------------------------------------------

def test_render_pdf_pages_returns_png_bytes(digital_pdf_path):
    """digital.pdf 逐页渲染为非空 PNG bytes（PNG magic 头），页数 >= 1。"""
    pages = render_pdf_pages(digital_pdf_path)
    assert isinstance(pages, list)
    assert len(pages) >= 1
    for png in pages:
        assert isinstance(png, (bytes, bytearray))
        assert bytes(png[:8]) == b"\x89PNG\r\n\x1a\n"  # PNG 签名


def test_render_pdf_pages_unreadable_returns_empty(tmp_path):
    """不可读 / 不存在的路径 -> 返回 []（不抛，调用方据此不建视觉索引）。"""
    assert render_pdf_pages(tmp_path / "nope.pdf") == []


# ---------------------------------------------------------------------------
# ColPaliVisualRetriever：视觉晚交互检索编排（MaxSim 打分 + provenance + is_visual）
# ---------------------------------------------------------------------------

def _pages():
    """三页伪内容（bytes 承载 ASCII，fake 视觉 embedder 按文本 token 首字母 one-hot 编码）。"""
    return [
        VisualPage(doc_id="d0", page_no=1, image=b"x y z"),      # 与 'a b' 无共享 -> 0
        VisualPage(doc_id="d1", page_no=2, image=b"a b c"),      # 命中 a,b -> 2（最强）
        VisualPage(doc_id="d2", page_no=3, image=b"a"),          # 命中 a -> 1
    ]


def test_retriever_ranks_by_visual_maxsim_descending(fake_visual_embedder):
    """视觉 MaxSim 降序给名次：query 'a b' -> 命中最多的页在最前（真晚交互检索）。"""
    embedder, _ = fake_visual_embedder()
    out = ColPaliVisualRetriever(embedder, _pages()).retrieve("a b", top_k=10)
    order = [r["doc_id"] for r in out]
    assert order == ["d1", "d2", "d0"]


def test_retriever_snippet_carries_provenance(fake_visual_embedder):
    """每条视觉命中带 provenance（doc_id / page locator / page_no），可溯源到具体页。"""
    embedder, _ = fake_visual_embedder()
    out = ColPaliVisualRetriever(embedder, _pages()).retrieve("a b", top_k=1)
    top = out[0]
    assert top["doc_id"] == "d1"
    assert top["source_locator"] == "d1#page2"
    assert top["page_no"] == 2
    assert top["scores"]["visual_maxsim"] == pytest.approx(2.0)


def test_retriever_marks_visual_and_no_fabricated_text(fake_visual_embedder):
    """视觉命中标 is_visual=True 且不带可引用文本（检索线索，非 citable fact 的编造源）。

    反编造：数字仍归结构化通路，视觉命中只给页引用，不注入任何伪造正文。"""
    embedder, _ = fake_visual_embedder()
    out = ColPaliVisualRetriever(embedder, _pages()).retrieve("a b", top_k=1)
    assert out[0]["is_visual"] is True
    assert out[0]["text"] == ""


def test_retriever_top_k_truncates(fake_visual_embedder):
    """top_k 截断到前 k 条（按视觉 MaxSim 降序）。"""
    embedder, _ = fake_visual_embedder()
    out = ColPaliVisualRetriever(embedder, _pages()).retrieve("a b", top_k=2)
    assert [r["doc_id"] for r in out] == ["d1", "d2"]


def test_retriever_empty_index_returns_empty(fake_visual_embedder):
    """空视觉索引 -> retrieve 返回 []，不触发任何编码。"""
    embedder, captured = fake_visual_embedder()
    assert ColPaliVisualRetriever(embedder, []).retrieve("a b") == []
    assert captured["query_calls"] == []
    assert captured["image_calls"] == []


def test_retriever_deterministic_two_instances(fake_visual_embedder):
    """同输入两个独立检索器给出完全一致名次（确定性 conformance，fake 层面）。"""
    e1, _ = fake_visual_embedder()
    e2, _ = fake_visual_embedder()
    a = ColPaliVisualRetriever(e1, _pages()).retrieve("a b")
    b = ColPaliVisualRetriever(e2, _pages()).retrieve("a b")
    assert [r["source_locator"] for r in a] == [r["source_locator"] for r in b]


def test_retriever_ties_keep_index_order(fake_visual_embedder):
    """平分时保持原索引序——稳定排序，确定性。"""
    embedder, _ = fake_visual_embedder()
    pages = [
        VisualPage(doc_id="p0", page_no=1, image=b"a"),
        VisualPage(doc_id="p1", page_no=2, image=b"a"),
        VisualPage(doc_id="p2", page_no=3, image=b"a"),
    ]
    out = ColPaliVisualRetriever(embedder, pages).retrieve("a")
    assert [r["doc_id"] for r in out] == ["p0", "p1", "p2"]


def test_retriever_image_count_mismatch_raises(fake_visual_embedder):
    """embed_images 返回条数与页数不一致 -> 抛错（绝不静默给坏名次）。"""
    embedder, _ = fake_visual_embedder(drop_last=True)
    with pytest.raises((RuntimeError, ValueError)):
        ColPaliVisualRetriever(embedder, _pages()).retrieve("a b")


def test_retriever_only_embeds_once_per_query(fake_visual_embedder):
    """一次 retrieve 只编码一次 query + 一批页图像（不逐页重复调用）。"""
    embedder, captured = fake_visual_embedder()
    ColPaliVisualRetriever(embedder, _pages()).retrieve("a b")
    assert captured["query_calls"] == ["a b"]
    assert len(captured["image_calls"]) == 1
    assert captured["image_calls"][0] == [b"x y z", b"a b c", b"a"]


# ---------------------------------------------------------------------------
# ColPaliVisualEmbedder：真 fastembed 后端（惰性构造 / 友好报错 / 透传 / 校验）
# ---------------------------------------------------------------------------

def test_embedder_ctor_is_lazy_no_fastembed(monkeypatch):
    """构造惰性：未装 fastembed 也能构造（模型在首次编码时才加载）。"""
    import sys

    monkeypatch.setitem(sys.modules, "fastembed", None)
    e = ColPaliVisualEmbedder()
    assert e.model_name == DEFAULT_COLPALI_MODEL


def test_embedder_without_fastembed_raises_friendly(monkeypatch, tiny_png):
    """首次编码缺 fastembed -> 友好提示（含 fastembed / colpali extra 名）。"""
    import sys

    monkeypatch.setitem(sys.modules, "fastembed", None)
    with pytest.raises(ImportError) as exc:
        ColPaliVisualEmbedder().embed_images([tiny_png])
    msg = str(exc.value).lower()
    assert "fastembed" in msg
    assert "colpali" in msg


def test_embedder_empty_images_no_load(monkeypatch):
    """空图像列表 -> 返回 []，不触发任何 import / 模型加载。"""
    import sys

    monkeypatch.setitem(sys.modules, "fastembed", None)  # 若误加载会抛
    assert ColPaliVisualEmbedder().embed_images([]) == []


def test_embedder_invalid_batch_size_rejected():
    """batch_size < 1 -> ValueError（构造期即拒）。"""
    with pytest.raises(ValueError):
        ColPaliVisualEmbedder(batch_size=0)


def test_embedder_implements_protocol():
    """ColPaliVisualEmbedder 满足 @runtime_checkable VisualEmbedder 协议。"""
    assert isinstance(ColPaliVisualEmbedder(), VisualEmbedder)


def test_embedder_default_model_constant():
    """默认视觉模型名集中一处（fastembed 支持的 ColPali 视觉晚交互 ONNX 权重）。"""
    assert DEFAULT_COLPALI_MODEL == "Qdrant/colpali-v1.3-fp16"


def test_embedder_query_uses_embed_text(fake_colpali):
    """embed_query 走 fastembed 的 embed_text（文本→query token 多向量）。"""
    captured = fake_colpali()
    m = ColPaliVisualEmbedder().embed_query("what revenue")
    assert captured["text_calls"] == ["what revenue"]
    assert m == [[1.0, 0.0], [0.0, 1.0]]


def test_embedder_images_use_embed_image(fake_colpali, tiny_png):
    """embed_images 走 fastembed 的 embed_image（PNG bytes -> PIL -> patch 多向量）。"""
    captured = fake_colpali()
    mats = ColPaliVisualEmbedder().embed_images([tiny_png, tiny_png])
    assert len(mats) == 2
    assert len(captured["image_calls"]) == 1
    assert len(captured["image_calls"][0]) == 2  # 两张图各转成一个 PIL 对象


def test_embedder_image_count_mismatch_raises(fake_colpali, tiny_png):
    """embed_image 返回条数与图像数不一致 -> 抛错（绝不静默给坏向量）。"""
    captured = fake_colpali(drop_last=True)
    with pytest.raises((RuntimeError, ValueError)):
        ColPaliVisualEmbedder().embed_images([tiny_png, tiny_png])


def test_embedder_model_override_passed(fake_colpali, tiny_png):
    """model_name 可覆盖并透传给 LateInteractionMultimodalEmbedding。"""
    captured = fake_colpali()
    ColPaliVisualEmbedder(model_name="Qdrant/colqwen2-v0.1").embed_images([tiny_png])
    assert captured["init_kwargs"]["model_name"] == "Qdrant/colqwen2-v0.1"


def test_embedder_cache_dir_threads_passthrough(fake_colpali, tiny_png):
    """cache_dir / threads 透传给 LateInteractionMultimodalEmbedding（缺省不传）。"""
    captured = fake_colpali()
    ColPaliVisualEmbedder(cache_dir="/tmp/cp", threads=3).embed_images([tiny_png])
    assert captured["init_kwargs"]["cache_dir"] == "/tmp/cp"
    assert captured["init_kwargs"]["threads"] == 3


def test_embedder_model_loaded_once(fake_colpali, tiny_png):
    """模型延迟加载且只构造一次（跨多次编码复用同一后端）。"""
    import sys

    fake_colpali()
    fake_mod = sys.modules["fastembed"]
    calls = {"n": 0}
    orig = fake_mod.LateInteractionMultimodalEmbedding

    class _Counting(orig):
        def __init__(self, *a, **k):
            calls["n"] += 1
            super().__init__(*a, **k)

    fake_mod.LateInteractionMultimodalEmbedding = _Counting
    e = ColPaliVisualEmbedder()
    e.embed_query("q")
    e.embed_images([tiny_png])
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 工厂 make_visual_embedder：opt-in（默认 None）+ 别名 + env + 模型覆盖
# ---------------------------------------------------------------------------

def test_factory_none_returns_none():
    """默认（None / 'none'）-> None：不启用视觉检索，opt-in，默认 loop 字节不变。"""
    assert make_visual_embedder(None) is None
    assert make_visual_embedder("none") is None


@pytest.mark.parametrize("spec", ["colpali", "colqwen2", "visual", "COLPALI", " colpali "])
def test_factory_aliases_return_embedder(spec):
    """'colpali' 及其别名（含大小写/留白归一）-> ColPaliVisualEmbedder（构造惰性）。"""
    assert isinstance(make_visual_embedder(spec), ColPaliVisualEmbedder)


def test_factory_unknown_raises():
    """未知 spec -> ValueError（Registry 列清可用名）。"""
    with pytest.raises(ValueError):
        make_visual_embedder("bogus")


def test_factory_via_env(monkeypatch):
    """缺省 spec 读 env RAGSPINE_VISUAL_EMBEDDER=colpali -> ColPaliVisualEmbedder。"""
    monkeypatch.setenv("RAGSPINE_VISUAL_EMBEDDER", "colpali")
    assert isinstance(make_visual_embedder(), ColPaliVisualEmbedder)


def test_factory_model_override_via_env(fake_colpali, tiny_png, monkeypatch):
    """缺省模型时读 RAGSPINE_COLPALI_MODEL 覆盖（仅对 colpali 系 spec）。"""
    captured = fake_colpali()
    monkeypatch.setenv("RAGSPINE_COLPALI_MODEL", "Qdrant/colqwen2-v0.1")
    make_visual_embedder("colpali").embed_images([tiny_png])
    assert captured["init_kwargs"]["model_name"] == "Qdrant/colqwen2-v0.1"


def test_factory_model_override_via_kwarg(fake_colpali, tiny_png):
    """kwargs 显式 model_name 优先于 env（透传给后端）。"""
    captured = fake_colpali()
    make_visual_embedder("colpali", model_name="Qdrant/colpali-v1.3-fp16").embed_images([tiny_png])
    assert captured["init_kwargs"]["model_name"] == "Qdrant/colpali-v1.3-fp16"
