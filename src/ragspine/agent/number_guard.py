"""叙事数字防编造（ADR 0024）：叙事答案里的每个数字都必须能在检索片段里原样找到。

确定性、零 LLM。口径复用 ``common/answer_text`` 的 ``normalize_answer`` / ``contains_normalized``
（千分位、百分号词、NFKC、数量级金额），外加：

- 豁免（不当数值主张）：来源 doc / locator 字符串、``[n]`` / ``〔n〕`` / ``【n】`` 引用标号、页码 / 幻灯片 / 页图名引用、
  行首列表序号与（n）枚举、问句里已有的数字；
- 年份 / 期间标记（1H26、FY2024、2026 年…）：年份与问句或片段里的年份 / 期间一致即放行；
- 数量级金额（5.14 亿美元 ↔ US$514m）按数值等价比对；百分数与裸数不互证（44% ≠ 44 个百分点）。

有无依据的数字时确定性改写，不再调 LLM 修补：

- 开头（首句，过短则并入下一句）里就有无依据数字 → 核心结论无依据：整体改写为
  ``NUMBER_GUARD_NOTICE`` + 原答案中“只含有依据数字”的句子（片段原值）；
- 否则只删掉含无依据数字的句子，其余原样保留，末尾附 ``（注：…）`` 说明删了几处。
"""

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from ragspine.common.answer_text import contains_normalized, normalize_answer

NARRATIVE_NUMBER_GUARD_ENV = "RAGSPINE_NARRATIVE_NUMBER_GUARD"
# 叙事 system prompt 追加的推断约束（软约束；硬保障是下面的确定性校验）。
NUMBER_GUARD_RULE = "只用片段里原样出现的数字，不做计算；不推断顺序、因果或趋势；片段没有明说的，就回答资料中没有给出。"
# 改写后答案的开头：明确“没有直接给出”，不复述被移除的数字。
NUMBER_GUARD_NOTICE = (
    "资料中没有直接给出该数值：回答里有数字在检索片段中找不到原文，已按防编造规则移除（不做推算）。"
)
_RAW_VALUES_HEADER = "片段中的相关原值："
_REMOVED_NOTE = "（注：已移除 {n} 处在检索片段中找不到原文的数字，不做推算。）"
# 开头：从首句起累积到规范化后至少这么多字符（与 nl-gold 判分器的“首句过短并入下一行”同口径）。
_LEAD_MIN_CHARS = 12

# ---- 豁免：先在原文上剔除（不是数值主张） ----
_CITE_RE = re.compile(r"[\[［〔【]\s*\d+(?:\s*[,，、\-–]\s*\d+)*\s*[\]］〕】]")
_PAGE_REF_RE = re.compile(
    r"(?i)(?<![a-z])(?:pages?|slides?|pp?\.)\s*[=:：]?\s*\d+(?:\s*[-–,，、]\s*\d+)*(?:#[\w\-]+)?"
    r"|(?<![a-z])#?para\d+(?:-\d+)?"
    r"|第\s*\d+\s*[页頁张張]"
    r"|(?<![a-z0-9])p\d+(?:-\d+)?\.png"
)
_LIST_MARKER_RE = re.compile(r"(?m)^[ \t>]*(?:[-*+][ \t]+)?\d{1,2}[.)、．](?!\d)")
_PAREN_ENUM_RE = re.compile(r"[(（]\d{1,2}[)）]")

# ---- 规范化文本（normalize_answer 之后，已小写）上的模式 ----
_PERIOD_RE = re.compile(
    r"(?<![a-z0-9.])(?:[1-4]q|q[1-4]|[12]h|h[12]|fy|cy)\s*(?P<year>\d{4}|\d{2})(?![a-z0-9%]|\.\d)"
)
_HALF_MARK_RE = re.compile(r"(?<![a-z0-9.])(?:[1-4]q|q[1-4]|[12]h|h[12])(?![a-z0-9])")
_YEAR_RE = re.compile(r"(?<![\d.])(?:19|20)\d{2}(?![\d%]|\.\d)")
_SCALE = {
    "十亿": Decimal(10) ** 9,
    "billion": Decimal(10) ** 9,
    "bn": Decimal(10) ** 9,
    "b": Decimal(10) ** 9,
    "亿": Decimal(10) ** 8,
    "百万": Decimal(10) ** 6,
    "million": Decimal(10) ** 6,
    "mn": Decimal(10) ** 6,
    "m": Decimal(10) ** 6,
    "万": Decimal(10) ** 4,
    "千": Decimal(10) ** 3,
    "thousand": Decimal(10) ** 3,
    "k": Decimal(10) ** 3,
}
_NUMBER_RE = re.compile(
    r"(?<![\d.])(?P<num>\d+(?:\.\d+)?)(?P<pct>%)?"
    r"(?:\s*(?P<scale>十亿|billion|bn|亿|百万|million|mn|万|千|thousand|m|b|k)(?![a-z]))?"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])|(?<=\.)(?=\s)")
# 数量级金额与表格裸数互证时尝试的表格单位（财报表格多以 US$m 计）。
_TABLE_UNITS = (Decimal(10) ** 6, Decimal(10) ** 9, Decimal(10) ** 3)


def resolve_number_guard(value: bool | None) -> bool:
    """显式参数优先；None 读 RAGSPINE_NARRATIVE_NUMBER_GUARD（on|off，默认 on）。"""
    if value is not None:
        return value
    spec = (os.environ.get(NARRATIVE_NUMBER_GUARD_ENV) or "on").strip().lower()
    if spec not in ("on", "off"):
        raise ValueError(f"{NARRATIVE_NUMBER_GUARD_ENV} 只能是 on / off，收到 {spec!r}")
    return spec == "on"


def _year(two_or_four: str) -> int:
    return int(two_or_four) + (2000 if len(two_or_four) == 2 else 0)


def _known_years(normalized: str) -> set[int]:
    years = {_year(m.group("year")) for m in _PERIOD_RE.finditer(normalized)}
    return years | {int(m.group(0)) for m in _YEAR_RE.finditer(normalized)}


def _scaled_values(normalized: str) -> set[Decimal]:
    return {
        Decimal(m.group("num")) * _SCALE[m.group("scale")]
        for m in _NUMBER_RE.finditer(normalized)
        if m.group("scale") and not m.group("pct")
    }


@dataclass(frozen=True)
class _Grounding:
    """一次叙事作答的证据口径：规范化后的片段 / 问句、已知年份、数量级金额值、来源引用串。"""

    evidence: str
    question: str
    years: frozenset[int]
    scaled: frozenset[Decimal]
    refs: tuple[str, ...]

    @classmethod
    def build(
        cls, question: str, evidence: Sequence[str], source_refs: Sequence[str]
    ) -> "_Grounding":
        ev = normalize_answer("\n".join(evidence))
        q = normalize_answer(question)
        refs = tuple(sorted({r for r in source_refs if r}, key=len, reverse=True))
        return cls(
            evidence=ev,
            question=q,
            years=frozenset(_known_years(ev) | _known_years(q)),
            scaled=frozenset(_scaled_values(ev) | _scaled_values(q)),
            refs=refs,
        )

    def _strip_exempt(self, text: str) -> str:
        for ref in self.refs:
            text = text.replace(ref, " ")
        for pattern in (_CITE_RE, _PAGE_REF_RE, _LIST_MARKER_RE, _PAREN_ENUM_RE):
            text = pattern.sub(" ", text)
        return normalize_answer(text)

    def _scaled_ok(self, num: str, scale: str) -> bool:
        """数量级金额按数值等价：等于片段里某个带数量级的金额，或等于按表格单位（百万 / 十亿 / 千）
        计的片段裸数（“11.68 亿美元” ↔ 表格 “1,168”）。"""
        value = Decimal(num) * _SCALE[scale]
        if value in self.scaled:
            return True
        return any(
            contains_normalized(self.evidence, format((value / unit).normalize(), "f"))
            for unit in _TABLE_UNITS
        )

    def classify(self, text: str) -> tuple[list[str], int]:
        """(无依据的数字 token，按出现顺序去重；有依据的数字个数)。"""
        normalized = self._strip_exempt(text)
        bad: list[str] = []
        grounded = 0
        for m in _PERIOD_RE.finditer(normalized):
            if _year(m.group("year")) not in self.years and m.group(0) not in bad:
                bad.append(m.group(0))
        normalized = _HALF_MARK_RE.sub(" ", _PERIOD_RE.sub(" ", normalized))
        for m in _NUMBER_RE.finditer(normalized):
            needle = m.group("num") + (m.group("pct") or "")
            scale = m.group("scale")
            ok = (
                contains_normalized(self.evidence, needle)
                or contains_normalized(self.question, needle)
                or (
                    scale is not None
                    and not m.group("pct")
                    and self._scaled_ok(m.group("num"), scale)
                )
                or (
                    not m.group("pct")
                    and _YEAR_RE.fullmatch(needle) is not None
                    and int(needle) in self.years
                )
            )
            if ok:
                grounded += 1
            elif needle not in bad:
                bad.append(needle)
        return bad, grounded


def ungrounded_numbers(
    answer: str, *, question: str, evidence: Sequence[str], source_refs: Sequence[str] = ()
) -> list[str]:
    """答案里找不到片段依据的数字（规范化 token，按出现顺序去重）；空列表 = 全部有依据。"""
    return _Grounding.build(question, evidence, source_refs).classify(answer)[0]


def _units(line: str) -> list[str]:
    return [u for u in _SENTENCE_SPLIT_RE.split(line) if u]


def _lead_units(lines: list[str]) -> list[str]:
    """开头：从首句起累积，直到规范化后不少于 _LEAD_MIN_CHARS 个字符。"""
    lead: list[str] = []
    size = 0
    for line in lines:
        for unit in _units(line):
            lead.append(unit)
            size += len(normalize_answer(unit))
            if size >= _LEAD_MIN_CHARS:
                return lead
    return lead


def guard_narrative_answer(
    answer: str, question: str, evidence: Sequence[str], source_refs: Sequence[str]
) -> tuple[str, int]:
    """(确定性改写后的答案, 无依据数字个数)。全部有依据时原样返回 (answer, 0)。

    开头有无依据数字：``NUMBER_GUARD_NOTICE``，再按原行序附上“至少含一个有依据数字、且不含无依据
    数字”的句子（``片段中的相关原值：``；没有这样的句子就只剩提示句）。
    开头有依据：删掉含无依据数字的句子（整行删空则删行），末尾附 ``_REMOVED_NOTE``。
    """
    grounding = _Grounding.build(question, evidence, source_refs)
    bad, _ = grounding.classify(answer)
    if not bad:
        return answer, 0
    lines = answer.splitlines()
    if any(grounding.classify(u)[0] for u in _lead_units(lines)):
        kept: list[str] = []
        for line in lines:
            units = [u for u in _units(line) if (c := grounding.classify(u)) and not c[0] and c[1]]
            if units:
                kept.append("".join(units).strip())
        if not kept:
            return NUMBER_GUARD_NOTICE, len(bad)
        return "\n".join([NUMBER_GUARD_NOTICE, _RAW_VALUES_HEADER, *kept]), len(bad)
    out: list[str] = []
    for line in lines:
        units = _units(line)
        kept_units = [u for u in units if not grounding.classify(u)[0]]
        if kept_units == units:
            out.append(line)
        elif "".join(kept_units).strip(" \t*_-|>"):
            out.append("".join(kept_units).rstrip())
    return "\n".join([*out, _REMOVED_NOTE.format(n=len(bad))]), len(bad)
