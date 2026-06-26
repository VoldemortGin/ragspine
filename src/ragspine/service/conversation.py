"""W6c 多轮会话记忆（opt-in 骨架，挂服务层）：无状态单发 → 可选多轮跟进/指代消解。

现状：answer_question 单发无状态。本模块给一个【最小可用】的多轮骨架——一个有界会话记忆 +
确定性的跟进槽位回填 + 每轮重过安全门的会话封装。它把"那 PROFIT 呢""利润呢"这类省略主体/
期间的跟进问句，用上一轮解析出的 home 受控槽位补全后再走 answer_question。

硬安全约束（不可破）：
- **每轮重过安全门**：ConversationSession.ask 每轮都调 answer_question，安全门从【augmented
  问句】独立复核越权/竞品——记忆绝不绕过 RESTRICTED 两出口过滤与竞品拒答。
- **记忆只承载 home 受控槽位**：只存上一轮解析出的 home entity 代码 + period（非敏感元数据），
  绝不存答案正文/事实数值/竞品。竞品跟进问句【绝不】被回填 home 上下文（resolve_followup 命中
  external_entity 即原样放行，交给安全门拒答），被拒答的轮也【绝不】写入记忆。
- **确定性回填**：回填是规则（reverse-alias + 期间渲染），零 LLM，同输入同输出。

opt-in、默认关：本模块不被任何默认路径 import，单发主流程字节不变。LLM 指代消解（真正的代词/
省略消解）、FastAPI 端点接线是 follow-up（见 docs/prd-quality-depth.md W6c）。
"""

from collections import deque
from dataclasses import dataclass
from datetime import date

from ragspine.agent.agent import AgentResult, NarrativeRetriever, answer_question
from ragspine.agent.intent import (
    CLARIFY_OUT_OF_SCOPE_ENTITY,
    ROUTE_COMPOSITE,
    ROUTE_STRUCTURED,
    IntentParser,
    RuleIntentParser,
)
from ragspine.agent.llm_provider import LLMProvider
from ragspine.common.glossary import ENTITY_SYNONYMS
from ragspine.storage.fact_store import FactStore

# 记忆默认保留的最近轮数（有界，防无限增长）。
DEFAULT_MEMORY_TURNS = 8

# 回填仅服务这两类（结构化/复合）跟进——它们的 entity/period 是硬限定，省略时补全才有意义。
_CARRY_FORWARD_ROUTES = (ROUTE_STRUCTURED, ROUTE_COMPOSITE)


@dataclass
class ConversationTurn:
    """一轮会话的非敏感元数据：问句 + 路由 + 解析出的 home 受控槽位（缺失为 None）。

    刻意只存 entity 代码 + period 槽位——绝不存答案正文/事实数值/检索片段（按 Restricted 对待）。
    """

    question: str
    route: str
    entity: str | None
    period: tuple[str, str] | None


class ConversationMemory:
    """有界会话记忆：保留最近 N 轮，向后回扫最近一个非空 entity/period 作为跟进默认。"""

    def __init__(self, maxlen: int = DEFAULT_MEMORY_TURNS):
        self._turns: deque[ConversationTurn] = deque(maxlen=maxlen)

    def remember(self, turn: ConversationTurn) -> None:
        self._turns.append(turn)

    def last_entity(self) -> str | None:
        for turn in reversed(self._turns):
            if turn.entity is not None:
                return turn.entity
        return None

    def last_period(self) -> tuple[str, str] | None:
        for turn in reversed(self._turns):
            if turn.period is not None:
                return turn.period
        return None

    @property
    def turns(self) -> list[ConversationTurn]:
        return list(self._turns)

    def __len__(self) -> int:
        return len(self._turns)


def _entity_alias(code: str) -> str | None:
    """home entity 代码 → 一个可被规则解析器识别的别名（reverse-map，确定性取插入序首个）。"""
    return next((alias for alias, c in ENTITY_SYNONYMS.items() if c == code), None)


def _period_text(period: tuple[str, str]) -> str:
    """期间槽位 (period_type, value) → 解析器可识别的绝对期间串（FY 加前缀，HY/QUARTER 自含年份）。"""
    period_type, value = period
    return f"FY{value}" if period_type == "FY" else value


def resolve_followup(
    memory: ConversationMemory,
    question: str,
    *,
    reference_date: date | None = None,
    intent_parser: IntentParser | None = None,
) -> str:
    """确定性跟进消解：结构化/复合跟进缺 entity/period 时，回填上一轮 home 槽位，否则原样返回。

    安全：命中竞品/外部主体（intent.external_entity）即原样放行，绝不把 home 上下文带进越权问句；
    回填的别名/期间都是 home 受控值，解析器据此重解析回同一受控代码。
    """
    parser = intent_parser or RuleIntentParser()
    intent = parser.parse(question, reference_date=reference_date)
    # 越权问句：绝不回填 home 上下文（交给安全门越权拒答；记忆不得污染拒答）。
    if intent.external_entity is not None:
        return question
    # 只为结构化/复合跟进回填（叙事问句的 entity/period 是软过滤，不强补）。
    if intent.route not in _CARRY_FORWARD_ROUTES:
        return question

    addenda: list[str] = []
    if intent.entity is None:
        ent = memory.last_entity()
        if ent is not None:
            alias = _entity_alias(ent)
            if alias is not None:
                addenda.append(alias)
    if intent.period is None:
        per = memory.last_period()
        if per is not None:
            addenda.append(_period_text(per))

    if not addenda:
        return question
    return f"{question}（接续上文：{' '.join(addenda)}）"


class ConversationSession:
    """多轮会话封装（最小可用骨架）：绑定 store/provider/retriever，逐轮记忆 + 确定性跟进回填。

    每轮 ask 都重新走完整 answer_question（含安全门、found/not-found 改写、血缘回指、隔离两出口）——
    记忆只在 answer_question 之前做确定性槽位回填，绝不绕过任何 guard。
    """

    def __init__(
        self,
        store: FactStore,
        provider: LLMProvider,
        *,
        narrative_retriever: NarrativeRetriever | None = None,
        reference_date: date | None = None,
        intent_parser: IntentParser | None = None,
        memory: ConversationMemory | None = None,
    ):
        self.store = store
        self.provider = provider
        self.narrative_retriever = narrative_retriever
        self.reference_date = reference_date
        self._parser = intent_parser or RuleIntentParser()
        self.memory = memory or ConversationMemory()

    def ask(self, question: str) -> AgentResult:
        augmented = resolve_followup(
            self.memory, question,
            reference_date=self.reference_date, intent_parser=self._parser,
        )
        result = answer_question(
            augmented, self.store, self.provider,
            reference_date=self.reference_date,
            narrative_retriever=self.narrative_retriever,
            intent_parser=self._parser,
        )
        self._remember(augmented, result)
        return result

    def _remember(self, augmented: str, result: AgentResult) -> None:
        """记录本轮 home 槽位；被越权拒答 / 命中竞品的轮绝不写入（不污染记忆）。"""
        clar = result.clarification
        if clar is not None and clar.mode == CLARIFY_OUT_OF_SCOPE_ENTITY:
            return
        intent = self._parser.parse(augmented, reference_date=self.reference_date)
        if intent.external_entity is not None:  # 双保险：绝不记竞品轮
            return
        self.memory.remember(ConversationTurn(
            question=augmented, route=result.route,
            entity=intent.entity, period=intent.period,
        ))
