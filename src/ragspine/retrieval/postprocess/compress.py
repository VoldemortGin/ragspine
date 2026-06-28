"""W8 上下文/prompt 压缩：确定性抽取式默认（句级相关性过滤）+ opt-in 重路径缝。

冗长片段稀释信号、烧 token。默认是【确定性抽取式】句级过滤：把片段切句，只保留与 query 内容词
重叠达标的句子（复用与 W5 LexicalOverlapJudge 同口径的词面机制，零模型）。重路径（abstractive /
learned）经 Compressor 缝 opt-in：[llm]（LLM 抽取）或 LLMLingua-2 token 级压缩——follow-up（见 PRD W8）。

对标 LlamaIndex SentenceEmbeddingOptimizer、LangChain ContextualCompressionRetriever
（LLMChainExtractor / LLMLinguaCompressor）。

设计约束（守反编造 + 召回）：
- **只 trim 不删片段**：某片段无句达标时【保留原文】（不致盲删 reranker 认为相关的片段——句级词面
  重叠为 0 不代表语义不相关）。压缩只缩短 text，绝不新增片段。
- **血缘不动**：只改 snippet 的 text，doc_id / source_locator / chunk_id / scores / sensitivity 原样
  保留——citation 回指与隔离继承不受影响。
- **确定性**：句切 + 词面重叠均无模型无网络，逐位可复现。

只返回输入片段的【子集（trim text）】（绝不造片段），故 RESTRICTED 隔离从 base 出口继承（见 chain.py）。
"""

import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ragspine.retrieval.postprocess.chain import snippet_text, tokens

# 句切：按 CJK/半角句末标点与分号、换行切；【刻意不切】半角句点，避免拆散小数（如 FY2024 的 3.5%）。
_SENT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")


def _split_sentences(text: str) -> list[str]:
    """把片段文本切成句子（去空句、保原序）。"""
    return [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]


@runtime_checkable
class Compressor(Protocol):
    """压缩缝：给定 query 与单片段文本，返回压缩后的文本（更短或等长，绝不变长）。

    默认实现 ExtractiveCompressor（确定性句级过滤）；LLMLingua-2 / LLM 抽取作 opt-in 适配器接入此缝
    （follow-up，见模块 docstring）。
    """

    def compress(self, query: str, text: str) -> str: ...


@dataclass
class ExtractiveCompressor:
    """确定性抽取式压缩（实现 Compressor 协议）：句级相关性过滤，零模型零网络。

    对片段切句，保留与 query 内容词重叠 >= min_overlap 个的句子（按原序拼回）。query 无内容词时
    原样返回（无从过滤）；无句达标时返回原文（只 trim 不盲删，见模块 docstring）。
    """

    min_overlap: int = 1

    def __post_init__(self) -> None:
        if self.min_overlap < 1:
            raise ValueError(f"min_overlap 须 >= 1，得到 {self.min_overlap}")

    def compress(self, query: str, text: str) -> str:
        query_tokens = set(tokens(query))
        if not query_tokens:
            return text
        sentences = _split_sentences(text)
        if not sentences:
            return text
        kept = [
            s
            for s in sentences
            if len(set(tokens(s)) & query_tokens) >= self.min_overlap
        ]
        if not kept:
            return text  # 无句达标 -> 保留原文（不盲删）
        return "。".join(kept)


@dataclass
class CompressionPostprocessor:
    """上下文压缩 postprocessor（实现 NodePostprocessor 协议）：对每片段跑 Compressor，只改 text。

    保留所有片段与其它字段（血缘/分数/敏感度原样），仅把 text 换成压缩结果——隔离与 citation 不受影响。
    默认 compressor = ExtractiveCompressor（确定性）；LLMLingua / LLM 压缩经此缝 opt-in（follow-up）。
    """

    compressor: Compressor | None = None

    def __post_init__(self) -> None:
        if self.compressor is None:
            self.compressor = ExtractiveCompressor()

    def postprocess(
        self, query: str, snippets: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        assert self.compressor is not None  # __post_init__ 保证
        out: list[dict[str, Any]] = []
        for s in snippets:
            compressed = self.compressor.compress(query, snippet_text(s))
            new = dict(s)  # 浅拷贝，只改 text，其它字段（含血缘/敏感度）原样保留
            new["text"] = compressed
            out.append(new)
        return out
