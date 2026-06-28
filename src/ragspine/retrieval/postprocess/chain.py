"""W8 后检索 postprocessor 链：NodePostprocessor 缝 + 链编排 + 检索器包裹 + 选型工厂。

现状（docs/prd-quality-depth.md W8）：W2 cross-encoder 精排后，reranked top-k 直接进 prompt 组装——
没有 node-postprocessor 阶段。主流栈（LlamaIndex / Haystack / LangChain）都有的三件事缺失：
(1) 多样性去重——近重复块挤占上下文窗口；(2) lost-in-the-middle 重排——LLM 对长上下文【中部】注意力
最差（Liu et al. 2023），而 reranked 序恰把最好的命中放在中间；(3) 上下文/prompt 压缩——冗长片段
稀释信号、烧 token。

本模块给精排出口与 prompt 组装之间补上这一确定性阶段，落在一条薄 NodePostprocessor 缝上：
- NodePostprocessor 协议：postprocess(query, snippets) -> snippets（只许返回输入的【子集/重排】）；
- PostprocessorChain：按序组合多个 processor（MMR 去重 -> 压缩 -> lost-in-the-middle 重排）；
- PostprocessingRetriever：包裹任一 base NarrativeRetriever，对其输出跑链（W6b CorrectiveRetriever 习语）；
- make_postprocessor / make_postprocessing_retriever：把「用哪些 postprocessor」降为一个 spec/env，
  默认 None=不接链=字节不变（范式同 make_reranker / make_corrective_retriever）。

三个 processor（mmr.py / reorder.py / compress.py）——两个纯确定性零模型（MMR、lost-in-the-middle），
一个确定性抽取式默认 + opt-in 重路径（压缩；LLMLingua-2 / LLM 抽取留 follow-up，见 compress.py）。

**默认字节不变**：MMR + lost-in-the-middle 是 (序, 词面/向量相似) 的纯函数，逐位可复现、本【可】默认开；
但为守住已发布 loop 的字节一致，统一 opt-in（make_postprocessor 默认 None => 不接链 => 字节不变），
recommended-on 而非 on-by-default。

**隔离不变量继承（非重新实现）**：postprocessor 只对 base.retrieve(...) 的输出取【子集/重排】，绝不
自行造片段、绝不直接读块库——base（NarrativeIndexRetriever）已在出口剔除 sensitivity==RESTRICTED，
故链输出恒为 base 输出的子集，RESTRICTED 永不出域（conformance 见
tests/retrieval/postprocess/test_postprocess_isolation.py）。
"""

import os
import re
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from corespine import Registry

from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY

# postprocessor 选型读取的环境变量名（缺省 spec 时生效）。
POSTPROCESSOR_ENV = "RAGSPINE_POSTPROCESSOR"

# 词元：ASCII 字母数字串 + 单个 CJK 汉字（与 corrective / eval.groundedness 同口径；本地定义，
# 避免跨域私有耦合——同 corrective.py 的取舍）。
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")

__all__ = [
    "RESTRICTED_SENSITIVITY",
    "NodePostprocessor",
    "NarrativeRetriever",
    "PostprocessorChain",
    "PostprocessingRetriever",
    "make_postprocessor",
    "make_postprocessing_retriever",
    "POSTPROCESSOR_ENV",
    "snippet_text",
    "tokens",
    "token_set",
    "jaccard",
]


def tokens(text: str) -> list[str]:
    """内容词元（小写化的 ASCII 串 + 单 CJK 字）；非内容字符（标点/空白）丢弃。"""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def token_set(text: str) -> set[str]:
    return set(tokens(text))


def jaccard(a: set[str], b: set[str]) -> float:
    """两词元集的 Jaccard 相似度 |A∩B|/|A∪B|；两者皆空记 0.0（无可比较内容）。"""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def snippet_text(snippet: dict[str, Any]) -> str:
    """片段文本访问器（镜像 agent._snippet_text / corrective._snippet_text）：text 优先、
    content 兜底、缺失为空串。"""
    return str(snippet.get("text") or snippet.get("content") or "")


@runtime_checkable
class NodePostprocessor(Protocol):
    """node-postprocessor 缝：检索片段列表 -> 处理后的片段列表（只许子集/重排）。

    约定（隔离继承的结构前提）：实现【只能】返回输入片段的子集或重排（可修改单片段的 text 做压缩，
    但绝不新增片段、绝不读外部块库），故 RESTRICTED 不出域由 base 出口的剔除继承得来。
    """

    def postprocess(
        self, query: str, snippets: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class NarrativeRetriever(Protocol):
    """叙事检索协议（duck-typed，结构等同 agent.NarrativeRetriever / corrective.NarrativeRetriever）。

    本地声明而非 import 编排层同名协议——只为结构一致，避免 retrieval 反向耦合 agent。
    """

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]: ...


class PostprocessorChain:
    """按序组合多个 NodePostprocessor（本身也是 NodePostprocessor）。

    逐个把上一个的输出喂给下一个；空链等同恒等（返回原片段）。每个 processor 都遵守「只返回子集/重排」，
    故整条链亦然——隔离继承一路守住。确定性 = 各 processor 确定 => 链确定。
    """

    def __init__(self, processors: list[NodePostprocessor]):
        self.processors = list(processors)

    def postprocess(
        self, query: str, snippets: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        out = snippets
        for proc in self.processors:
            out = proc.postprocess(query, out)
        return out


class PostprocessingRetriever:
    """对 base NarrativeRetriever 的输出跑 postprocessor 链（实现 NarrativeRetriever 协议）。

    包裹任一 base：retrieve -> base.retrieve(...) -> postprocessor.postprocess(query, snippets)。
    本层绝不造片段（隔离继承，见模块 docstring）：只把 base 已 RESTRICTED-剔除的输出做子集/重排。

    确定性：base 确定 + postprocessor 确定 => 可复现。
    """

    def __init__(self, base: NarrativeRetriever, postprocessor: NodePostprocessor):
        self.base = base
        self.postprocessor = postprocessor

    def retrieve(
        self,
        query: str,
        *,
        filters: dict[str, str] | None = None,
        top_k: int = 50,
    ) -> list[dict[str, object]]:
        snippets = self.base.retrieve(query, filters=filters, top_k=top_k)
        return self.postprocessor.postprocess(query, snippets)


# ---------------------------------------------------------------------------
# 注册表：内置 processor 名字 -> 惰性 loader（返回 NodePostprocessor【工厂】）。范式同 chunker.py：
# 惰性 import 各 processor，使 chain.py 与 processor 模块无环依赖（processor import 本模块取
# Protocol/helpers；本模块仅在 loader 内反向 import processor）。别名共指同一 loader。
# ---------------------------------------------------------------------------
def _load_mmr() -> Callable[..., NodePostprocessor]:
    from ragspine.retrieval.postprocess.mmr import MMRPostprocessor

    return MMRPostprocessor


def _load_reorder() -> Callable[..., NodePostprocessor]:
    from ragspine.retrieval.postprocess.reorder import LostInTheMiddleReorder

    return LostInTheMiddleReorder


def _load_compress() -> Callable[..., NodePostprocessor]:
    from ragspine.retrieval.postprocess.compress import CompressionPostprocessor

    return CompressionPostprocessor


# corespine.Registry 泛化 make_*：名字->工厂 + spec 归一（大小写/留白/连字符不敏感）。
# 内置 processor 注册的是【惰性 loader 调用结果】（真工厂），延后到 make 时才反向 import。
POSTPROCESSORS: Registry[NodePostprocessor] = Registry("postprocessor")
POSTPROCESSORS.register("mmr", lambda **kw: _load_mmr()(**kw))
POSTPROCESSORS.register("diversity", lambda **kw: _load_mmr()(**kw))
POSTPROCESSORS.register("reorder", lambda **kw: _load_reorder()(**kw))
POSTPROCESSORS.register("litm", lambda **kw: _load_reorder()(**kw))
POSTPROCESSORS.register("long_context", lambda **kw: _load_reorder()(**kw))
POSTPROCESSORS.register("lost_in_the_middle", lambda **kw: _load_reorder()(**kw))
POSTPROCESSORS.register("compress", lambda **kw: _load_compress()(**kw))
POSTPROCESSORS.register("compression", lambda **kw: _load_compress()(**kw))
POSTPROCESSORS.register("extractive", lambda **kw: _load_compress()(**kw))

# 预设链：把推荐组合降为一个名字。'recommended'/'all'/'default' = MMR 去重 -> 抽取式压缩 ->
# lost-in-the-middle 重排（全确定性零模型；重排放最后，作用于最终 prompt 版面）。
_PRESET_CHAINS: dict[str, tuple[str, ...]] = {
    "recommended": ("mmr", "compress", "reorder"),
    "all": ("mmr", "compress", "reorder"),
    "default": ("mmr", "compress", "reorder"),
}


def make_postprocessor(spec: str | None = None, **kwargs: Any) -> NodePostprocessor | None:
    """postprocessor 选型工厂：把「接哪些 postprocessor」降为一个 spec/env，默认 None=不接链（字节不变）。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_POSTPROCESSOR）：
        - None / 'none' / ''                 -> None（不接链；retriever 输出原样进 prompt——默认 loop
                                                字节不变，postprocessor 是 opt-in）
        - 'mmr' / 'diversity'                -> MMRPostprocessor（确定性多样性去重 + 重排）
        - 'reorder' / 'litm' / 'long_context'/ 'lost_in_the_middle'
                                             -> LostInTheMiddleReorder（确定性，最相关置首尾）
        - 'compress' / 'compression' / 'extractive'
                                             -> CompressionPostprocessor（确定性抽取式句级过滤）
        - 'recommended' / 'all' / 'default'  -> PostprocessorChain([mmr, compress, reorder])（推荐组合）
        - 'a,b,c'（逗号分隔）                 -> 按序组合成 PostprocessorChain（如 'mmr,reorder'）
        - 其他                               -> ValueError（Registry 列清可用名）

    单 processor 时 kwargs 透传给该 processor 工厂；多 processor（预设/逗号链）时各取默认（kwargs 忽略，
    与 make_reranker「kwargs 仅对单实现有意义」同口径）。返回 NodePostprocessor（单个或链）或 None。
    """
    if spec is None:
        spec = os.environ.get(POSTPROCESSOR_ENV)
    normalized = (spec or "none").strip().lower()
    if normalized in ("none", ""):
        return None

    if normalized in _PRESET_CHAINS:
        parts = list(_PRESET_CHAINS[normalized])
    else:
        parts = [p.strip() for p in normalized.split(",") if p.strip()]
    if not parts:
        return None

    if len(parts) == 1:
        return POSTPROCESSORS.make(parts[0], **kwargs)
    return PostprocessorChain([POSTPROCESSORS.make(p) for p in parts])


def make_postprocessing_retriever(
    base: NarrativeRetriever, spec: str | None = None, **kwargs: Any
) -> NarrativeRetriever:
    """后检索包裹工厂：默认 'none' 返回 base 本身（字节不变），postprocessor 链 opt-in。

    范式同 make_corrective_retriever：spec=None/'none' => base 原样返回（未接线即无影响）；否则
    make_postprocessor(spec) 得到链，再包成 PostprocessingRetriever。隔离继承自 base（其输出已剔除
    RESTRICTED）。kwargs 透传给 make_postprocessor（仅单 processor 时有意义）。
    """
    postprocessor = make_postprocessor(spec, **kwargs)
    if postprocessor is None:
        return base
    return PostprocessingRetriever(base, postprocessor)
