"""门面：n8n workflow JSON ↔ Dify DSL 双向转换的一站式入口。

公开入口：
- n8n_to_dify(source) -> (dify dict, warnings)
- dify_to_n8n(source) -> (n8n dict, warnings)
低层入口：parse_n8n_workflow（parse 段边界模型）。

str 入参按 JSON/YAML 自动识别（先 json.loads 再 yaml.safe_load，均延迟处理）。
转换失败一律抛 N8nConvertError（带稳定 code），绝不泄漏裸 pydantic/yaml 异常。
无法语义映射处一律进 warnings，绝不静默丢弃。
"""

from __future__ import annotations

from typing import Any

from ragspine.n8n.convert.to_dify import convert_to_dify
from ragspine.n8n.convert.to_n8n import convert_to_n8n
from ragspine.n8n.parse.loader import load_dify_document, parse_n8n_workflow

__all__ = ["n8n_to_dify", "dify_to_n8n", "parse_n8n_workflow"]


def n8n_to_dify(source: dict[str, Any] | str) -> tuple[dict[str, Any], list[str]]:
    """n8n workflow（dict，或 JSON/YAML 文本）→ Dify DSL dict。

    返回 (dify dict, warnings)。产物可直接 yaml.safe_dump 后喂给
    ragspine.dify.parse_dify_yaml / lower_to_ir。原始 n8n 节点完整保留在各 dify
    节点 data._n8n（round-trip 无损机制），workflow 级其余键存顶层 x_n8n。
    """
    workflow = parse_n8n_workflow(source)
    return convert_to_dify(workflow)


def dify_to_n8n(source: dict[str, Any] | str) -> tuple[dict[str, Any], list[str]]:
    """Dify DSL（dict，或 YAML/JSON 文本）→ n8n workflow dict。

    返回 (n8n dict, warnings)。带 data._n8n 的节点无损还原；无 _n8n 的按映射表新建；
    合成 start/end（_n8n.synthetic）直接剔除。
    """
    document = load_dify_document(source)
    return convert_to_n8n(document)
