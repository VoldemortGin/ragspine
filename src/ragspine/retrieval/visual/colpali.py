"""W12 ColPali 视觉文档检索：把页面【作为图像】嵌入 + patch 级晚交互，无 OCR→text（opt-in，最重）。

现状（docs/prd-quality-depth.md W12）：家族 OCR→text 路线（W3a）在问题依赖视觉结构（图表、密集财务表、
图形版面）时会丢版面/图形信息。ColPali / ColQwen2（Faysse et al. 2024）把整页渲染成图像、直接在图像
patch 上做晚交互（MaxSim），不经 OCR→text，常在图表/密表的财报上明显更强——与 W3a 的离线 OCR→text
【并列】的一条路线（非替代）。

本模块给视觉检索一个【视觉多向量缝】+ 一个 page-as-image 检索器：
- VisualMultiVectorBackend 协议：query 文本 -> token 多向量；页图像 -> patch 多向量。
- ColPaliRetriever：对一组页图像按 MaxSim（复用 W11 max_sim，patch 级晚交互）打分，返回带血缘的页命中
  （doc_id + source_locator + page）。**RESTRICTED 页在出口剔除**（绝不嵌入/打分/返回——同 link 出口纪律）。
- FastEmbedColPaliBackend：fastembed LateInteractionMultimodalEmbedding 适配器（vidore/colpali-v1.2
  等），延迟 import、归 [colpali]。

**重依赖诚实标注（不可省略）**：ColPali 需 **GPU + 视觉语言模型**，且首次从 HF 下载权重（"首拉后离线"）。
**opt-in、默认关、绝不在精简/CPU 默认路径上**。CPU/离线/确定的默认 loop 仍是产品本体；视觉检索是其上的
扩展，**与 W3a 家族 OCR→text 并存**（图表密集文档视觉胜，离线/确定/CPU 场景 OCR→text 胜）。

复用 W11 多向量缝：本质是「patch 晚交互」而非「text token 晚交互」，MaxSim 打分函数直接复用
late_interaction.max_sim。把视觉命中与 OCR→text 通道 RRF 融合是 follow-up（见 PRD W12）。
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from corespine import lazy_extra_import

from ragspine.retrieval.representation.late_interaction import max_sim
from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY

# 默认 ColPali 模型（视觉语言晚交互）。ColQwen2 vs ColPali 的取舍是 follow-up。
DEFAULT_COLPALI_MODEL = "vidore/colpali-v1.2"
# 模型覆盖环境变量。
COLPALI_MODEL_ENV = "RAGSPINE_COLPALI_MODEL"


@dataclass
class PageImage:
    """一页文档图像 + 血缘元数据（视觉检索的输入单元）。

    image：页图像（路径 str / bytes / PIL.Image——由后端解读，核心不绑具体类型）。
    source_locator：citation 回指（如 'report.pdf#page3'）。sensitivity：RESTRICTED 页在出口剔除。
    """

    doc_id: str
    page: int
    image: Any
    source_locator: str = ""
    sensitivity: str = "INTERNAL"


@runtime_checkable
class VisualMultiVectorBackend(Protocol):
    """视觉多向量嵌入缝：query 文本 -> token 多向量；页图像 -> patch 多向量。

    具体实现（fastembed ColPali 等）延迟加载、需 GPU + 视觉模型，核心只 import 此 Protocol。
    """

    def embed_query(self, query: str) -> list[list[float]]: ...

    def embed_images(self, images: list[Any]) -> list[list[list[float]]]: ...


def _page_result(page: PageImage, score: float) -> dict[str, Any]:
    """页命中 -> snippet 风格 dict（带血缘 + 视觉标记；无 text——视觉检索不产文本）。"""
    return {
        "doc_id": page.doc_id,
        "page": page.page,
        "source_locator": page.source_locator or f"{page.doc_id}#page{page.page}",
        "sensitivity": page.sensitivity,
        "is_visual": True,
        "scores": {"colpali_maxsim": score},
    }


class FastEmbedColPaliBackend:
    """fastembed LateInteractionMultimodalEmbedding 适配器（实现 VisualMultiVectorBackend，[colpali]）。

    __init__ 只记模型名、不 import fastembed、不加载模型（构造极轻，没装 [colpali] / 无 GPU 也能构造）；
    模型首次 embed 时延迟下载并加载（需 GPU + 视觉模型）。
    """

    def __init__(self, model_name: str = DEFAULT_COLPALI_MODEL, *, cache_dir: str | None = None):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            mod = lazy_extra_import("fastembed", pkg="ragspine", extra="colpali")
            kwargs: dict[str, Any] = {}
            if self.cache_dir is not None:
                kwargs["cache_dir"] = self.cache_dir
            self._model = mod.LateInteractionMultimodalEmbedding(self.model_name, **kwargs)
        return self._model

    @staticmethod
    def _to_lists(arr: Any) -> list[list[float]]:
        return [[float(x) for x in row] for row in arr]

    def embed_query(self, query: str) -> list[list[float]]:
        model = self._load()
        return self._to_lists(next(iter(model.embed_text([query]))))

    def embed_images(self, images: list[Any]) -> list[list[list[float]]]:
        if not images:
            return []
        model = self._load()
        return [self._to_lists(arr) for arr in model.embed_image(images)]


class ColPaliRetriever:
    """page-as-image 视觉检索器：对一组页图像按 patch 级 MaxSim 打分，返回带血缘的页命中。

    backend 默认 None -> 延迟构造 FastEmbedColPaliBackend（首次 retrieve 时加载，需 GPU）。
    **RESTRICTED 页在构造时即排除**（绝不嵌入/打分/返回——隔离出口纪律）。页向量按需嵌入一次并缓存。
    确定性：后端确定 + max_sim 确定 => 可复现（真 ColPali 推理的确定性取决于模型/硬件，故 opt-in）。
    """

    def __init__(
        self,
        pages: list[PageImage],
        backend: VisualMultiVectorBackend | None = None,
        *,
        model_name: str = DEFAULT_COLPALI_MODEL,
        cache_dir: str | None = None,
    ):
        # RESTRICTED 页在入口即剔除：绝不进入嵌入/打分/返回（不出域，最强保证）。
        self.pages = [
            p for p in pages if str(p.sensitivity).upper() != RESTRICTED_SENSITIVITY
        ]
        self._backend = backend
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._page_vectors: list[list[list[float]]] | None = None

    def _backend_or_default(self) -> VisualMultiVectorBackend:
        if self._backend is None:
            self._backend = FastEmbedColPaliBackend(self.model_name, cache_dir=self.cache_dir)
        return self._backend

    def retrieve(self, query: str, *, top_k: int = 10) -> list[dict[str, Any]]:
        if not self.pages:
            return []
        backend = self._backend_or_default()
        if self._page_vectors is None:
            self._page_vectors = backend.embed_images([p.image for p in self.pages])
            if len(self._page_vectors) != len(self.pages):
                raise RuntimeError(
                    f"embed_images 返回 {len(self._page_vectors)} 条与页数 {len(self.pages)} 不一致"
                )
        q_vecs = backend.embed_query(query)
        scores = [max_sim(q_vecs, pv) for pv in self._page_vectors]
        order = sorted(range(len(self.pages)), key=lambda i: scores[i], reverse=True)
        return [_page_result(self.pages[i], scores[i]) for i in order[:top_k]]


def make_colpali_retriever(
    pages: list[PageImage],
    backend: VisualMultiVectorBackend | None = None,
    **kwargs: Any,
) -> ColPaliRetriever:
    """ColPali 视觉检索器工厂：缺省读 RAGSPINE_COLPALI_MODEL。

    opt-in、默认关：调用方显式构造才启用视觉检索（与 W3a OCR→text 并存）。kwargs 透传 model_name/cache_dir。
    """
    if "model_name" not in kwargs:
        import os

        env_model = os.environ.get(COLPALI_MODEL_ENV)
        if env_model:
            kwargs["model_name"] = env_model
    return ColPaliRetriever(pages, backend, **kwargs)
