"""vision —— 视觉文档检索：ColPali/ColQwen2 page-as-image 晚交互（GPU/opt-in，默认关）。

与家族 OCR→文本（extraction 的 W3a 扫描线）**平行**的另一条强路线：直接对文档【页图像】做晚交互
（视觉 patch 多向量 vs query token 多向量 MaxSim），不经 OCR→文本，保版面/图表视觉结构；对图表密集
金融报告往往更强。默认 opt-in（工厂缺省 None、不接默认 loop）→ 检索/answer 字节不变。视觉命中带
provenance（doc_id/page locator）、标 is_visual（检索线索非 citable fact 的编造源），RESTRICTED 页在
建索引即门口剔除（隔离，含 reverse-proof）。

Submodules:
    colpali.py — ColPali 视觉文档检索（W12）：VisualEmbedder 缝 + ColPaliVisualRetriever 编排（视觉 MaxSim 复用 rerank/colbert.maxsim、page→image 渲染复用 pypdfium2）+ 真 fastembed LateInteractionMultimodalEmbedding 后端（惰性、[colpali]、@pytest.mark.gpu）+ make_visual_embedder 工厂。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
