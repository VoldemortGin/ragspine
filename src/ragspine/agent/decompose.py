"""W6a 查询分解（opt-in，默认关）：把真多跳问题拆成多个子问题，分别回答再确定性合成。

现状（docs/prd-quality-depth.md W6a）：编排是单发、规则路由 retrieve-then-generate；分解只有
确定性笛卡尔（intent.expand_subtasks，仅在用户明确列举的轴上展开）。本模块沿既有 IntentParser/
QueryRewriter 缝（ADR 0010 已把"问什么"从安全判定解耦）加一个 **LLM 驱动** 的子问题分解：
"哪个区域增长最快、为什么"这类一句话夹多跳的问题，拆成 N 个独立子问题，各自走 answer_question 的
既有通路（结构化/叙事 + 全部 guard），再确定性合成。

硬约束（守 ADR 0001 确定性 + 反编造）：
- **默认关、字节不变**：answer_question 的 decomposer 参数默认 None，不注入即整条主流程逐位不变。
- **不绕过任何 guard**：每个子问题重新跑完整 answer_question——独立过安全门（越权/竞品拒答）、独立
  做 found/not-found 改写。分解只决定"问什么"，绝不在合成处夹带模型散文或编造数字。
- **确定性合成**：子答案以固定模板拼接（各子答案已是各通路 guard 后的结果），合成本身零 LLM。
- **有界**：子问题数量上限 max_subquestions，防发散；解析失败/provider 故障 → 退回原问句单元素表。

LLM 分解非确定，故仅作 opt-in 适配器（经 make_decomposer / RAGSPINE_QUERY_DECOMPOSE 选用，且必须
注入 provider 才生效）；默认仍是 RuleIntentParser 的确定性笛卡尔。
"""

import json
from datetime import date
from typing import Protocol, runtime_checkable

from ragspine.agent.llm_provider import LLMProvider, ProviderError

# 分解结果的路由标记：与 structured/narrative/composite 区分，仅在注入 decomposer 且真分解时出现。
ROUTE_DECOMPOSED = "decomposed"

# 分解选型读取的环境变量名（缺省 spec 时生效）。
QUERY_DECOMPOSE_ENV = "RAGSPINE_QUERY_DECOMPOSE"

# 子问题数量默认上限（有界，防发散）。
DEFAULT_MAX_SUBQUESTIONS = 4

# 分解提示：要求模型只输出一个 JSON 字符串数组（子问题），不可分解则原样返回单元素数组。
_DECOMPOSE_SYSTEM = (
    "你是查询分解器。把用户的复杂多跳问题拆成若干个相互独立、可分别检索回答的子问题，"
    "只输出一个 JSON 字符串数组，不要任何解释。若问题本就单一、无需分解，返回只含原问题的单元素数组。"
)


@runtime_checkable
class QueryDecomposer(Protocol):
    """查询分解协议：把一个问句拆成 1..N 个子问句。

    约定：返回 **非空** 列表；不可分解时返回 [原问句]（长度 1，调用方据此回退到正常单发路由）。
    实现可为非确定（LLM），故只作 opt-in 注入件——默认 None＝不分解，主流程字节不变。
    """

    def decompose(
        self, question: str, *, reference_date: date | None = None
    ) -> list[str]: ...


class LLMQueryDecomposer:
    """LLM 驱动的查询分解器（opt-in）。

    单轮调用 provider 让其产出 JSON 字符串数组；鲁棒解析 + 有界截断 + 确定性降级：
    - provider 抛 ProviderError（网络/API 故障）→ 返回 [question]（不分解、不崩）；
    - 回文非 JSON 数组 / 数组空 / 元素非字符串 → 返回 [question]；
    - 数组超过 max_subquestions → 截断到上限（防发散）。
    """

    def __init__(self, provider: LLMProvider, *, max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS):
        self.provider = provider
        self.max_subquestions = max(1, max_subquestions)

    def decompose(
        self, question: str, *, reference_date: date | None = None
    ) -> list[str]:
        try:
            resp = self.provider.chat([
                {"role": "system", "content": _DECOMPOSE_SYSTEM},
                {"role": "user", "content": question},
            ])
        except ProviderError:
            return [question]
        text = resp.choices[0].message.content or ""
        subs = _parse_subquestions(text)
        if not subs:
            return [question]
        return subs[: self.max_subquestions]


def _parse_subquestions(text: str) -> list[str]:
    """从模型回文鲁棒解析 JSON 字符串数组；任何不合规一律视为"无法分解"返回空表。"""
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    subs = [s.strip() for s in parsed if isinstance(s, str) and s.strip()]
    return subs


def make_decomposer(
    spec: str | None = None, *, provider: LLMProvider | None = None
) -> QueryDecomposer | None:
    """分解器选型工厂：把「是否 LLM 分解」从改代码降为一个 spec/env，默认 None＝不分解（行为不变）。

    spec 取值（大小写/留白/连字符不敏感；缺省读环境变量 RAGSPINE_QUERY_DECOMPOSE）：
        - None / 'none'  -> None（不分解；answer_question 走既有确定性笛卡尔单发路由，字节不变）
        - 'llm'          -> 注入了 provider 则 LLMQueryDecomposer；未注入 provider 则 None
                            （"注入 provider 才生效"——诚实降级为不分解，绝不空跑）
        - 其他           -> ValueError

    返回 QueryDecomposer 实例或 None（可直接喂给 answer_question 的 decomposer 参数）。
    """
    if spec is None:
        import os

        spec = os.environ.get(QUERY_DECOMPOSE_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "none":
        return None
    if normalized == "llm":
        if provider is None:
            return None
        return LLMQueryDecomposer(provider)
    raise ValueError(
        f"未知 query-decompose spec：{normalized!r}（可选 none / llm；llm 需注入 provider）"
    )
