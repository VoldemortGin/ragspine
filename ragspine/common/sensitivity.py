"""叙事入库的【确定性敏感度分级】策略与纯函数分级器。

安全 P0：narrative_ingest 对未显式标 sensitivity 的文档原本一律落 'INTERNAL'，
RESTRICTED 文档若漏标即以 INTERNAL 流进检索/prompt/回答，两道出域过滤
（narrative_link / listwise_rerank 剔除 RESTRICTED）形同虚设——漏标 = 泄露。

分级器的「未标 = fail-safe 到高敏」以【信号命中即升级 RESTRICTED】为主力机制
（文件名/路径受限模式、正文受限关键词）；blanket「一切未分类 → RESTRICTED」会把
正常财报也藏掉、击穿 41 golden 与检索，故仅作可选 strict 开关
（escalate_unknown_to_restricted，默认 False 保行为）。

规则全部由 SensitivityPolicy 承载，从 config 的 [sensitivity] 段读入
（见 ragspine/common/company_profile.py），代码里不硬编码任何公司专属词。
"""

from dataclasses import dataclass, field

# 受控敏感度级别（与 chunk_store / 出域过滤约定一致）。
RESTRICTED = "RESTRICTED"


@dataclass(frozen=True)
class SensitivityPolicy:
    """敏感度分级策略（不可变；从 [sensitivity] 段读入，缺省回退内置默认）。

    字段：
        default_level:                   无信号且非 strict 时的落地级别（默认 'INTERNAL'）。
        escalate_unknown_to_restricted:  strict 开关——无信号文档亦升级 RESTRICTED（默认 False）。
        restricted_filename_patterns:    文件名/路径命中任一即 RESTRICTED（大小写不敏感子串）。
        restricted_keywords:             正文命中任一即 RESTRICTED（大小写不敏感子串）。
    """

    default_level: str = "INTERNAL"
    escalate_unknown_to_restricted: bool = False
    restricted_filename_patterns: list[str] = field(default_factory=list)
    restricted_keywords: list[str] = field(default_factory=list)


def classify_sensitivity(filename: str, text: str, policy: SensitivityPolicy) -> str:
    """确定性分级：返回敏感度级别字符串，零外部调用。

    优先级（命中即返回，全大小写不敏感子串匹配）：
        1) filename/路径命中任一 restricted_filename_patterns → RESTRICTED；
        2) 否则 text 命中任一 restricted_keyword → RESTRICTED；
        3) 否则 escalate_unknown_to_restricted 为 True → RESTRICTED；
        4) 否则 policy.default_level。
    """
    name_lower = filename.lower()
    if any(p.lower() in name_lower for p in policy.restricted_filename_patterns):
        return RESTRICTED
    text_lower = text.lower()
    if any(k.lower() in text_lower for k in policy.restricted_keywords):
        return RESTRICTED
    if policy.escalate_unknown_to_restricted:
        return RESTRICTED
    return policy.default_level
