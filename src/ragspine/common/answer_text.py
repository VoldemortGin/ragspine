"""答案 / 证据文本的数字友好规范化与带数字边界的包含判断。

评测判分（``eval/nl_gold_ragspine``）与叙事数字防编造（``agent/number_guard``，ADR 0024）共用同一口径：
NFKC、弯直引号、大小写、千分位、百分号、数量级金额、标点、空白。
"""

import re
import unicodedata
from decimal import Decimal

_CHAR_FOLD = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "′": "'",
        "–": "-",
        "—": "-",
        "−": "-",
    }
)
# 答案里的来源回指（如 "deck.md@page=18#para3"）不是内容，匹配前剔除，免得页码/段号冒充数值。
_LOCATOR_RE = re.compile(r"[\w.\-]*@page=\d+(?:#[\w\-]+)?")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_PERCENT_WORD_RE = re.compile(r"(?<=\d)\s*(?:per\s*cent|percent)\b")
_SPACED_PERCENT_RE = re.compile(r"(?<=\d)\s+%")
# 除 \w、%、小数点外的一切标点折成空格（冒号/破折号/引号/markdown 强调符 都不影响匹配）。
_PUNCT_RE = re.compile(r"[^\w%.]+|_+")
_NON_DECIMAL_DOT_RE = re.compile(r"(?<!\d)\.|\.(?!\d)")
_SPACES_RE = re.compile(r"\s+")
# 带币种的数量级金额（"5.14 亿美元"、"US$5.14 billion"、"$1.168b"）：换算成百万后附在原文后。
# 数量级词相对“百万”的小数点位移；只收小数（整数需补零，精度不够）。
_SCALE_SHIFT = {"十亿": 3, "亿": 2, "billion": 3, "bn": 3, "b": 3}
_SCALED_AMOUNT_RE = re.compile(
    r"(?P<prefix>(?:(?:us|hk|s|a|nz)?\$|usd|hkd|rmb|cny)\s*)?"
    r"(?<![\d.])(?P<num>\d+\.\d+)\s*(?P<scale>十亿|亿|billion|bn|b)(?![a-z])"
    r"(?P<suffix>\s*(?:美元|港元|港币|人民币|元|dollars?|usd|hkd))?"
)


def _append_millions(match: re.Match[str]) -> str:
    """同一数量级内换算：小数位不少于位移才换算（不补零），结果精度与原文一致。"""
    if not (match.group("prefix") or match.group("suffix")):
        return match.group(0)
    number = match.group("num")
    shift = _SCALE_SHIFT[match.group("scale")]
    if len(number.split(".")[1]) < shift:
        return match.group(0)
    millions = format(Decimal(number).scaleb(shift), "f")
    return f"{match.group(0)} {millions} million "


def normalize_answer(text: str) -> str:
    """答案 / 期望文本的统一规范化：NFKC、弯直引号、大小写、千分位、百分号、数量级金额、标点、空白。"""
    folded = unicodedata.normalize("NFKC", text).translate(_CHAR_FOLD).casefold()
    folded = _LOCATOR_RE.sub(" ", folded)
    folded = _THOUSANDS_RE.sub("", folded)
    folded = _PERCENT_WORD_RE.sub("%", folded)
    folded = _SPACED_PERCENT_RE.sub("%", folded)
    folded = _SCALED_AMOUNT_RE.sub(_append_millions, folded)
    folded = _PUNCT_RE.sub(" ", folded)
    folded = _NON_DECIMAL_DOT_RE.sub(" ", folded)
    return _SPACES_RE.sub(" ", folded).strip()


def contains_normalized(haystack_normalized: str, needle: str) -> bool:
    """已规范化的答案是否含 needle（needle 在此规范化）；数字两端不许粘连别的数字。"""
    target = normalize_answer(needle)
    if not target:
        return False
    lead = r"(?<![\d.])" if target[0].isdigit() else ""
    trail = r"(?![\d%]|\.\d)" if target[-1].isdigit() else ""
    return re.search(lead + re.escape(target) + trail, haystack_normalized) is not None
