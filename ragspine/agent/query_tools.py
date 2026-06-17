"""参数化查询工具层（function-calling）。

本期不接 LLM：只产出工具的 JSON schema（Anthropic / OpenAI 两种格式）
+ 本地确定性执行函数，供下期接入真实 LLM 时直接挂载。
执行函数先经 glossary 归一参数，再查 fact_metric：
命中→返回确定值 + 血缘；未命中→明确 not_found，绝不编造。
"""

from ragspine.common.company_profile import DomainProfile, load_company_profile
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
_REQUIRED = ["metric", "entity", "period"]


def _entity_examples(profile: DomainProfile) -> str:
    """从 profile 派生 entity 示例串：展示名 + 本地语言同义词（去掉受控代码本身）。

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
        "参数接受同义词与中文：metric 如 'REVENUE'/'营收'/'NEWSALES'/'新签金额'/'PROFIT'/'ROE'；"
        f"entity 如 {_entity_examples(profile)}；"
        "period 如 'FY2024'/'2024'/'2024H1'。查不到会明确返回 not_found，不会编造数字。"
    )


def _properties(profile: DomainProfile) -> dict[str, dict[str, str]]:
    return {
        "metric": {
            "type": "string",
            "description": "指标名，支持缩写/英文全称/中文同义词，如 REVENUE、营收、NEWSALES、PROFIT、ROE。",
        },
        "entity": {
            "type": "string",
            "description": f"实体名，支持中英文同义词，如 {_entity_examples(profile)}。",
        },
        "period": {
            "type": "string",
            "description": "期间，如 FY2024、2024、2024H1、2025Q1。",
        },
        "channel": {
            "type": "string",
            "description": "渠道，可选，默认 TOTAL（AGENCY/BANCA/TOTAL）。",
        },
    }


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
            "required": _REQUIRED,
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
                "required": _REQUIRED,
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
