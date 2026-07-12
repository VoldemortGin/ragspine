"""变量表达式双向转换（规则集中一处；转不动一律原样保留 + warning，绝不静默丢弃）。

n8n 侧：值以 "=" 开头才是表达式，内嵌若干 `{{ … }}` token。可识别的引用模式：

    {{ $json.field }} / {{ $json["field"] }}            → 需唯一主链上游节点
    {{ $node["Name"].json["field"] }} / …json.field     → 按 name 查节点
    {{ $('Name').item.json.field }} / $('Name').first() → 同上

dify 侧：`{{#node_id.field#}}`。输出字段换算：n8n agent 的 "output" ↔ dify llm 的 "text"
（llm_node_ids 给出「由 agent 映射来的 llm」节点 id 集合）。

全有或全无：任一 token 转不动 → 整个值原样保留（含 "=" 前缀）+ 每个坏 token 一条 warning，
避免产出半生不熟的混合表达式。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping

from ragspine.n8n.convert.mapping import DIFY_LLM_OUTPUT_FIELD, N8N_AGENT_OUTPUT_FIELD

# n8n 表达式里的 {{ … }} token（内容任意，惰性匹配）。
_EXPR_TOKEN = re.compile(r"\{\{(.+?)\}\}", re.DOTALL)
# dify 模板里的 {{#nodeId.field#}} 引用（与 dify/ir/lower.py 的 _TEMPLATE_REF 同形）。
_DIFY_REF = re.compile(r"\{\{#\s*([^#}]+?)\s*#\}\}")

# 取字段的两种写法：.field 或 ['field'] / ["field"]。
_FIELD = r"""(?:\.([A-Za-z_][\w]*)|\[\s*(?:'([^']*)'|"([^"]*)")\s*\])"""
_JSON_REF = re.compile(r"\s*\$json" + _FIELD + r"\s*")
_NODE_REF = re.compile(
    r"""\s*\$node\[\s*(?:'([^']*)'|"([^"]*)")\s*\]\.json""" + _FIELD + r"\s*"
)
_CALL_REF = re.compile(
    r"""\s*\$\(\s*(?:'([^']*)'|"([^"]*)")\s*\)\.(?:item|first\(\))\.json""" + _FIELD + r"\s*"
)

# {{#nodeId#}} 缺 field 时的兜底字段（与 dify/ir/lower.py 的 _ref_from_token 一致）。
DEFAULT_FIELD = "output"


def n8n_value_to_dify(
    value: object,
    *,
    upstream_id: str | None,
    name_to_id: Mapping[str, str],
    llm_node_ids: Collection[str] = (),
) -> tuple[object, list[str]]:
    """n8n 参数值 → dify 文本。非表达式（不以 "=" 开头/非 str）原样返回。

    返回 (转换结果, warnings)。任一 token 转不动 → 返回原值 + warning（全有或全无）。
    """
    if not isinstance(value, str) or not value.startswith("="):
        return value, []
    body = value[1:]
    warnings: list[str] = []
    failed = False

    def _sub(match: re.Match[str]) -> str:
        nonlocal failed
        token = match.group(1)
        ref = _token_to_ref(token, upstream_id=upstream_id, name_to_id=name_to_id)
        if ref is None:
            failed = True
            warnings.append(f"变量表达式无法转换，原样保留：{{{{{token}}}}}")
            return match.group(0)
        node_id, field = ref
        if node_id in llm_node_ids and field == N8N_AGENT_OUTPUT_FIELD:
            field = DIFY_LLM_OUTPUT_FIELD  # agent 输出字段换算：output → text
        return "{{#" + node_id + "." + field + "#}}"

    converted = _EXPR_TOKEN.sub(_sub, body)
    if failed:
        return value, warnings
    return converted, warnings


def n8n_expr_to_selector(
    value: object,
    *,
    upstream_id: str | None,
    name_to_id: Mapping[str, str],
    llm_node_ids: Collection[str] = (),
) -> list[str] | None:
    """n8n 表达式 → dify value_selector（[node_id, field]）；解析不动 → None。

    仅当整个值恰是【单一】`{{ … }}` 引用（if 的 leftValue、set 的单引用赋值）才给 selector。
    """
    if not isinstance(value, str) or not value.startswith("="):
        return None
    match = re.fullmatch(r"\s*\{\{(.+?)\}\}\s*", value[1:], re.DOTALL)
    if match is None:
        return None
    ref = _token_to_ref(match.group(1), upstream_id=upstream_id, name_to_id=name_to_id)
    if ref is None:
        return None
    node_id, field = ref
    if node_id in llm_node_ids and field == N8N_AGENT_OUTPUT_FIELD:
        field = DIFY_LLM_OUTPUT_FIELD
    return [node_id, field]


def dify_text_to_n8n(
    text: str,
    *,
    id_to_name: Mapping[str, str],
    llm_node_ids: Collection[str] = (),
) -> tuple[str, list[str]]:
    """dify 文本 → n8n 表达式。`{{#id.field#}}` → `{{ $node["Name"].json["field"] }}`。

    id 查不到 → 该 token 原样保留 + warning。结果含表达式（`{{`）时整值前缀 "="。
    """
    warnings: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        node_id, _, field = token.partition(".")
        if not field:
            field = DEFAULT_FIELD
        name = id_to_name.get(node_id)
        if name is None:
            warnings.append(
                f"变量引用 {{{{#{token}#}}}} 的节点 id {node_id!r} 不在图中，原样保留"
            )
            return match.group(0)
        if node_id in llm_node_ids and field == DIFY_LLM_OUTPUT_FIELD:
            field = N8N_AGENT_OUTPUT_FIELD  # llm 输出字段换算：text → output
        return '{{ $node["' + name + '"].json["' + field + '"] }}'

    converted = _DIFY_REF.sub(_sub, text)
    if "{{" in converted:
        converted = "=" + converted
    return converted, warnings


def _token_to_ref(
    token: str, *, upstream_id: str | None, name_to_id: Mapping[str, str]
) -> tuple[str, str] | None:
    """单个 `{{ … }}` token 内容 → (node_id, field)；不可识别/查不到 → None。"""
    match = _JSON_REF.fullmatch(token)
    if match is not None:
        if upstream_id is None:
            return None  # $json 需要唯一主链上游
        return upstream_id, _picked(match.groups())
    match = _NODE_REF.fullmatch(token) or _CALL_REF.fullmatch(token)
    if match is not None:
        groups = [g for g in match.groups() if g is not None]
        if len(groups) != 2:
            return None
        name, field = groups
        node_id = name_to_id.get(name)
        if node_id is None:
            return None
        return node_id, field
    return None


def _picked(groups: tuple[str | None, ...]) -> str:
    """从互斥的备选捕获组里取第一个非 None 的值。"""
    for group in groups:
        if group is not None:
            return group
    return ""
