"""ColPali 视觉文档检索（W12）：page-as-image 晚交互（视觉 patch 多向量 vs query token 多向量 MaxSim）。

现状（docs/prd-quality-depth.md W12）：检索只有文本通路——家族 OCR→文本（extraction 的 W3a 扫描线）
在问题依赖版面/图表/图形位置时会丢视觉结构。本模块给检索补上一条**平行**的强路线（不替代 W3a）：
直接对文档【页图像】做 late-interaction（ColPali/ColQwen2 视觉晚交互），不经 OCR→文本，保版面/图表
视觉结构，对图表密集金融报告往往明显更强（对标 LlamaIndex ColPali、Weaviate/Vespa ColPali、2025 前沿
ColPali/ColQwen2）。

落地方式（务实优先，同 W11）：一个 VisualEmbedder 缝 + ColPaliVisualRetriever 确定性可测编排——
- **视觉 MaxSim 打分复用 W11 colbert.py 的 maxsim**（同一 sum-of-max-cosine 逻辑：视觉 patch 多向量
  vs query token 多向量，逐 query token 取其对任一 patch 的最大 cosine 求和）——不重复造轮子；
- **page→image 渲染复用 pypdfium2**（已是基础依赖，与 extraction 的 pdf_scanned_extractor 同款
  逐页 render→PNG）——render_pdf_pages 纯离线可测；
- 真 ColPali 视觉后端 ColPaliVisualEmbedder 经 fastembed 的 LateInteractionMultimodalEmbedding
  （Apache-2.0，过 ADR 0009 ≤Apache-2.0 许可门），延迟 import、惰性加载，归 [colpali] extra
  （复用 fastembed，与 W1/W2/W11 同范式）。重型视觉模型 + 首拉权重，真加载/编码打 @pytest.mark.gpu
  （见 tests/retrieval/vision/test_colpali_gpu.py），CI 默认 `-m "not gpu"` 跳过。

**模型许可诚实声明（不可省略）**：**代码依赖** fastembed 是 Apache-2.0（过 ADR 0009 依赖许可门）；但
**模型权重**另有其许可——默认 Qdrant/colpali-v1.3-fp16 的底座 PaliGemma 走 **Gemma 许可**（含使用限制，
并非 ≤Apache-2.0 的宽松许可）。权重是运行时首拉、非打包依赖，故不过 CI 依赖许可门，但此为诚实标注的
模型许可注意项。ColQwen2（底座 Qwen2-VL，Apache-2.0）是更宽松的可配置替代——可经 RAGSPINE_COLPALI_MODEL
指定；模型名的最终宽松选型（含 ColQwen2 的 fastembed 可用性）是 follow-up（见 docs/prd-quality-depth.md）。

**离线性诚实声明（同 W1/W2/W11）**：fastembed 首次会从 HuggingFace 下载权重再缓存（"首拉后离线"，
不是首跑即离线）。真正的"首跑即离线"需把权重做成数据包随包出货——follow-up。

**默认行为不变**：make_visual_embedder 缺省（None/'none'）返回 None，视觉检索是 opt-in 新增，不接
默认文本检索 loop（build_narrative_retriever / answer_question 均不涉及本模块）——检索/answer 字节不变。

**隔离不变量（新出口，isolation at the door）**：视觉检索是一条【新的可达 prompt 的路径】（同 W7
GraphStore / W10 RAPTOR），故 ColPaliVisualRetriever 在**建索引即门口**剔除 sensitivity==RESTRICTED
的页——RESTRICTED 页从不进视觉索引、从不喂给视觉 embedder、从不出现在检索输出（conformance 见
tests/retrieval/vision/test_colpali_isolation.py，含 reverse-proof）。**反编造**：视觉命中标
is_visual=True、不带可引用正文（text=""），只给页引用（doc_id/page locator）作检索线索——数字仍归
结构化通路，视觉命中绝非 citable fact 的编造源。

maxsim：本模块直接复用 rerank/colbert.py 的晚交互打分纯函数（re-export，同一函数对象），可脱离 fastembed 单测。
"""

import io
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pypdfium2 as pdfium
from corespine import Registry, lazy_extra_import

# 视觉 MaxSim 打分**复用** W11 colbert.py 的 maxsim（同一 sum-of-max-cosine 逻辑，不重复造轮子）。
# re-export 以便调用方/单测从本模块取用；`maxsim is rerank.colbert.maxsim`。
from ragspine.retrieval.rerank.colbert import maxsim
from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY

__all__ = [
    "DEFAULT_COLPALI_MODEL",
    "COLPALI_MODEL_ENV",
    "VISUAL_EMBEDDER_ENV",
    "DEFAULT_COLPALI_BATCH_SIZE",
    "VisualPage",
    "VisualEmbedder",
    "ColPaliVisualEmbedder",
    "ColPaliVisualRetriever",
    "make_visual_embedder",
    "render_pdf_pages",
    "maxsim",
]

# 默认 ColPali 视觉模型名（唯一出处）。选 Qdrant/colpali-v1.3-fp16：fastembed 支持的 ColPali 视觉晚
# 交互 ONNX 权重。**注意模型许可**：底座 PaliGemma 走 Gemma 许可（见模块 docstring 的诚实声明）。
DEFAULT_COLPALI_MODEL = "Qdrant/colpali-v1.3-fp16"

# ColPali 模型覆盖环境变量名（缺省模型时生效，仅对 colpali 系 spec，由 make_visual_embedder 注入）。
COLPALI_MODEL_ENV = "RAGSPINE_COLPALI_MODEL"

# 视觉 embedder 选型读取的环境变量名（缺省 spec 时生效）。
VISUAL_EMBEDDER_ENV = "RAGSPINE_VISUAL_EMBEDDER"

# 默认视觉编码分批大小（透传给 LateInteractionMultimodalEmbedding.embed_image）。
DEFAULT_COLPALI_BATCH_SIZE = 8

# 页图像渲染 DPI（越高越清晰、越慢；与 extraction/pdf_scanned_extractor.RENDER_DPI 同量级）。
RENDER_DPI = 200


@dataclass(frozen=True)
class VisualPage:
    """一页文档的视觉检索候选（页图像 + provenance）。

    字段：
        doc_id:          源文档 id（血缘）。
        page_no:         页号（1-based，血缘）。
        image:           页图像字节（PNG bytes；由 render_pdf_pages 渲染或调用方提供）。
        sensitivity:     敏感级（默认 INTERNAL；RESTRICTED 页在建索引即门口剔除）。
        title:           可选文档标题（透传到 snippet，便于展示）。
        source_locator:  可选显式源引用；缺省取 '{doc_id}#page{page_no}'。
    """

    doc_id: str
    page_no: int
    image: bytes
    sensitivity: str = "INTERNAL"
    title: str | None = None
    source_locator: str | None = None

    @property
    def locator(self) -> str:
        """源引用：显式 source_locator 优先，否则缺省 '{doc_id}#page{page_no}'。"""
        return self.source_locator or f"{self.doc_id}#page{self.page_no}"


@runtime_checkable
class VisualEmbedder(Protocol):
    """视觉晚交互 embedder 协议（依赖注入点）。

    任何实现本协议的对象都可注入 ColPaliVisualRetriever：真后端用 ColPaliVisualEmbedder
    （fastembed，GPU/首拉权重），确定性单测用 fake stub。把视觉编码抽象成协议，使 MaxSim 打分 /
    page→image 编排 / RESTRICTED 隔离的逻辑可在无 GPU 的机器上完整确定性测试。
    """

    def embed_query(self, query: str) -> list[list[float]]:
        """query 文本 -> query token 级多向量矩阵（每行一个 token 的向量）。"""
        ...

    def embed_images(self, images: Sequence[bytes]) -> list[list[list[float]]]:
        """一批页图像（PNG bytes）-> 每张一个 patch 级多向量矩阵（顺序与输入对齐）。"""
        ...


def _to_matrix(rows: Any) -> list[list[float]]:
    """把后端返回的一篇（图像/query）的多向量矩阵（numpy 2D array 或 list）归一成 list[list[float]]。"""
    return [[float(x) for x in row] for row in rows]


def render_pdf_pages(path: str | Path) -> list[bytes]:
    """用 pypdfium2 把 PDF 逐页渲染为 PNG bytes（1-based 页序），供构造 VisualPage。

    与 extraction/pdf_scanned_extractor 的逐页 render→PNG 同款（复用已有基础依赖 pypdfium2）。
    不可读 / 不存在 / 渲染失败 -> 返回 []（不抛，调用方据此不建视觉索引）；零页 PDF -> []。
    """
    path = Path(path)
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception:
        return []
    try:
        images: list[bytes] = []
        for idx in range(len(doc)):
            page = doc[idx]
            try:
                bitmap = page.render(scale=RENDER_DPI / 72)
                try:
                    pil_image = bitmap.to_pil()
                finally:
                    bitmap.close()
            finally:
                page.close()
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            images.append(buf.getvalue())
        return images
    except Exception:
        return []
    finally:
        doc.close()


def _to_visual_snippet(page: VisualPage, score: float) -> dict[str, Any]:
    """VisualPage + 视觉 MaxSim 分 -> snippet dict（provenance + is_visual，无可引用正文）。

    反编造：text 恒为空、is_visual=True——视觉命中是检索线索（页引用），不是 citable fact 的编造源；
    数字仍归结构化通路。doc_id / source_locator / page_no 携带完整 provenance。
    """
    return {
        "text": "",
        "doc_id": page.doc_id,
        "title": page.title,
        "source_locator": page.locator,
        "page_no": page.page_no,
        "sensitivity": page.sensitivity,
        "is_visual": True,
        "scores": {"visual_maxsim": score},
    }


class ColPaliVisualRetriever:
    """ColPali 视觉文档检索编排：对页图像候选做视觉晚交互 MaxSim 打分并按分降序给命中。

    retrieve(query, top_k) 把 query 编成 token 多向量、把每页图像编成 patch 多向量，按 MaxSim
    （复用 colbert.maxsim）打分，再按分降序返回带 provenance 的视觉命中 dict（平分保持索引序——确定性）。

    **隔离（isolation at the door）**：构造即在门口剔除 sensitivity==RESTRICTED 的页——RESTRICTED
    页从不进 self.pages（视觉索引）、从不喂给 embedder、从不出现在输出。全 RESTRICTED -> 空索引 ->
    retrieve 返回 [] 且 embedder 完全不被调用。
    """

    def __init__(
        self,
        embedder: VisualEmbedder,
        pages: Sequence[VisualPage] | None = None,
    ):
        self.embedder = embedder
        # isolation at the door：RESTRICTED 页从不进视觉索引（大小写不敏感，同两出口口径）。
        self.pages: list[VisualPage] = [
            p
            for p in (pages or [])
            if str(p.sensitivity).upper() != RESTRICTED_SENSITIVITY
        ]

    def retrieve(self, query: str, *, top_k: int = 10) -> list[dict[str, Any]]:
        """query 对每页图像按视觉 MaxSim 打分 -> 按分降序的视觉命中（前 top_k）。

        空索引（无非 RESTRICTED 页）直接返回 []，不触发任何编码/加载。
        """
        if not self.pages:
            return []
        query_matrix = self.embedder.embed_query(query)
        page_matrices = self.embedder.embed_images([p.image for p in self.pages])
        if len(page_matrices) != len(self.pages):
            raise RuntimeError(
                f"视觉 embed_images 返回矩阵条数 {len(page_matrices)} 与页数 {len(self.pages)} 不一致"
            )
        scores = [maxsim(query_matrix, pm) for pm in page_matrices]
        # 稳定降序：sorted(reverse=True) 对平分保持原索引序——确定性。
        order = sorted(range(len(self.pages)), key=lambda i: scores[i], reverse=True)
        return [_to_visual_snippet(self.pages[i], scores[i]) for i in order[:top_k]]


def _bytes_to_pil(image_bytes: bytes) -> Any:
    """PNG bytes -> PIL.Image（惰性 import Pillow：由 fastembed 视觉模型带入，归 [colpali]）。"""
    from PIL import Image

    return Image.open(io.BytesIO(image_bytes))


class ColPaliVisualEmbedder:
    """真本地 ColPali 视觉晚交互后端（实现 VisualEmbedder 协议，fastembed 运行时延迟 import）。

    embed_query(query) 走 fastembed 的 embed_text（文本→query token 多向量）；embed_images(images)
    把每张 PNG bytes 转 PIL 再走 embed_image（页图像→patch 多向量）。视觉 MaxSim 打分由
    ColPaliVisualRetriever 用复用的 colbert.maxsim 完成。

    设计约束（延迟 import 范式同 W1 OnnxEmbeddingBackend / W11 ColbertReranker）：
    - __init__ 只校验参数、记下模型名，**不** import fastembed、不加载模型——构造极轻，没装 [colpali]
      的机器上也能构造对象（工厂命名 / 协议单测）；
    - 模型在首次编码时延迟下载并缓存（fastembed 调 HF），之后复用缓存；
    - 空图像列表直接返回 []，不触发任何 import / 下载；
    - 返回矩阵条数与图像数一致性校验，异常即抛，绝不静默给坏向量。

    **重型 / GPU / 首拉权重 / 模型许可**：见模块 docstring 的诚实声明；真加载/编码打 @pytest.mark.gpu。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_COLPALI_MODEL,
        *,
        cache_dir: str | None = None,
        batch_size: int = DEFAULT_COLPALI_BATCH_SIZE,
        threads: int | None = None,
    ):
        if batch_size < 1:
            raise ValueError(f"batch_size 必须 >= 1，得到 {batch_size}")
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.batch_size = batch_size
        self.threads = threads
        self._model: Any = None  # 延迟加载缓存

    def _load_model(self) -> Any:
        """首次调用时延迟 import fastembed 并构造 LateInteractionMultimodalEmbedding（之后复用缓存）。"""
        if self._model is None:
            # 重依赖延迟 import：缺失时由 corespine.lazy_extra_import 翻成
            # `pip install ragspine[colpali]` 友好提示（离线/不做视觉检索场景无需安装）。
            fastembed = lazy_extra_import("fastembed", pkg="ragspine", extra="colpali")
            kwargs: dict[str, Any] = {}
            if self.cache_dir is not None:
                kwargs["cache_dir"] = self.cache_dir
            if self.threads is not None:
                kwargs["threads"] = self.threads
            self._model = fastembed.LateInteractionMultimodalEmbedding(
                model_name=self.model_name, **kwargs
            )
        return self._model

    def embed_query(self, query: str) -> list[list[float]]:
        """query 文本 -> query token 级多向量矩阵（fastembed embed_text）。"""
        model = self._load_model()
        return _to_matrix(next(iter(model.embed_text(query))))

    def embed_images(self, images: Sequence[bytes]) -> list[list[list[float]]]:
        """一批页图像（PNG bytes）-> 每张一个 patch 级多向量矩阵（顺序对齐）。空输入返回 []，不触发加载。"""
        imgs = list(images)
        if not imgs:
            return []
        model = self._load_model()
        pil_images = [_bytes_to_pil(b) for b in imgs]
        matrices = [
            _to_matrix(m) for m in model.embed_image(pil_images, batch_size=self.batch_size)
        ]
        if len(matrices) != len(imgs):
            raise RuntimeError(
                f"视觉 embed_image 返回矩阵条数 {len(matrices)} 与图像数 {len(imgs)} 不一致"
                f"（model={self.model_name}）"
            )
        return matrices


# 视觉 embedder 缝注册表（corespine.Registry 泛化 make_*：名字->工厂 + spec 归一）。colpali / colqwen2 /
# visual 三个名字都指向同一 ColPaliVisualEmbedder（模型经 model_name / RAGSPINE_COLPALI_MODEL 配；
# colqwen2 是命名别名，其更宽松底座的模型选型是 config/follow-up）。构造惰性（不 import fastembed），
# 故本表在此登记零 SDK import。
VISUAL_EMBEDDERS: Registry[VisualEmbedder] = Registry("visual_embedder")
VISUAL_EMBEDDERS.register("colpali", ColPaliVisualEmbedder)
VISUAL_EMBEDDERS.register("colqwen2", ColPaliVisualEmbedder)
VISUAL_EMBEDDERS.register("visual", ColPaliVisualEmbedder)

# colpali 系别名集合（归一后；缺省读 RAGSPINE_COLPALI_MODEL 仅对这些 spec 生效）。
_COLPALI_SPECS = frozenset({"colpali", "colqwen2", "visual"})


def make_visual_embedder(spec: str | None = None, **kwargs: Any) -> VisualEmbedder | None:
    """视觉 embedder 选型工厂：默认 None＝不启用视觉检索（opt-in，默认 loop 字节不变）。

    薄封装 corespine.Registry（VISUAL_EMBEDDERS）：归一 spec 后交由 Registry 分派；保留 None＝不启用
    默认、env RAGSPINE_VISUAL_EMBEDDER 缺省、colpali 系的 model_name env 注入与 kwargs 透传。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_VISUAL_EMBEDDER）：
        - None / 'none'                    -> None（不启用视觉检索；视觉检索是 opt-in 新增，默认 loop
                                              字节不变）
        - 'colpali' / 'colqwen2' / 'visual' -> ColPaliVisualEmbedder（真视觉晚交互后端，延迟 import，
                                              缺 fastembed 在首次编码时抛友好错；model_name 可经 kwargs
                                              或 RAGSPINE_COLPALI_MODEL 覆盖）
        - 其他                             -> ValueError（Registry 列清当前可用名）

    返回 VisualEmbedder 实例或 None（可注入 ColPaliVisualRetriever 的 embedder 参数）。
    """
    if spec is None:
        spec = os.environ.get(VISUAL_EMBEDDER_ENV)
    # 与 Registry._normalize 同口径地归一，用于 none 短路与 colpali 系别名判定。
    normalized = (spec or "none").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "none":
        return None
    if normalized in _COLPALI_SPECS and "model_name" not in kwargs:
        env_model = os.environ.get(COLPALI_MODEL_ENV)
        if env_model:
            kwargs["model_name"] = env_model
    return VISUAL_EMBEDDERS.make(normalized, **kwargs)
