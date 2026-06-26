"""叙事侧 groundedness 度量（W5）：faithfulness + free-text answer-accuracy。

把"反编造"从叙事侧的【断言】变成可度量的【回归锁】：narrative 答案信模型散文 + 强制
citation，但既有四命门从不检验答案是否被检索片段蕴含。本模块补上两条：
    faithfulness   —— 答案的每条 claim 是否被检索 context 蕴含（RAGAS 式）。
    answer-accuracy —— 叙事自由文本答案是否覆盖期望文档的实质内容（叙事侧内容正确性）。

默认离线确定性方法 = 词面重叠蕴含启发式（LexicalOverlapJudge），零模型、零网络、确定性，
make ci 默认即跑。诚实局限：它是"claim 的内容词被 context 词集覆盖比例 ≥ 阈值"这一【词面
代理】，不是真 NLI——不理解改写/否定/数值反转（context 说"下降"、claim 说"上升"但共享其余
词时可能漏判），也可能被与 context 大量共词却语义不同的句子骗过。它擅长抓最常见的编造形态：
答案引入了 context 里根本没有的新词（新实体/新数字/新主张）。

真 ONNX 蕴含模型（小 MNLI/cross-encoder NLI，[eval] extra，过 ADR 0009 ≤Apache-2.0 许可门）
与 LLM-judge（[llm] extra，opt-in 默认关）作为 EntailmentJudge 缝的【opt-in 适配器 +
follow-up】（见 docs/prd-quality-depth.md W5）；context-precision/recall 与 answer-relevance
同列 follow-up。本模块只交付核心 faithfulness + answer-accuracy 的确定性离线默认。
"""

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# 判定阈值（单处定义）。
FAITHFULNESS_COVERAGE_THRESHOLD = 0.6
ANSWER_ACCURACY_RECALL_THRESHOLD = 0.6

# 词元：ASCII 字母数字串 + 单个 CJK 汉字（混合中英文）。
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")
# 引用尾注（来源/资料来源）—— 系统强制 citation，非世界断言，计分前剥离。
_CITATION_RE = re.compile(r"（(?:资料)?来源[：:][^）]*）")
# 行内检索序号标记 [1]/[2]。
_INDEX_MARKER_RE = re.compile(r"\[\d+\]")
# 句子切分：中文句末标点 + 分号 + 换行 + 英文句末叹/问号（不含 '.'，避免切碎 8.18/U.S.）。
_SENT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")

# 系统模板/提示脚手架行前缀：由编排层 / MockProvider 合成 / 回显 prompt 时固定加上，属于
# "答案怎么组织"的元话语或回显的问句脚手架，不是对世界的断言，故不计入 faithfulness。集中在此，
# 避免与 agent 层模板字面隐式耦合到各处。整行命中即整行丢弃（见 split_claims 的按行处理）。
_FRAMING_PREFIXES = (
    "基于检索到的资料",
    "未检索到",
    "归因部分未检索到",
    "对比结果",
    "归因分析",
    "资料来源",
    "问题：",  # MockProvider 叙事合成回显的提示脚手架（问句重述，非世界断言）
    "问题:",
    "检索片段",
)


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _token_set(text: str) -> set[str]:
    return set(_tokens(text))


def _is_framing(claim: str) -> bool:
    stripped = claim.strip()
    return any(stripped.startswith(prefix) for prefix in _FRAMING_PREFIXES)


def split_claims(answer: str) -> list[str]:
    """把答案切成 claim：先按行丢弃系统框架/提示脚手架行（整行丢，避免其内部片段如回显问句的
    分句被误当 claim），再对正文行剥引用尾注与序号标记、按句末标点切，丢空句与纯标点句。"""
    claims: list[str] = []
    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped or _is_framing(stripped):
            continue
        cleaned = _CITATION_RE.sub("", stripped)
        cleaned = _INDEX_MARKER_RE.sub(" ", cleaned)
        for raw in _SENT_SPLIT_RE.split(cleaned):
            sentence = raw.strip()
            if not sentence or not _tokens(sentence):
                continue
            claims.append(sentence)
    return claims


@runtime_checkable
class EntailmentJudge(Protocol):
    """蕴含判定缝：给定 claim 与 context，判 context 是否蕴含 claim。

    默认实现是确定性词面重叠启发式（LexicalOverlapJudge）；真 ONNX NLI 模型 / LLM-judge
    作 opt-in 适配器接入此缝（follow-up，见模块 docstring）。"""

    def entails(self, claim: str, context: str) -> bool: ...


@dataclass
class LexicalOverlapJudge:
    """确定性词面重叠蕴含启发式（离线默认方法）。

    判定：claim 的内容词被 context 词集覆盖的比例 ≥ threshold 即视为蕴含。局限见模块 docstring。
    """

    threshold: float = FAITHFULNESS_COVERAGE_THRESHOLD

    def coverage(self, claim: str, context: str) -> float:
        claim_tokens = _tokens(claim)
        if not claim_tokens:
            return 1.0
        ctx = _token_set(context)
        hit = sum(1 for t in claim_tokens if t in ctx)
        return hit / len(claim_tokens)

    def entails(self, claim: str, context: str) -> bool:
        return self.coverage(claim, context) >= self.threshold


def make_entailment_judge(kind: str = "lexical", **kwargs: object) -> EntailmentJudge:
    """蕴含判定器工厂。

    'lexical'（默认，离线确定性）已实现；'nli'（ONNX 蕴含模型，[eval] extra）与
    'llm'（LLM-judge，[llm] extra）为 opt-in follow-up（见 docs/prd-quality-depth.md W5）。
    """
    if kind == "lexical":
        return LexicalOverlapJudge(**kwargs)  # type: ignore[arg-type]
    raise NotImplementedError(
        f"entailment judge {kind!r} 是 opt-in follow-up（见 PRD W5）；"
        "当前离线默认为 'lexical'。"
    )


@dataclass
class FaithfulnessResult:
    """一条答案的 faithfulness 结果。

    字段：
        claims:      参与判定的 claim（已剥引用/序号/框架句）。
        unsupported: 未被 context 蕴含的 claim 明细。
    """

    claims: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def n_claims(self) -> int:
        return len(self.claims)

    @property
    def score(self) -> float:
        """被蕴含 claim 占比（无 claim 时约定 1.0）。"""
        if not self.claims:
            return 1.0
        return 1.0 - len(self.unsupported) / len(self.claims)

    @property
    def ok(self) -> bool:
        """每条 claim 都被蕴含才算忠实（gate 判定口径）。"""
        return not self.unsupported


def faithfulness(
    answer: str,
    context_texts: list[str],
    judge: EntailmentJudge | None = None,
) -> FaithfulnessResult:
    """逐 claim 判答案是否被检索 context 蕴含。judge 缺省用确定性词面重叠启发式。"""
    judge = judge or LexicalOverlapJudge()
    context = "\n".join(context_texts)
    claims = split_claims(answer)
    unsupported = [c for c in claims if not judge.entails(c, context)]
    return FaithfulnessResult(claims=claims, unsupported=unsupported)


def answer_accuracy(answer: str, reference: str) -> float:
    """叙事自由文本答案准确性：答案覆盖了多少期望文档的内容词（recall 取向重叠代理）。

    诚实局限：词面 recall 代理，非真语义等价——不惩罚答案夹带额外内容（那由 faithfulness 管），
    只度量"是否真的把期望文档的实质讲出来了"。空 reference 视为无可评（返回 1.0）。"""
    ref = _token_set(reference)
    if not ref:
        return 1.0
    ans = _token_set(answer)
    return len(ref & ans) / len(ref)
