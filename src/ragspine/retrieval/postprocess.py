"""W8 后检索 postprocessor 链：MMR 去冗余 + lost-in-the-middle 重排 + 上下文压缩。

现状（docs/prd-quality-depth.md W8）：W2 cross-encoder 精排之后，reranked top-k 直接进 prompt 组装，
没有 node-postprocessor 阶段。本模块补上一条【确定性、可组合、opt-in、默认字节不变】的后处理链，挂在
精排出口之后、prompt 组装之前（对标 LlamaIndex LongContextReorder/MMRPostprocessor/
SentenceEmbeddingOptimizer · Haystack LostInTheMiddleRanker/DiversityRanker · LangChain
ContextualCompressionRetriever）：

- MMRPostprocessor —— Maximal Marginal Relevance（Carbonell & Goldstein 1998）多样性去冗余：对已排序
  候选贪心选 `λ·rel − (1−λ)·max_sim(已选)`，把近重复块压到多样块之后（top_n 截断即真丢弃）。确定性、
  零模型：相关性用【输入位次】（继承上游 rerank/RRF 序），相似度用【纯词面重叠 Jaccard】（block 向量
  检索期不可得——embedding 相似度是 follow-up，见 PRD W8）。平分按原序稳定。
- LostInTheMiddlePostprocessor —— 对抗 LLM long-context「中间遗忘」（Liu et al. 2023）：把最相关的
  放上下文两端、次要放中间。纯确定性重排（规范 LITM：偶数名次入头、奇数名次入尾再翻转）。
- CompressionPostprocessor —— 上下文/prompt 压缩去噪省 token。确定性抽取式默认【复用 W5
  LexicalOverlapJudge】按 query 相关性抽句；LLMLingua-2/LLM 压缩作 opt-in `compressor` 缝（behind [llm]，
  见 PRD W8 follow-up）。**provenance 分层**（同 W4a contextual 的 index_text vs chunk.text 思路）：压缩
  产出写进独立的 `prompt_text` 键（agent._snippet_text 优先读之送 prompt），原文 `text` 与全部引用字段
  （source_locator/doc_id/chunk_id/title/scores/sensitivity）逐字不动——只影响送进 prompt 的文本，绝不
  破坏可追溯性。

make_postprocessor / RAGSPINE_POSTPROCESSOR：postprocessor 选型工厂（范式完全照
retrieval.rerank.cross_encoder.make_reranker）；逗号分隔的 spec（如 "mmr,lost_in_middle"）组合成
ChainPostprocessor（按序应用）。默认 None/'none' → 不挂链 → 默认 loop 字节不变（MMR/lost-in-middle 虽
确定性、理论可进默认链，但为保 byte-identity 一律 opt-in）。

**隔离不变量继承（同 W6b CorrectiveRetriever / W2 cross-encoder）**：postprocessor 只对 link 出口
（NarrativeIndexRetriever）已剔除 sensitivity==RESTRICTED 的【输出子集】做重排/去冗余/压缩，绝不自行造
片段、绝不直接读块库——故 RESTRICTED 永不进入 postprocessor、更永不出域（conformance +
reverse-proof 见 tests/retrieval/postprocess/test_postprocess_isolation.py）。

**确定性**：同输入同输出（平分按原序稳定；零模型零网络的抽取式压缩逐字可复现）。
"""

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from corespine import Registry

from ragspine.eval.groundedness import LexicalOverlapJudge

# postprocessor 选型读取的环境变量名（缺省 spec 时生效）。
POSTPROCESSOR_ENV = "RAGSPINE_POSTPROCESSOR"

# 压缩产出送进 prompt 的文本层键：agent._snippet_text 优先读之（缺省回落 text/content，默认字节不变）。
# 定于单处，agent 侧以同名字面量消费（agent 不反向 import retrieval——见 agent/CLAUDE.md 契约）。
PROMPT_TEXT_KEY = "prompt_text"

# MMR 相关性/多样性权衡系数默认值（经典 Carbonell-Goldstein 取 0.5：相关性与多样性等权）。
DEFAULT_MMR_LAMBDA = 0.5
# 抽取式压缩的句相关性阈值默认值（句内容词落在 query 词集的占比 ≥ 阈值即保留；去噪代理，非真语义）。
DEFAULT_COMPRESSION_THRESHOLD = 0.3

# 词元：ASCII 字母数字串 + 单个 CJK 汉字（与 eval/groundedness、corrective 同口径；本地定义避免跨域私有耦合）。
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")
# 句切分：中文句末标点 + 分号 + 换行 + 英文叹/问号（不含 '.'，避免切碎 8.18/U.S.）；保留句末标点随句。
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;\n]*")

Snippet = dict[str, object]

__all__ = [
    "PROMPT_TEXT_KEY",
    "POSTPROCESSOR_ENV",
    "NodePostprocessor",
    "MMRPostprocessor",
    "LostInTheMiddlePostprocessor",
    "CompressionPostprocessor",
    "ChainPostprocessor",
    "make_postprocessor",
]


@runtime_checkable
class NodePostprocessor(Protocol):
    """后检索 postprocessor 协议（依赖注入点）。

    postprocess(query, results) 返回对 results 做重排/去冗余/压缩后的片段列表（输入的子集/重排/加注，
    绝不新增或篡改引用字段）。真实现见本模块三者；测试可用确定性 fake。
    """

    def postprocess(self, query: str, results: list[Snippet]) -> list[Snippet]: ...


def _orig_text(snippet: Snippet) -> str:
    """片段原文访问器（镜像 agent._snippet_text 的回落链，但只取原文 text/content——不读 prompt_text）。"""
    return str(snippet.get("text") or snippet.get("content") or "")


def _token_set(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _jaccard(a: set[str], b: set[str]) -> float:
    """对称词面相似度：|A∩B|/|A∪B|；任一为空一律 0.0。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


@dataclass
class MMRPostprocessor:
    """MMR 多样性去冗余（确定性、零模型）。

    对已排序候选贪心选 `λ·rel − (1−λ)·max_sim(已选)`：
    - rel（相关性）取【输入位次】——首位最高、末位最低（继承上游 rerank/RRF 序，尺度无关、不篡改次序信号）；
    - max_sim（冗余度）取候选与已选集的最大【词面 Jaccard】相似度（block 向量检索期不可得，embedding 相似度
      是 follow-up）；
    - 平分按原（输入）序稳定（strict `>` + 升序遍历 → 最小下标胜出），确定性。

    top_n：None＝仅重排去冗余（保留全部）；给正整数＝截断（近重复块被压到尾部后即真正丢弃、省上下文窗口）。
    """

    lambda_: float = DEFAULT_MMR_LAMBDA
    top_n: int | None = None

    def postprocess(self, query: str, results: list[Snippet]) -> list[Snippet]:
        items = list(results)
        n = len(items)
        if n <= 1:
            return items
        rel = [(n - i) / n for i in range(n)]  # 输入位次相关性：首位 1.0 递减。
        toks = [_token_set(_orig_text(s)) for s in items]

        selected: list[int] = []
        remaining = list(range(n))
        while remaining:
            best_i: int | None = None
            best_score = 0.0
            for i in remaining:
                max_sim = max((_jaccard(toks[i], toks[j]) for j in selected), default=0.0)
                score = self.lambda_ * rel[i] - (1.0 - self.lambda_) * max_sim
                if best_i is None or score > best_score:
                    best_i, best_score = i, score
            assert best_i is not None
            selected.append(best_i)
            remaining.remove(best_i)

        out = [items[i] for i in selected]
        return out[: self.top_n] if self.top_n is not None else out


@dataclass
class LostInTheMiddlePostprocessor:
    """lost-in-the-middle 重排序（确定性、零模型）。

    输入按相关性降序（上游 rerank/RRF 序）；规范 LITM 布局：偶数名次进头、奇数名次进尾再翻转，令最相关落
    上下文两端、最不相关居中——对抗 LLM long-context 中间遗忘（Liu et al. 2023）。纯重排，不增删片段。
    """

    def postprocess(self, query: str, results: list[Snippet]) -> list[Snippet]:
        head: list[Snippet] = []
        tail: list[Snippet] = []
        for i, item in enumerate(results):
            (head if i % 2 == 0 else tail).append(item)
        return head + tail[::-1]


# 压缩缝：opt-in 的 (query, text) -> 压缩文本 适配器（LLMLingua-2 / LLM 抽取式压缩接入此缝，behind [llm]）。
Compressor = Callable[[str, str], str]


@dataclass
class CompressionPostprocessor:
    """上下文/prompt 压缩（确定性抽取式默认 + opt-in LLM 缝），provenance 绝不破坏。

    确定性默认【复用 W5 LexicalOverlapJudge】：把每个片段原文切句，保留「句内容词落在 query 词集占比 ≥
    threshold」的句子（逐字抽取，非改写），全不达标则退保最相关一句（绝不产出空、不丢内容）。

    provenance 分层（同 W4a index_text vs chunk.text）：压缩文本写进独立的 `prompt_text` 键，原文 `text`
    与全部引用字段逐字不动——只影响送进 prompt 的文本、绝不改 RetrievalResult 的引用字段。返回新 dict，不
    原地篡改调用方片段。

    compressor：opt-in 的 (query, text)->压缩文本 缝；缺省 None＝确定性抽取（离线零模型）。LLMLingua-2 /
    LLM 压缩作适配器经此缝接入——follow-up（见模块 docstring / PRD W8）。
    """

    threshold: float = DEFAULT_COMPRESSION_THRESHOLD
    compressor: Compressor | None = None

    def postprocess(self, query: str, results: list[Snippet]) -> list[Snippet]:
        judge = LexicalOverlapJudge(threshold=self.threshold)
        out: list[Snippet] = []
        for s in results:
            new = dict(s)
            new[PROMPT_TEXT_KEY] = self._compress(query, _orig_text(s), judge)
            out.append(new)
        return out

    def _compress(self, query: str, text: str, judge: LexicalOverlapJudge) -> str:
        if self.compressor is not None:
            return self.compressor(query, text)
        sentences = _SENTENCE_RE.findall(text)
        if not sentences:
            return text
        kept = [sent for sent in sentences if judge.entails(sent, query)]
        if not kept:
            # 全不达标：退保最相关一句（coverage 最高；平分取原序最前）——绝不产出空 prompt_text。
            best = max(
                range(len(sentences)),
                key=lambda i: (judge.coverage(sentences[i], query), -i),
            )
            kept = [sentences[best]]
        return "".join(kept)


@dataclass
class ChainPostprocessor:
    """把多个 postprocessor 组合成一条链：按序把上一处理器输出喂给下一处理器（实现 NodePostprocessor）。"""

    processors: Sequence[NodePostprocessor] = field(default_factory=list)

    def postprocess(self, query: str, results: list[Snippet]) -> list[Snippet]:
        out = list(results)
        for p in self.processors:
            out = list(p.postprocess(query, out))
        return out


# postprocessor 缝注册表（corespine.Registry 泛化 make_*：名字->工厂 + spec 归一，同 cross_encoder.RERANKERS）。
POSTPROCESSORS: Registry[NodePostprocessor] = Registry("postprocessor")
POSTPROCESSORS.register("mmr", MMRPostprocessor)
POSTPROCESSORS.register("lost_in_middle", LostInTheMiddlePostprocessor)
POSTPROCESSORS.register("litm", LostInTheMiddlePostprocessor)
POSTPROCESSORS.register("long_context_reorder", LostInTheMiddlePostprocessor)
POSTPROCESSORS.register("compress", CompressionPostprocessor)
POSTPROCESSORS.register("compression", CompressionPostprocessor)


def make_postprocessor(spec: str | None = None, **kwargs: object) -> NodePostprocessor | None:
    """postprocessor 选型工厂：默认 None＝不挂链（字节不变），链是 opt-in（范式照 make_reranker）。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_POSTPROCESSOR）：
        - None / 'none' / ''                 -> None（不挂链，默认 loop 字节不变）
        - 'mmr'                              -> MMRPostprocessor（多样性去冗余）
        - 'lost_in_middle' / 'litm' /
          'long_context_reorder'             -> LostInTheMiddlePostprocessor（两端布局）
        - 'compress' / 'compression'         -> CompressionPostprocessor（抽取式压缩）
        - 逗号分隔（如 'mmr,lost_in_middle'） -> ChainPostprocessor（按序组合）
        - 其他                               -> ValueError（Registry 列清可用名）

    kwargs 仅在单一 spec 时透传给该 postprocessor 构造；成链时各处理器用默认参数（直接构造可细调）。
    返回 NodePostprocessor 实例或 None（可直接喂给 build_narrative_retriever 的 postprocessor 参数）。
    """
    if spec is None:
        spec = os.environ.get(POSTPROCESSOR_ENV)
    normalized = (spec or "none").strip().lower()
    if normalized in ("", "none"):
        return None
    parts = [p.strip() for p in normalized.split(",") if p.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return POSTPROCESSORS.make(parts[0], **kwargs)
    return ChainPostprocessor([POSTPROCESSORS.make(p) for p in parts])
