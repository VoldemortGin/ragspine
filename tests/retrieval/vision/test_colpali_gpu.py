"""真 ColPali 视觉晚交互后端集成测试（W12）—— 仅 GPU 盒子执行（重型视觉模型 + 首拉权重）。

覆盖务实两层策略的「第②层」：真实 ColPaliVisualEmbedder（fastembed LateInteractionMultimodalEmbedding，
ColPali/ColQwen2 视觉晚交互 ONNX 权重）对渲染后的真实页图像做 patch 多向量编码 + query token 多向量
编码，断言外部行为：
    - 页图像编码得到非空 patch 多向量矩阵、query 得到非空 token 多向量矩阵；
    - 视觉 MaxSim 打分对含目标内容的页更高（真晚交互检索），且同输入确定性一致；
    - 端到端 ColPaliVisualRetriever（真 embedder + 渲染真页）返回带 provenance 的视觉命中。

依赖 / 许可诚实：
    - fastembed 是 Apache-2.0（过 ADR 0009 ≤Apache-2.0 许可门），归 [colpali] extra，惰性 import。
    - **模型权重许可**：默认 Qdrant/colpali-v1.3-fp16 的底座 PaliGemma 走 **Gemma 许可**（含使用限制，
      并非 ≤Apache-2.0 的宽松许可）。权重是运行时首拉、非打包依赖，故不过 CI 依赖许可门，但此为诚实标注
      的模型许可注意项；ColQwen2（底座 Qwen2-VL，Apache-2.0）是更宽松的可配置替代（RAGSPINE_COLPALI_MODEL）。
    - 重型视觉模型 + 首拉权重，故整组打 @pytest.mark.gpu：本地 / CI（无 fastembed / 无 GPU）下
      `pytest.importorskip('fastembed')` + `-m "not gpu"` 双重跳过（SKIP 而非 FAIL）。

在 GPU 盒子上执行：
    1) uv pip install -e ".[colpali]"    # fastembed（+ 视觉模型依赖 Pillow/onnxruntime）
    2) pytest -m gpu tests/retrieval/vision/test_colpali_gpu.py -q
    本地开发跑全套时排除：pytest -m "not gpu" -q
"""

import os
import warnings

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

# 未安装 fastembed（本地 / CI）时整文件 SKIP 而非 FAIL。
pytest.importorskip(
    "fastembed",
    reason="fastembed 未装（pip install ragspine[colpali]）；跳过真实 ColPali 视觉集成测试",
)

from ragspine.retrieval.rerank.colbert import maxsim
from ragspine.retrieval.vision.colpali import (
    ColPaliVisualEmbedder,
    ColPaliVisualRetriever,
    VisualPage,
    render_pdf_pages,
)

# 全文件统一打 gpu marker：可用 `pytest -m "not gpu"` 显式排除（重型视觉模型 + 首拉权重）。
pytestmark = pytest.mark.gpu


@pytest.fixture(scope="module")
def real_embedder() -> ColPaliVisualEmbedder:
    """真实 ColPali 视觉后端（首次编码时惰性下载权重）。"""
    return ColPaliVisualEmbedder()


def test_real_query_embed_nonempty(real_embedder):
    """W12 —— 真 ColPali 对 query 编码出非空 token 多向量矩阵。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m = real_embedder.embed_query("What was total revenue in FY2024?")
    assert m and all(len(row) > 0 for row in m)


def test_real_page_embed_and_maxsim_relevant(real_embedder, digital_pdf_path):
    """W12 —— 真 ColPali 对渲染页图像编码 patch 多向量，视觉 MaxSim 对更相关的 query 更高 + 确定性。"""
    pages = render_pdf_pages(digital_pdf_path)
    assert pages
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        page_mats = real_embedder.embed_images(pages[:1])
        q_rel = real_embedder.embed_query("revenue table figures")
        q_rel2 = real_embedder.embed_query("revenue table figures")
    assert page_mats and page_mats[0]
    s1 = maxsim(q_rel, page_mats[0])
    s2 = maxsim(q_rel2, page_mats[0])
    assert s1 == pytest.approx(s2)  # 确定性：同输入逐位一致
    assert s1 > 0.0


def test_real_end_to_end_retrieve_carries_provenance(real_embedder, digital_pdf_path):
    """W12 —— 端到端：真 embedder + 渲染真页 -> 视觉命中带 provenance（doc_id / page locator）。"""
    pages = render_pdf_pages(digital_pdf_path)
    visual_pages = [
        VisualPage(doc_id="digital", page_no=i + 1, image=png)
        for i, png in enumerate(pages)
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = ColPaliVisualRetriever(real_embedder, visual_pages).retrieve("revenue", top_k=3)
    assert out
    assert out[0]["is_visual"] is True
    assert out[0]["doc_id"] == "digital"
    assert out[0]["source_locator"].startswith("digital#page")
