"""n8n workflow JSON 的 pydantic v2 边界模型（本域唯一的 pydantic 出现处）。

只做【边界校验 + 形状归一】：把 json.loads / yaml.safe_load 出的裸 dict 校验成结构化的
N8nWorkflow，保证 nodes 存在且每个节点带 name/type。刻意宽松（`extra='allow'`）：n8n 版本
演进会给节点/工作流加字段，未建模的键原样保留在 model_extra，round-trip 时经
`model_dump(by_alias=True, exclude_unset=True)` 无损还原。

connections 收成裸 dict（形状差异大：main / ai_languageModel 等连接类型、端口二维数组、
null 空端口），结构校验留在 loader 段做（引用不存在的节点 name → N8nConvertError）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Lenient(BaseModel):
    """宽松基类：允许未知字段（n8n 加字段不脆断），未知字段保留在 model_extra。"""

    model_config = ConfigDict(extra="allow")


class N8nNode(_Lenient):
    """一个 n8n 节点：name（图内唯一显示名，connections 以它为键）+ type + parameters。"""

    name: str
    type: str
    id: str = ""
    type_version: int | float = Field(default=1, alias="typeVersion")
    position: list[int | float] | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def to_raw(self) -> dict[str, Any]:
        """还原为原始 n8n 节点 dict（含未建模键，仅含输入里出现过的字段）。"""
        return self.model_dump(by_alias=True, exclude_unset=True)


class N8nWorkflow(_Lenient):
    """一个 n8n workflow 导出 JSON 的根：name + nodes + connections。

    settings / pinData / meta / id / versionId 等其余键经 extra='allow' 透传保留在
    model_extra，转换时整体存进 dify DSL 顶层 `x_n8n`（round-trip 用）。
    """

    name: str = ""
    nodes: list[N8nNode]
    connections: dict[str, Any] = Field(default_factory=dict)
