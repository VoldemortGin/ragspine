"""Dify DSL 的 pydantic v2 边界模型（解析期唯一的 pydantic 出现处）。

只做【边界校验 + 形状归一】，不含任何业务/IR 概念：把 yaml.safe_load 出的裸 dict 校验成
结构化的 DifyDoc，保证 `app.mode` 合法、`workflow.graph.{nodes,edges}` 存在且形状正确。

刻意宽松（`extra='allow'`）：Dify 版本演进会给节点/边/app 加字段，我们【不】因未知字段而脆断——
未建模的字段原样保留在 `.model_extra` / 透传，由 IR 段按需取用（如各节点 data 的具体配置）。
故每个节点的 `data` 不在此处强类型化，统一收成 `dict`，节点语义留到 IR 段按 `type` 分派。

domain-neutral 边界：这里只认 Dify 的 wire 形状，不认任何 ragspine 业务概念。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 已支持的 app 模式（ADR 0013 默认 5）：workflow + advanced-chat（含 answer 节点）。
# chat / agent-chat / completion 等留待后续；非此集合在 loader 段抛 UnsupportedAppMode。
SUPPORTED_APP_MODES: frozenset[str] = frozenset({"workflow", "advanced-chat"})


class _Lenient(BaseModel):
    """宽松基类：允许未知字段（Dify 加字段不脆断），未知字段保留在 model_extra。"""

    model_config = ConfigDict(extra="allow")


class DifyNode(_Lenient):
    """图中的一个节点：id + data（data.type 决定节点语义，data 内字段留到 IR 段取用）。

    data 刻意收成裸 dict 而非强类型——节点种类繁多且各自字段差异大，强类型化会与
    `extra='allow'` 的「不因 Dify 演进脆断」目标冲突。节点语义在 IR 段按 data['type'] 分派。
    """

    id: str
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def node_type(self) -> str:
        """节点类型（data.type）；缺失时返回空串，由 IR 段判为未知类型处理。"""
        value = self.data.get("type", "")
        return str(value) if value is not None else ""


class DifyEdge(_Lenient):
    """图中的一条有向边：source → target，可带 sourceHandle（if-else 的 true/false 分支等）。"""

    source: str
    target: str
    # if-else / question-classifier 的分支出边用 sourceHandle 区分（"true"/"false"/case id）。
    source_handle: str | None = Field(default=None, alias="sourceHandle")

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class DifyGraph(_Lenient):
    """工作流图：节点表 + 边表。"""

    nodes: list[DifyNode] = Field(default_factory=list)
    edges: list[DifyEdge] = Field(default_factory=list)


class DifyWorkflow(_Lenient):
    """workflow 段：承载 graph，以及（advanced-chat 时）conversation/环境变量等（暂透传）。"""

    graph: DifyGraph = Field(default_factory=DifyGraph)


class DifyApp(_Lenient):
    """app 元信息：mode（被校验）+ name/description 等（透传）。"""

    mode: str
    name: str = ""

    @field_validator("mode")
    @classmethod
    def _mode_nonempty(cls, value: str) -> str:
        """mode 必须非空；具体是否在支持集合由 loader 段判（以抛 UnsupportedAppMode）。"""
        if not value or not value.strip():
            raise ValueError("app.mode 不能为空")
        return value


class DifyDoc(_Lenient):
    """一个 Dify 应用导出 DSL 的根：app（mode）+ workflow（graph）。

    顶层其它键（kind / version / dependencies 等）经 extra='allow' 透传保留，不强校验。
    """

    app: DifyApp
    workflow: DifyWorkflow = Field(default_factory=DifyWorkflow)

    @property
    def mode(self) -> str:
        """便捷取 app.mode。"""
        return self.app.mode

    @property
    def nodes(self) -> list[DifyNode]:
        """便捷取节点表。"""
        return self.workflow.graph.nodes

    @property
    def edges(self) -> list[DifyEdge]:
        """便捷取边表。"""
        return self.workflow.graph.edges
