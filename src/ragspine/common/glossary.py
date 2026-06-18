"""维度同义词词典与归一化。

口语/中文/缩写 → 受控维度代码（metric_code / entity / geography / period）。
这是"结构化数字通路"的入口：查询参数和抽取出的行列标签都先过这里归一，
无法识别就返回 None，由上层决定"查不到"还是"跳过并告警"，绝不猜测。
"""

import re
from datetime import date
from typing import cast

from ragspine.common.company_profile import (
    DimensionSpec,
    DomainProfile,
    load_company_profile,
)

# home 公司 profile（模块导入时载一次）：实体同义词 / 地理 / 外部实体清单 / 声明维度
# 词表皆由此构建，代码不再硬编码 "ACME" 或金融词表。文件缺失时静默回退内置默认
# （= 原硬编码 ACME 值），既有行为不变。
_PROFILE = load_company_profile()


def _dim(profile: DomainProfile, name: str) -> DimensionSpec | None:
    """取 profile 中名为 name 的 DimensionSpec；缺失返回 None。"""
    return next((d for d in profile.dimensions if d.name == name), None)


# 指标同义词：缩写 / 英文全称 / 中文 → metric_code（来自 profile 的 metric 维，
# 默认 = ACME 金融值；与原内联字面量字节级等价）。
METRIC_SYNONYMS: dict[str, str] = dict(cast(DimensionSpec, _dim(_PROFILE, "metric")).synonyms)

# 实体同义词：中文/英文/缩写 → entity_code（来自 home 公司 profile，默认= ACME 值）
ENTITY_SYNONYMS: dict[str, str] = dict(_PROFILE.home_entity_synonyms)

# 实体默认地理口径（来自 home 公司 profile）
ENTITY_GEOGRAPHY: dict[str, str] = dict(_PROFILE.entity_geography)

# 外部 / 竞品实体同义词：别名 → 展示名（来自 home 公司 profile）。
# 命中即视为越权（系统无外部数据），由上层拒答并提议改查 home 等价口径。
EXTERNAL_ENTITY_SYNONYMS: dict[str, str] = dict(_PROFILE.external_entities)

# 指标默认单位（来自 profile 的 metric 维 units，默认= ACME 金融值；字节级等价）
METRIC_UNITS: dict[str, str] = dict(cast(DimensionSpec, _dim(_PROFILE, "metric")).units)


def _clean(text: str) -> str:
    """统一小写、去首尾空白、压缩连续空白，便于匹配。"""
    return re.sub(r"\s+", " ", text.strip().lower())


# 同义词查表用小写键缓存：词典键大小写不一（如中文条目混入大写公司前缀
# "ACME香港"），而归一化后的查询文本统一小写——查表前把键一并降为小写，
# 保证大小写不敏感匹配，公共词典常量本身保持不变（EXT-11 逐字回归守护）。
_METRIC_SYNONYMS_LOWER: dict[str, str] = {k.lower(): v for k, v in METRIC_SYNONYMS.items()}
_ENTITY_SYNONYMS_LOWER: dict[str, str] = {k.lower(): v for k, v in ENTITY_SYNONYMS.items()}


def normalize_metric(raw: str | None) -> str | None:
    """指标名归一化为 metric_code；无法识别返回 None。"""
    if not raw:
        return None
    key = _clean(raw)
    if key in _METRIC_SYNONYMS_LOWER:
        return _METRIC_SYNONYMS_LOWER[key]
    # 容错：去掉括号注释（如 "REVENUE (US$m)" / "营收（百万美元）"）后再试
    stripped = re.sub(r"[\(（].*?[\)）]", "", key).strip()
    return _METRIC_SYNONYMS_LOWER.get(stripped)


def normalize_entity(raw: str | None) -> str | None:
    """实体名归一化为 entity_code；无法识别返回 None。"""
    if not raw:
        return None
    key = _clean(raw)
    if key in _ENTITY_SYNONYMS_LOWER:
        return _ENTITY_SYNONYMS_LOWER[key]
    # 容错：标题常形如 "ACME Hong Kong — Financial Performance"，取破折号前主体再试
    head = re.split(r"[—\-–|:：]", key, maxsplit=1)[0].strip()
    return _ENTITY_SYNONYMS_LOWER.get(head)


def resolve_external_entity(text: str | None) -> str | None:
    """文本中是否命中外部/竞品实体；命中返回展示名（最长匹配、大小写不敏感），否则 None。

    最长匹配：按别名长度降序，整体吃掉如"中国竞安"（长于"竞安"），避免遮蔽短别名后
    残留 home 词"中国"。home 词（ACME/中国等）不在外部清单内，绝不被误判为外部。
    """
    if not text:
        return None
    key = _clean(text)
    for alias in sorted(EXTERNAL_ENTITY_SYNONYMS, key=len, reverse=True):
        if alias and alias in key:
            return EXTERNAL_ENTITY_SYNONYMS[alias]
    return None


def geography_for_entity(entity_code: str) -> str | None:
    """实体的默认地理口径。"""
    return ENTITY_GEOGRAPHY.get(entity_code)


def unit_for_metric(metric_code: str) -> str | None:
    """指标的默认单位。"""
    return METRIC_UNITS.get(metric_code)


def normalize_period(raw: str | None) -> tuple[str, str] | None:
    """期间归一化为 (period_type, period)。

    支持：
        'FY2024' / '2024' / 'FY 2024'        -> ('FY', '2024')
        '2024H1' / '2024 H1' / 'FY2024H1'    -> ('HY', '2024H1')
        '2025Q1' / '2025 Q1'                 -> ('QUARTER', '2025Q1')
    无法识别返回 None。
    """
    if not raw:
        return None
    key = re.sub(r"\s+", "", raw.strip().upper())
    key = key.replace("FY", "")  # 'FY2024' / 'FY2024H1' 前缀去掉

    # 半年：2024H1 / 2024H2
    m = re.fullmatch(r"(\d{4})H([12])", key)
    if m:
        return ("HY", f"{m.group(1)}H{m.group(2)}")

    # 季度：2025Q1..Q4
    m = re.fullmatch(r"(\d{4})Q([1-4])", key)
    if m:
        return ("QUARTER", f"{m.group(1)}Q{m.group(2)}")

    # 全年：2024
    m = re.fullmatch(r"(\d{4})", key)
    if m:
        return ("FY", m.group(1))

    return None


# ---------------------------------------------------------------------------
# 相对期间解析（增量扩展：normalize_period 只管绝对期间，相对期间走这里）
# ---------------------------------------------------------------------------

# 年份偏移词：去年/今年/前年（含英文）
_RELATIVE_YEAR_OFFSETS: dict[str, int] = {
    "今年": 0,
    "本年": 0,
    "this year": 0,
    "去年": -1,
    "上年": -1,
    "last year": -1,
    "前年": -2,
}

# 半年词 → H1/H2
_HALF_WORDS: dict[str, str] = {
    "上半年": "H1",
    "下半年": "H2",
}

# 季度词 → 相对当前季度的偏移
_RELATIVE_QUARTER_OFFSETS: dict[str, int] = {
    "本季度": 0,
    "这个季度": 0,
    "这季度": 0,
    "上季度": -1,
    "上个季度": -1,
}


def resolve_relative_period(
    raw: str | None, reference_date: date | None = None
) -> tuple[str, str] | None:
    """相对期间解析为 (period_type, period)，以 reference_date 为基准（默认今天）。

    支持：
        '去年' / '今年' / '前年' / 'last year'      -> ('FY', 'YYYY')
        '上半年' / '去年上半年' / '今年下半年'        -> ('HY', 'YYYYH1/H2')
        '本季度' / '上季度' / '上个季度'              -> ('QUARTER', 'YYYYQn')
    绝对期间（FY2024/2024H1 等）与无法识别的输入返回 None，由 normalize_period 处理。
    """
    if not raw:
        return None
    ref = reference_date or date.today()
    key = _clean(raw)

    # 半年：可带年份偏移前缀（去年上半年），裸"上半年"默认今年
    for half_word, half in _HALF_WORDS.items():
        if key.endswith(half_word):
            prefix = key[: -len(half_word)].strip()
            if not prefix:
                return ("HY", f"{ref.year}{half}")
            offset = _RELATIVE_YEAR_OFFSETS.get(prefix)
            if offset is not None:
                return ("HY", f"{ref.year + offset}{half}")
            return None

    # 整年
    offset = _RELATIVE_YEAR_OFFSETS.get(key)
    if offset is not None:
        return ("FY", str(ref.year + offset))

    # 季度（按参考日期所在季度做偏移，跨年自动回卷）
    q_offset = _RELATIVE_QUARTER_OFFSETS.get(key)
    if q_offset is not None:
        current_q = (ref.month - 1) // 3 + 1
        total = ref.year * 4 + (current_q - 1) + q_offset
        return ("QUARTER", f"{total // 4}Q{total % 4 + 1}")

    return None
