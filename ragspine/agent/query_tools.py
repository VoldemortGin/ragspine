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
    """确定性执行 query_metric。

    参数无法归一 -> {"status":"unrecognized_param","param":...,"raw":...}
    命中 -> {"status":"found","value",...,"source":{"doc","locator"}}
    未命中 -> {"status":"not_found","normalized":{...}}（绝不编造）
    """
    metric_code = normalize_metric(metric)
    if metric_code is None:
        return {"status": "unrecognized_param", "param": "metric", "raw": metric}

    entity_code = normalize_entity(entity)
    if entity_code is None:
        return {"status": "unrecognized_param", "param": "entity", "raw": entity}

    parsed_period = normalize_period(period)
    if parsed_period is None:
        return {"status": "unrecognized_param", "param": "period", "raw": period}
    period_type, period_norm = parsed_period

    chan = (channel or "TOTAL").upper()
    normalized = {
        "metric_code": metric_code,
        "entity": entity_code,
        "geography": geography_for_entity(entity_code),
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
