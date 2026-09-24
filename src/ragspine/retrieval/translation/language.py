"""确定性语言检测（零外部依赖）：按 CJK 字符数与拉丁词数的比例判断中 / 英。

只区分两种文字体系：含 CJK 统一表意文字的记为 ``zh``，以拉丁字母为主的记为 ``en``，两者都没有
（纯数字 / 标点 / 空串）记为 ``und``。一个 CJK 字符与一个拉丁词等权（1H26 这类字母数字混写的期间标签不算词）——中文问句里夹几个指标缩写
（"泰国 1H26 VONB"）仍判为 zh，英文文档里偶尔出现的中文名仍判为 en。
"""

import re
from collections.abc import Iterable
from typing import Any

LANG_ZH = "zh"
LANG_EN = "en"
LANG_UNKNOWN = "und"

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN_WORD = re.compile(r"\b[A-Za-z]+\b")


def _script_counts(text: str) -> tuple[int, int]:
    """(CJK 字符数, 拉丁词数)。"""
    return len(_CJK.findall(text)), len(_LATIN_WORD.findall(text))


def _classify(cjk: int, latin: int) -> str:
    if cjk == 0 and latin == 0:
        return LANG_UNKNOWN
    return LANG_ZH if cjk >= latin else LANG_EN


def detect_language(text: str) -> str:
    """一段文本的语言：``zh`` / ``en`` / ``und``。"""
    return _classify(*_script_counts(text))


def _metadata_language(value: str) -> str:
    primary = value.strip().lower().replace("_", "-").split("-")[0]
    return primary if primary in (LANG_ZH, LANG_EN) else LANG_UNKNOWN


def corpus_language(chunks: Iterable[Any]) -> str:
    """一组块的语言：块元数据 ``language`` 全部可识别且一致时直接用它，否则按全部块正文的字符统计。"""
    items = list(chunks)
    declared = {_metadata_language(getattr(c, "language", "") or "") for c in items}
    if len(declared) == 1 and LANG_UNKNOWN not in declared:
        return declared.pop()
    cjk = latin = 0
    for chunk in items:
        c, w = _script_counts(chunk.text)
        cjk += c
        latin += w
    return _classify(cjk, latin)
