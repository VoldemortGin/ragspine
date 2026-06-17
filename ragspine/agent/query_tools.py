"""参数化查询工具层（function-calling）。

本期不接 LLM：只产出工具的 JSON schema（Anthropic / OpenAI 两种格式）
+ 本地确定性执行函数，供下期接入真实 LLM 时直接挂载。
执行函数先经 glossary 归一参数，再查 fact_metric：
命中→返回确定值 + 血缘；未命中→明确 not_found，绝不编造。
"""

from ragspine.common.company_profile import (
    DimensionSpec,
    DomainProfile,
    _default_profile,
    load_company_profile,
)
from ragspine.common.glossary import (
    geography_for_entity,
    normalize_entity,
    normalize_metric,
    normalize_period,
)
from ragspine.storage.fact_store import FactStore

# home 公司 profile（tool schema 里的实体示例由此派生，不硬编码 "ACME"）。
_PROFILE = load_company_profile()

_TOOL_NAME = "query_metric"

# 各维度属性 description 模板（按维度受控名取用）。示例文案随 profile 切换的部分由
# _dim_examples 派生；其余领域措辞作为模板留存，对默认金融 profile 字节级等价。
_METRIC_PROPERTY_DESC = (
    "指标名，支持缩写/英文全称/中文同义词，如 REVENUE、营收、NEWSALES、PROFIT、ROE。"
)
_PERIOD_PROPERTY_DESC = "期间，如 FY2024、2024、2024H1、2025Q1。"
_CHANNEL_PROPERTY_DESC = "渠道，可选，默认 TOTAL（AGENCY/BANCA/TOTAL）。"

# tool description 里的 metric 示例片段与反幻觉条款（默认 profile 逐字不变）。
_DESC_METRIC_EXAMPLES = "metric 如 'REVENUE'/'营收'/'NEWSALES'/'新签金额'/'PROFIT'/'ROE'"
_DESC_PERIOD_EXAMPLES = "period 如 'FY2024'/'2024'/'2024H1'"
_ANTI_FABRICATION_CLAUSE = "查不到会明确返回 not_found，不会编造数字。"


def _dim_examples(profile: DomainProfile) -> str:
    """从 profile 派生类目维（entity）示例串：展示名 + 本地语言同义词（去掉受控代码本身）。

    顺序稳定（labels 插入序的展示名在前，再补 synonyms 插入序的非代码别名），
    保证可测。ACME 默认下应得 'ACME Hong Kong'/'香港'/'ACME China'/… 等示例。
    """
    labels = profile.home_entity_labels
    examples: list[str] = list(labels.values())
    # 受控代码形式（如 'acme_group'）不作为示例；其余别名补入丰富召回。
    codes_lower = {code.lower() for code in labels}
    for alias in profile.home_entity_synonyms:
        if alias.lower() in codes_lower:
            continue
        if alias not in examples:
            examples.append(alias)
    return "、".join(examples)


def _tool_description(profile: DomainProfile) -> str:
    return (
        "查询单个财务/经营指标的确定值，返回数值、单位与数据血缘（源文件+定位）。"
        f"参数接受同义词与中文：{_DESC_METRIC_EXAMPLES}；"
        f"entity 如 {_dim_examples(profile)}；"
        f"{_DESC_PERIOD_EXAMPLES}。{_ANTI_FABRICATION_CLAUSE}"
    )


def _property_description(dim: DimensionSpec, profile: DomainProfile) -> str:
    """单个维度的属性 description：领域措辞取自模板，类目示例随 profile 切换。"""
    if dim.name == "metric":
        return _METRIC_PROPERTY_DESC
    if dim.name == "entity":
        return f"实体名，支持中英文同义词，如 {_dim_examples(profile)}。"
    if dim.name == "period":
        return _PERIOD_PROPERTY_DESC
    if dim.name == "channel":
        return _CHANNEL_PROPERTY_DESC
    return f"{dim.label}。"


def _declared_dims(profile: DomainProfile) -> list[DimensionSpec]:
    """调用期取当前 profile 的非派生维度（derived_from is None），保留声明顺序。

    派生维（如 geography ← entity）从 tool schema 中排除——与 _required 同一谓词，
    保证属性键与必填列表口径一致（默认下属性序 metric→entity→period→channel）。

    profile 未声明 dimensions 时（仅含标量字段的 profile，如 G2 运行期换公司用的精简
    fixture）回退默认金融维结构，使工具 schema 仍产出 metric/entity/period/channel——
    示例文案仍读当前 profile 的标量（home_entity_labels/synonyms），随 profile 切换。
    """
    dims = profile.dimensions or _default_profile().dimensions
    return [d for d in dims if d.derived_from is None]


def _properties(profile: DomainProfile) -> dict[str, dict[str, str]]:
    return {
        dim.name: {
            "type": "string",
            "description": _property_description(dim, profile),
        }
        for dim in _declared_dims(profile)
    }


def _required(profile: DomainProfile) -> list[str]:
    return [d.name for d in _declared_dims(profile) if d.required]


def build_query_metric_tool_anthropic(
    profile: DomainProfile | None = None,
) -> dict[str, object]:
    """按当前（或指定）profile 动态构造 Anthropic 格式 tool schema。

    实体示例随 profile 切换——换公司后 description 不再说硬编码的 ACME 实体名。
    profile 缺省时读模块级 _PROFILE（测试可 monkeypatch 它演示运行期换公司）。
    """
    prof = profile if profile is not None else _PROFILE
    return {
        "name": _TOOL_NAME,
        "description": _tool_description(prof),
        "input_schema": {
            "type": "object",
            "properties": _properties(prof),
            "required": _required(prof),
        },
    }


def build_query_metric_tool_openai(
    profile: DomainProfile | None = None,
) -> dict[str, object]:
    """按当前（或指定）profile 动态构造 OpenAI 格式 tool schema。"""
    prof = profile if profile is not None else _PROFILE
    return {
        "type": "function",
        "function": {
            "name": _TOOL_NAME,
            "description": _tool_description(prof),
            "parameters": {
                "type": "object",
                "properties": _properties(prof),
                "required": _required(prof),
            },
        },
    }


# 默认 profile 下预构造的 tool schema 常量（既有调用方按此名引用，行为不变）。
QUERY_METRIC_TOOL_ANTHROPIC = build_query_metric_tool_anthropic()
QUERY_METRIC_TOOL_OPENAI = build_query_metric_tool_openai()


def execute_query_metric(
    store: FactStore,
    metric: str,
    entity: str,
    period: str,
    channel: str = "TOTAL",
) -> dict[str, object]:
    """确定性执行 query_metric（对外签名冻结）。

    薄封装：把位置参数转交泛化的 execute_fact_query（按默认 profile 的声明维度执行）。
    三态语义、channel 直通、found-dict 形状对默认 profile 字节级一致。

    参数无法归一 -> {"status":"unrecognized_param","param":...,"raw":...}
    命中 -> {"status":"found","value",...,"source":{"doc","locator"}}
    未命中 -> {"status":"not_found","normalized":{...}}（绝不编造）
    """
    return execute_fact_query(
        store, _PROFILE, metric=metric, entity=entity, period=period, channel=channel
    )


def execute_fact_query(
    store: FactStore,
    profile: DomainProfile,
    **dims: str,
) -> dict[str, object]:
    """按 profile 的声明维度确定性执行事实查询。

    - 必填维（required 且 derived_from is None）按声明顺序逐个 normalize；任一落空 ->
      {"status":"unrecognized_param","param":<dim.name>,"raw":<原值>}（三态、顺序一致）。
    - 可选 channel 维：保持 (channel or "TOTAL").upper() 字面直通——不做同义词校验、不对
      "传了但不认识的值"静默回退 TOTAL（typo'd channel 查不到 -> not_found）；default 仅
      对缺省/空生效。
    - 派生维（如 geography ← entity）经 derivation 映射推导，不作必填输入。
    - store.query() 以钉死的位置签名调用；found-dict 仍把 period_type 与 period 作为两个
      分开的键返回（agent 两期差值比较 + _period_label 依赖），源 / 值 / 单位 / 受控代码
      等键对默认 profile 字节级一致。
    """
    declared = list(profile.dimensions) or list(_default_profile().dimensions)

    # 必填非派生维：按声明顺序逐个 normalize（按维名分派归一器），任一落空即 unrecognized
    # （三态、顺序与声明一致）。period 维归一返回 (period_type, period) 二元组。
    resolved: dict[str, str] = {}
    period_type = ""
    period_norm = ""
    for dim in declared:
        if not (dim.required and dim.derived_from is None):
            continue
        raw = dims.get(dim.name, "")
        if dim.name == "period":
            parsed_period = normalize_period(raw)
            if parsed_period is None:
                return {"status": "unrecognized_param", "param": dim.name, "raw": dims.get(dim.name)}
            period_type, period_norm = parsed_period
            continue
        normalizer = normalize_metric if dim.name == "metric" else normalize_entity
        code = normalizer(raw)
        if code is None:
            return {"status": "unrecognized_param", "param": dim.name, "raw": dims.get(dim.name)}
        resolved[dim.name] = code
    metric_code = resolved["metric"]
    entity_code = resolved["entity"]

    # 可选 channel：字面直通（present-but-unknown 不强转 TOTAL；default 仅对缺省/空生效）。
    channel_dim = next((d for d in declared if d.name == "channel"), None)
    channel_default = (channel_dim.default if channel_dim else None) or "TOTAL"
    chan = (dims.get("channel") or channel_default).upper()

    # 派生维（geography ← entity）：经 derivation 映射推导，不作必填输入。
    geography = geography_for_entity(entity_code)

    normalized = {
        "metric_code": metric_code,
        "entity": entity_code,
        "geography": geography,
        "period_type": period_type,
        "period": period_norm,
        "channel": chan,
    }

    results = store.query(metric_code, entity_code, period_type, period_norm, chan)
    if not results:
        return {"status": "not_found", "normalized": normalized}

    fact = results[0]
    return {
        "status": "found",
        "value": fact.value,
        "unit": fact.unit,
        "metric_code": fact.metric_code,
        "entity": fact.entity,
        "geography": fact.geography,
        "channel": fact.channel,
        "period_type": fact.period_type,
        "period": fact.period,
        "valid_as_of": fact.valid_as_of,
        "ingested_at": fact.ingested_at,
        "source": {"doc": fact.source_doc_id, "locator": fact.source_locator},
    }
