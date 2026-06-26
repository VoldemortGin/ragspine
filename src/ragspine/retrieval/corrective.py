"""W6b 纠错检索（Corrective Retrieval / CRAG）：确定性、opt-in、默认关的 grade→act 检索环。

现状（docs/prd-quality-depth.md W6b）：narrative_link.NarrativeIndexRetriever 的唯一兜底是
retry_without_filters——元数据过滤把候选全滤光时去过滤重试一次。本模块把这单点兜底泛化成一个
【有界、确定性、可观测】的 grade→act 环：先对检索结果打相关性分（grade），不达标则按序执行纠错
动作（drop_filters / rewrite_query），仍不达标则诚实拒答（返回空——叙事通道据此如实说「未检索到
相关资料」；拒绝弱上下文是反编造安全的选择）。

设计约束（与 W1/W2 同范式）：
- **opt-in 默认关**：未接线时 make_corrective_retriever('none') 返回 base 本身（字节不变，默认）；
  grade→act 环是 opt-in，需显式选用（spec / RAGSPINE_CORRECTIVE env）。
- **隔离继承**：本层只对 base.retrieve(...) 的输出打分/取舍，绝不自行造片段、绝不直接读块库——base
  （NarrativeIndexRetriever）已在出口剔除 sensitivity==RESTRICTED，故本层输出恒为 base 输出的子集，
  RESTRICTED 永不出域（conformance 见 tests/retrieval/corrective/test_corrective_isolation.py）。
- **有界**：纠错重试至多 2 次（PRD 上限），max_retries 构造期 clamp 到 0..2，绝不无界。
- **确定性**：默认 grader 是纯词面重叠（LexicalOverlapGrader），零模型、零网络。LLM / cross-encoder
  grader 经 RelevanceGrader 缝 opt-in 接入——follow-up（见 PRD W6b）。

CorrectiveRetriever 满足 agent.NarrativeRetriever 协议（duck-typed retrieve 签名一致），故可透明
注入 answer_question(narrative_retriever=...)，包裹任一 base 检索器。
"""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ragspine.common.observability import emit_trace
from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY

# corrective 选型读取的环境变量名（缺省 spec 时生效）。
CORRECTIVE_ENV = "RAGSPINE_CORRECTIVE"

# 词元：ASCII 字母数字串 + 单个 CJK 汉字（与 eval/groundedness 同口径；本地定义，避免跨域私有耦合）。
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")

# RESTRICTED 隔离不出域的常量，从 rerank 出口再导出（供隔离 conformance 测试单点引用）。
__all__ = [
    "RESTRICTED_SENSITIVITY",
    "NarrativeRetriever",
    "RelevanceGrader",
    "LexicalOverlapGrader",
    "GradeAction",
    "CorrectiveRetriever",
    "make_corrective_retriever",
    "CORRECTIVE_ENV",
]


@runtime_checkable
class NarrativeRetriever(Protocol):
    """叙事检索协议（duck-typed，结构等同 agent.NarrativeRetriever）：本层既包裹它、也实现它。

    本地声明而非 import 编排层的同名协议——只为结构一致，避免 retrieval 反向耦合 agent。
    """

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]: ...


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _snippet_text(snippet: dict[str, object]) -> str:
    """片段文本访问器（镜像 agent._snippet_text）：text 优先、content 兜底、缺失为空串。"""
    return str(snippet.get("text") or snippet.get("content") or "")


@runtime_checkable
class RelevanceGrader(Protocol):
    """相关性打分缝：给定 query 与检索片段，返回 0..1 的相关性分。

    默认实现是确定性词面重叠（LexicalOverlapGrader）；LLM / cross-encoder grader 作 opt-in
    适配器接入此缝（follow-up，见模块 docstring）。"""

    def grade(self, query: str, snippets: list[dict[str, object]]) -> float: ...


@dataclass
class LexicalOverlapGrader:
    """确定性词面重叠相关性 grader（W6b 离线默认）。

    grade = query 内容词中「出现在所有片段文本并集里」的占比（按 query 词元计数，含重复，与
    eval/groundedness 同口径）；query 无内容词时返回 1.0，片段为空时返回 0.0。零模型、零网络、确定性。
    阈值不在此处——本 grader 只给原始分，达标判定（min_grade）归 CorrectiveRetriever。
    """

    def grade(self, query: str, snippets: list[dict[str, object]]) -> float:
        query_tokens = _tokens(query)
        if not query_tokens:
            return 1.0
        if not snippets:
            return 0.0
        corpus: set[str] = set()
        for s in snippets:
            corpus.update(_tokens(_snippet_text(s)))
        hit = sum(1 for t in query_tokens if t in corpus)
        return hit / len(query_tokens)


@dataclass
class GradeAction:
    """grade→act 环里一步的 trace 记录（仅非敏感元数据：动作名 / 分数 / 片段数，绝不含片段文本）。"""

    action: str
    grade: float
    snippet_count: int


class CorrectiveRetriever:
    """有界、确定性、可观测的 grade→act 纠错检索环（实现 NarrativeRetriever 协议）。

    包裹任一 base NarrativeRetriever：先 retrieve+grade，不达标则按序执行纠错动作
    （drop_filters -> rewrite_query），至多 max_retries 次（clamp 到 0..2）；全不达标则诚实拒答
    返回 []。本层绝不造片段（隔离继承，见模块 docstring）。

    确定性：base 确定 + grader 确定 => 可复现。last_actions 每次 retrieve 重置，承载本次环的逐步
    trace（grade→act 审计）。
    """

    def __init__(
        self,
        base: NarrativeRetriever,
        *,
        grader: RelevanceGrader | None = None,
        min_grade: float = 0.5,
        max_retries: int = 2,
        query_rewriter: Callable[[str], str] | None = None,
        emit: bool = True,
    ):
        self.base = base
        self.grader = grader or LexicalOverlapGrader()
        self.min_grade = min_grade
        # 有界：纠错重试至多 2 次（PRD 上限），绝不无界。
        self.max_retries = max(0, min(2, max_retries))
        self.query_rewriter = query_rewriter
        self.emit = emit
        self.last_actions: list[GradeAction] = []

    def retrieve(
        self,
        query: str,
        *,
        filters: dict[str, str] | None = None,
        top_k: int = 50,
    ) -> list[dict[str, object]]:
        """grade→act 环入口：检索打分 -> 不达标按序纠错 -> 仍不达标诚实拒答返回 []。"""
        self.last_actions = []

        # 尝试 0：原样检索 + 打分。
        snippets = self.base.retrieve(query, filters=filters, top_k=top_k)
        grade = self.grader.grade(query, snippets)
        self.last_actions.append(GradeAction("retrieve", grade, len(snippets)))
        best_grade = grade
        if grade >= self.min_grade:
            self._emit()
            return snippets

        # 纠错动作（按序构造，整体 cap 到 max_retries 次）。每个动作针对原始 query 重新打分——
        # 相关性始终对用户真实信息需求评判，改写只是为换取更优候选的手段。
        actions: list[tuple[str, str]] = []
        if filters:
            actions.append(("drop_filters", query))
        if self.query_rewriter is not None:
            actions.append(("rewrite_query", self.query_rewriter(query)))

        for action, q in actions[: self.max_retries]:
            snippets = self.base.retrieve(q, filters=None, top_k=top_k)
            grade = self.grader.grade(query, snippets)
            self.last_actions.append(GradeAction(action, grade, len(snippets)))
            best_grade = max(best_grade, grade)
            if grade >= self.min_grade:
                self._emit()
                return snippets

        # 全不达标 -> 诚实拒答（拒绝弱上下文，反编造安全）：返回空，叙事通道据此说「未检索到」。
        self.last_actions.append(GradeAction("refuse", best_grade, 0))
        self._emit()
        return []

    def _emit(self) -> None:
        """发一条 grade→act trace（仅非敏感元数据：动作序列 + 分数，绝不含片段文本/值）。"""
        if not self.emit:
            return
        emit_trace(
            corrective_actions=[a.action for a in self.last_actions],
            corrective_grades=[round(a.grade, 6) for a in self.last_actions],
        )


def make_corrective_retriever(
    base: NarrativeRetriever,
    spec: str | None = None,
    *,
    grader: RelevanceGrader | None = None,
    **kwargs: object,
) -> NarrativeRetriever:
    """纠错检索选型工厂：默认 'none' 返回 base 本身（字节不变），grade→act 环 opt-in。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_CORRECTIVE）：
        - None / 'none'                  -> base 本身（opt-out，字节不变，默认；未接线即无影响）
        - 'crag' / 'corrective' / 'on'   -> CorrectiveRetriever(base, ...)（有界确定性 grade→act 环）
        - 其他                           -> ValueError（列清可用 spec）

    grader：可选相关性打分缝（RelevanceGrader）；默认 None=确定性词面重叠 LexicalOverlapGrader。
    cross-encoder / LLM grader 作 opt-in 适配器经此缝接入——follow-up（见 PRD W6b）。
    其余 kwargs（min_grade / max_retries / query_rewriter / emit）透传给 CorrectiveRetriever。
    """
    if spec is None:
        spec = os.environ.get(CORRECTIVE_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_")
    if normalized == "none":
        return base
    if normalized in {"crag", "corrective", "on"}:
        return CorrectiveRetriever(base, grader=grader, **kwargs)  # type: ignore[arg-type]
    raise ValueError(
        f"未知 corrective spec {spec!r}；可用：none / crag / corrective / on"
    )
