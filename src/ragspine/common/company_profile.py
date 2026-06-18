"""领域 profile（DomainProfile）的配置化载入。

项目是【通用】管理 copilot，不专属任何一家公司，也不专属金融领域：home 身份、实体
同义词、默认实体、地理口径、外部/竞品实体清单，以及【声明维度】（DimensionSpec，
如 metric / entity / period / channel / geography 的词表与行为旗标，ADR 0004）全部
来自一个 TOML 配置文件（config/company.toml），代码里不再硬编码 "ACME" 或金融词表。

历史名 CompanyProfile 仍作为模块级别名指向同一个 DomainProfile 类对象（构造 / isinstance
均不变），便于既有调用平滑过渡。

文件缺失时静默回退内置默认 profile（值 = 现有 ACME 金融值），保证 import 期零副作用、
既有行为字节级不变（glossary 模块导入时即调用本模块构建词典）。

TOML 读取：Python>=3.11 用 stdlib tomllib；3.10 用第三方 tomli（pyproject 条件依赖）。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python >= 3.11
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from ragspine.common.sensitivity import SensitivityPolicy

# 本部署默认配置文件路径（config/company.toml，相对仓库根）。
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "company.toml"

# 运行期换公司用的环境变量：path 缺省时优先读它（指向另一份 toml），
# 无需就地编辑文件即可切换部署。未设时回退 _DEFAULT_CONFIG_PATH。
_CONFIG_PATH_ENV_VAR = "RAGSPINE_COMPANY_CONFIG"


# 内置默认（= 现有 glossary 硬编码 ACME 值，字节级等价）。
# 文件缺失 / 解析不出对应段时回退至此，保证零行为变更。
_DEFAULT_HOME_COMPANY_NAME = "ACME Group"
_DEFAULT_HOME_ENTITY_CODE = "ACME_GROUP"
_DEFAULT_HOME_ENTITY_SYNONYMS: dict[str, str] = {
    # 集团
    "acme group": "ACME_GROUP",
    "acme": "ACME_GROUP",
    "group": "ACME_GROUP",
    "ACME集团": "ACME_GROUP",
    "ACME": "ACME_GROUP",
    "集团": "ACME_GROUP",
    # 香港
    "acme hong kong": "ACME_HK",
    "acme hk": "ACME_HK",
    "hong kong": "ACME_HK",
    "hk": "ACME_HK",
    "香港": "ACME_HK",
    "ACME香港": "ACME_HK",
    # 中国内地
    "acme china": "ACME_CN",
    "acme cn": "ACME_CN",
    "china": "ACME_CN",
    "cn": "ACME_CN",
    "中国": "ACME_CN",
    "中国内地": "ACME_CN",
    "ACME中国": "ACME_CN",
    "ACME人寿": "ACME_CN",
    # 受控代码本身可直接传入（已归一的查询参数）
    "acme_group": "ACME_GROUP",
    "acme_hk": "ACME_HK",
    "acme_cn": "ACME_CN",
}
_DEFAULT_ENTITY_GEOGRAPHY: dict[str, str] = {
    "ACME_GROUP": "ASIA",
    "ACME_HK": "HK",
    "ACME_CN": "CN",
}
# home 实体展示名（entity_code → 用户可见名）。表现层（澄清文案 / 系统 prompt /
# tool schema 示例）由此派生，不再硬编码 "ACME"。
_DEFAULT_HOME_ENTITY_LABELS: dict[str, str] = {
    "ACME_GROUP": "ACME Group",
    "ACME_HK": "ACME Hong Kong",
    "ACME_CN": "ACME China",
}
# 默认指标维词表（= 现有 glossary.METRIC_SYNONYMS 字节级副本，含混合大小写键）。
# 缩写 / 英文全称 / 中文 → metric_code。下游 glossary 的小写缓存逐字依赖此键集。
_DEFAULT_METRIC_SYNONYMS: dict[str, str] = {
    "revenue": "REVENUE",
    "营收": "REVENUE",
    "newsales": "NEWSALES",
    "new sales": "NEWSALES",
    "新签金额": "NEWSALES",
    "profit": "PROFIT",
    "operating profit": "PROFIT",
    "营运利润": "PROFIT",
    "roe": "ROE",
    "return on equity": "ROE",
    "净资产收益率": "ROE",
    "股本回报率": "ROE",
}
# 默认指标单位（= 现有 glossary.METRIC_UNITS 字节级副本）：metric_code → 单位。
_DEFAULT_METRIC_UNITS: dict[str, str] = {
    "REVENUE": "USD_M",
    "NEWSALES": "USD_M",
    "PROFIT": "USD_M",
    "ROE": "PCT",
}
# 默认指标展示名：metric_code → 展示名。codes-as-display，保证 tool schema 示例字节不变。
_DEFAULT_METRIC_LABELS: dict[str, str] = {
    "REVENUE": "REVENUE",
    "NEWSALES": "NEWSALES",
    "PROFIT": "PROFIT",
    "ROE": "ROE",
}
# 默认渠道维词表（= 现有 intent._CHANNEL_SYNONYMS 字节级副本）：别名 → channel_code。
_DEFAULT_CHANNEL_SYNONYMS: dict[str, str] = {
    "代理": "AGENCY",
    "代理人": "AGENCY",
    "agency": "AGENCY",
    "银保": "BANCA",
    "bancassurance": "BANCA",
    "banca": "BANCA",
}
# 外部/竞品实体清单（alias → 展示名）。
# 以"中国"开头的竞品全称（中国竞安/中国竞寿/中国竞平）作为完整键存在，
# 以便最长匹配整体吃掉"中国"，遮蔽后不残留 home 词"中国"泄露成 ACME_CN。
_DEFAULT_EXTERNAL_ENTITIES: dict[str, str] = {
    "竞安": "竞安(Jingan)",
    "中国竞安": "竞安(Jingan)",
    "jingan": "竞安(Jingan)",
    "竞诚": "Jingcheng",
    "jingcheng": "Jingcheng",
    "中国竞寿": "Jingshou",
    "jingshou": "Jingshou",
    "竞保": "Jingbao",
    "中国竞保": "Jingbao",
    "jingbao": "Jingbao",
    "中国竞平": "Jingping",
    "竞平保险": "Jingping",
    "竞康": "Jingkang",
    "jingkang": "Jingkang",
    "竞华保险": "Jinghua Life",
    "jinghua life": "Jinghua Life",
    "竞利": "Jingli",
    "jingli": "Jingli",
    "竞盛": "Jingsheng",
    "jingsheng": "Jingsheng",
    "竞联": "Jinglian",
    "jinglian": "Jinglian",
    "竞明": "Jingming",
    "jingming": "Jingming",
    "竞都": "Jingdu",
    "jingdu": "Jingdu",
    "竞东": "Jingdong",
    "jingdong": "Jingdong",
}
# 内置默认敏感策略（无 [sensitivity] 段时回退）：default_level='INTERNAL'、
# strict 开关 False、无受限模式/关键词 —— 保证既有行为字节级不变（漏标仍 INTERNAL）。
_DEFAULT_SENSITIVITY = SensitivityPolicy()


@dataclass(frozen=True)
class DimensionSpec:
    """一个声明维度的规格（ADR 0004）：名称 + 词表 + 行为旗标。不可变；集合字段
    一律 default_factory，避免裸 {} 在类定义期报错、或模块级共享 dict 跨实例串味。

    字段（最小正交集，每个都有真实消费者）：
        name / label:      维度受控名 + 展示名。
        kind:              'categorical' | 'temporal' | 'measure'（temporal 关联反编造
                           白名单；measure 才有 units）。
        synonyms:          {别名 → 受控值}（归一化）。
        units:             {受控值 → 单位}（仅 measure 维有意义）。
        labels:            {受控值 → 展示名}（澄清文案 / 示例派生）。
        default:           槽位缺失时的默认受控值（如 channel='TOTAL'）。
        required:          是否必填（缺失触发澄清）。
        clarify:           缺失时澄清策略 'ask_first' | 'assume' | 'none'。
        identity:          是否参与事实自然键（dim_key）；派生维（如 geography）为 False。
        expand:            是否参与多值笛卡尔展开（composite 子任务）。
        derived_from:      若本维由另一维派生，给出源维名（如 geography ← entity）。
        derivation:        {源受控值 → 本维受控值}（派生映射）。
        whitelist_in_fabrication_check: 反编造检查是否把本维 token 当合法期间剥离
                           （仅 temporal 维为 True）。
        fabrication_whitelist_regex: 反编造剥离用的【显式字面正则】（仅 temporal 维设此
                           字面；绝不从 synonyms/grain 自动派生）。None 时该维不剥离任何数字。
    """

    name: str
    label: str
    kind: str = "categorical"
    synonyms: dict[str, str] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    default: str | None = None
    required: bool = False
    clarify: str = "assume"
    identity: bool = True
    expand: bool = True
    derived_from: str | None = None
    derivation: dict[str, str] = field(default_factory=dict)
    whitelist_in_fabrication_check: bool = False
    fabrication_whitelist_regex: str | None = None


@dataclass(frozen=True)
class DomainProfile:
    """领域 profile（不可变）：home 身份 + 同义词 + 地理 + 外部实体清单 + 声明维度。

    字段：
        home_company_name:     home 公司展示名（拒答提议改查时用于泛化文案）。
        home_entity_code:      默认实体受控代码（用户未提实体时的默认口径）。
        home_entity_synonyms:  home 实体同义词 {别名 → entity_code}。
        entity_geography:      实体默认地理口径 {entity_code → geography}。
        external_entities:     外部/竞品实体 {别名 → 展示名}（命中即拒答）。
        home_entity_labels:    home 实体展示名 {entity_code → 用户可见名}（表现层文案派生用）。
        sensitivity:           敏感度分级策略（从 [sensitivity] 段读，缺省回退内置默认）。
        dimensions:            声明维度元组（DimensionSpec，ADR 0004）：每维携带词表 +
                               行为旗标（必填 / 澄清策略 / 单位 / 派生等）。默认空元组；
                               内置默认 profile 携带金融 5 维（metric/entity/period/
                               channel/geography）。当前无下游消费者读取本字段（DP-2 仅
                               铺设数据，行为零变更）。

    历史别名 ``CompanyProfile`` 在类定义后指向同一个类对象（见下方 ``CompanyProfile =
    DomainProfile``），保证 ``isinstance`` 与 ``CompanyProfile(**kwargs)`` 构造继续有效。
    """

    home_company_name: str
    home_entity_code: str
    home_entity_synonyms: dict[str, str] = field(default_factory=dict)
    entity_geography: dict[str, str] = field(default_factory=dict)
    external_entities: dict[str, str] = field(default_factory=dict)
    home_entity_labels: dict[str, str] = field(default_factory=dict)
    sensitivity: SensitivityPolicy = field(default_factory=SensitivityPolicy)
    dimensions: tuple[DimensionSpec, ...] = field(default_factory=tuple)


# 历史名别名：旧调用 / 测试以 CompanyProfile 构造并 isinstance 校验，指向同一类对象
# （非子类），保证两种契约不破。
CompanyProfile = DomainProfile


def _default_profile() -> DomainProfile:
    """内置默认 profile（= 现有 ACME 金融值）。返回副本，避免外部改动污染默认常量；
    dimensions 的每个 DimensionSpec 集合字段都 dict(...) 复制冻结常量，防被改穿。"""
    return DomainProfile(
        home_company_name=_DEFAULT_HOME_COMPANY_NAME,
        home_entity_code=_DEFAULT_HOME_ENTITY_CODE,
        home_entity_synonyms=dict(_DEFAULT_HOME_ENTITY_SYNONYMS),
        entity_geography=dict(_DEFAULT_ENTITY_GEOGRAPHY),
        external_entities=dict(_DEFAULT_EXTERNAL_ENTITIES),
        home_entity_labels=dict(_DEFAULT_HOME_ENTITY_LABELS),
        sensitivity=_DEFAULT_SENSITIVITY,
        dimensions=(
            DimensionSpec(
                "metric",
                label="指标",
                required=True,
                clarify="ask_first",
                synonyms=dict(_DEFAULT_METRIC_SYNONYMS),
                units=dict(_DEFAULT_METRIC_UNITS),
                labels=dict(_DEFAULT_METRIC_LABELS),
                whitelist_in_fabrication_check=False,
            ),
            DimensionSpec(
                "entity",
                label="实体",
                required=True,
                clarify="assume",
                synonyms=dict(_DEFAULT_HOME_ENTITY_SYNONYMS),
                labels=dict(_DEFAULT_HOME_ENTITY_LABELS),
            ),
            DimensionSpec(
                "period",
                label="期间",
                kind="temporal",
                required=True,
                clarify="assume",
                whitelist_in_fabrication_check=True,
                # 与 qa_eval._PERIOD_TOKEN_RE 字面逐字节相同；(?:19|20) 年份锚是 '9999'
                # 能被判编造的唯一原因，绝不可丢。显式字面，绝不从 synonyms/grain 派生。
                fabrication_whitelist_regex=(
                    r"(?:FY\s*)?(?:19|20)\d{2}\s*年?\s*"
                    r"(?:H\s*[12]|Q\s*[1-4]|上半年|下半年)?"
                ),
            ),
            DimensionSpec(
                "channel",
                label="渠道",
                required=False,
                clarify="none",
                default="TOTAL",
                expand=False,
                synonyms=dict(_DEFAULT_CHANNEL_SYNONYMS),
            ),
            DimensionSpec(
                "geography",
                label="地理",
                identity=False,
                expand=False,
                default="UNKNOWN",
                derived_from="entity",
                derivation=dict(_DEFAULT_ENTITY_GEOGRAPHY),
            ),
        ),
    )


def _str_map(raw: object) -> dict[str, str]:
    """把 TOML 表（[home.synonyms] 等）拍平为 dict[str, str]；非表则空 dict。"""
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _str_list(raw: object) -> list[str]:
    """把 TOML 数组拍平为 list[str]；非列表则空 list。"""
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw]


def _sensitivity_policy(data: dict[str, object]) -> SensitivityPolicy:
    """从 [sensitivity] 段构建策略；缺段或字段缺失逐项回退内置默认。"""
    raw = data.get("sensitivity")
    if not isinstance(raw, dict):
        return _DEFAULT_SENSITIVITY
    return SensitivityPolicy(
        default_level=str(raw.get("default_level", _DEFAULT_SENSITIVITY.default_level)),
        escalate_unknown_to_restricted=bool(
            raw.get(
                "escalate_unknown_to_restricted",
                _DEFAULT_SENSITIVITY.escalate_unknown_to_restricted,
            )
        ),
        restricted_filename_patterns=_str_list(raw.get("restricted_filename_patterns")),
        restricted_keywords=_str_list(raw.get("restricted_keywords")),
    )


def load_company_profile(path: str | Path | None = None) -> DomainProfile:
    """加载 home 领域 profile。

    path 缺省时先读环境变量 RAGSPINE_COMPANY_CONFIG（运行期换公司不靠就地编辑文件）；
    未设时找 config/company.toml。显式传 path 时优先于 env-var。
    文件不存在 / 解析不出某段时静默回退内置默认（不抛错、不打印），
    保证 glossary 模块导入期零副作用、既有行为不破。
    """
    if path is not None:
        config_path = Path(path)
    else:
        env_path = os.environ.get(_CONFIG_PATH_ENV_VAR)
        config_path = Path(env_path) if env_path else _DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        return _default_profile()

    with config_path.open("rb") as fh:
        data = tomllib.load(fh)

    default = _default_profile()
    home = data.get("home", {}) if isinstance(data.get("home"), dict) else {}

    return DomainProfile(
        home_company_name=str(home.get("company_name", default.home_company_name)),
        home_entity_code=str(home.get("entity_code", default.home_entity_code)),
        home_entity_synonyms=_str_map(home.get("synonyms")) or default.home_entity_synonyms,
        entity_geography=_str_map(home.get("geography")) or default.entity_geography,
        external_entities=_str_map(data.get("external_entities"))
        or default.external_entities,
        home_entity_labels=_str_map(home.get("labels")) or default.home_entity_labels,
        sensitivity=_sensitivity_policy(data),
        # TODO(ADR 0004 fast-follow): 解析 [dimensions] 段。当前无 TOML 维度解析，所有
        # 文件加载路径都继承默认金融 5 维（与缺失回退的 _default_profile() 维度等价），
        # 是有意的临时态——避免出现"有标量字段却空维度"的不一致 profile。
        dimensions=default.dimensions,
    )
