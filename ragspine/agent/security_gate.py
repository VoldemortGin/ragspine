"""确定性安全门（ADR 0010）：越权/竞品拒答 + 命中遮蔽。

安全门是【不可插拔、永不调 LLM】的前门守卫，与（可替换的）意图解析器解耦：
意图解析器把"问的是哪个指标/期间"这类**非安全**判断做成 Protocol 可换实现，
而"这是不是越权/竞品主体"这类**安全**判断永远走这里，确定性、零外部调用。

唯一真源：外部/竞品实体的最长匹配 + 等长空格遮蔽（防"中国竞安"遮蔽后残留"中国"
泄露成 home 实体 ACME_CN），以及命中即拒答的文案/收窄项构造，皆集中于此。
检测数据（外部清单）与 home 公司名由 DomainProfile 声明，本模块不硬编码任何公司。
"""

import re
from dataclasses import dataclass, field

# 决策码：ALLOW 放行 / REFUSE_OUT_OF_SCOPE 越权拒答。
# REFUSE 取值与 intent.CLARIFY_OUT_OF_SCOPE_ENTITY 一致，便于澄清网关直接映射。
SECURITY_ALLOW = "allow"
SECURITY_REFUSE_OUT_OF_SCOPE = "out_of_scope_entity"


@dataclass(frozen=True)
class SecurityScreen:
    """检测结果：命中的外部主体展示名（未命中为 None）+ 遮蔽后文本。"""

    external_entity: str | None
    masked_text: str


@dataclass
class SecurityVerdict:
    """拒答裁决：决策码 + 命中主体 + 拒答文案 + 收窄项（放行时仅决策码）。"""

    decision: str
    external_entity: str | None = None
    message: str | None = None
    narrowing_options: list[str] = field(default_factory=list)


class SecurityGate:
    """确定性安全门。由外部/竞品清单 + home 公司名构造，不依赖意图解析器或 LLM。

    external_entities：{别名 → 展示名}（DomainProfile 声明，命中即视为越权）。
    home_company_name：拒答提议改查时的泛化口径主体名。
    """

    def __init__(self, external_entities: dict[str, str], home_company_name: str):
        self._external = dict(external_entities)
        self._home_company_name = home_company_name
        # 别名按长度降序预排：保证"中国竞安"整体先于"竞安"命中（最长匹配）。
        self._aliases = sorted((a for a in self._external if a), key=len, reverse=True)

    @staticmethod
    def _clean(text: str) -> str:
        """统一小写、去首尾空白、压缩连续空白（与 intent/glossary 归一化一致）。"""
        return re.sub(r"\s+", " ", text.strip().lower())

    def detect(self, text: str) -> SecurityScreen:
        """最长匹配外部主体并整体遮蔽为等长空格。

        命中：返回 (展示名, 遮蔽后文本)——遮蔽保持后续 home 实体匹配的位置语义，
        且让残留文本不再含可泄露成 home 实体的子串（防"中国"碰撞）。
        未命中：返回 (None, 归一后文本)。
        """
        clean = self._clean(text)
        for alias in self._aliases:
            pos = clean.find(alias.lower())
            if pos >= 0:
                end = pos + len(alias)
                masked = clean[:pos] + " " * len(alias) + clean[end:]
                return SecurityScreen(self._external[alias], masked)
        return SecurityScreen(None, clean)

    def screen(self, *, raw_question: str, metric: str | None) -> SecurityVerdict:
        """对原始问句独立复核：命中外部主体即越权拒答，否则放行。

        只吃 raw_question + metric，不读任何解析器产出的字段——这样即便换上别的
        意图解析器（如 LLM 后端）漏判竞品，安全门仍能确定性拦截。
        """
        scr = self.detect(raw_question)
        if scr.external_entity is None:
            return SecurityVerdict(decision=SECURITY_ALLOW)
        metric_clause = f"的 {metric}" if metric else "的对应数字"
        message = (
            f"系统不掌握「{scr.external_entity}」的数据，无法回答该外部/竞品主体的"
            f"问题。可改查 {self._home_company_name}{metric_clause}。"
        )
        return SecurityVerdict(
            decision=SECURITY_REFUSE_OUT_OF_SCOPE,
            external_entity=scr.external_entity,
            message=message,
            narrowing_options=[f"改查 {self._home_company_name}{metric_clause}"],
        )
