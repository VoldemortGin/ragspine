"""pipeline —— 管线拓扑导出：从真实装配派生的静态 PipelineGraph（Mermaid/DOT/JSON）。

「框架自由」的具象反证：不靠 Dify/LangGraph 的可视化画布，而是从【真实接线】生成图——
graph.py 提供零依赖的图值类型（Node/Edge/PipelineGraph）与三种导出器；topology.py 提供三个
duck-typed 构建器（agent/retriever/service），按是否注入向量/multi-query/叙事检索如实出图。

本包是叶子级关注点（screaming-architecture）：模块层【不】import 任何 agent/retrieval/service
编排器，一切内省靠 duck-typing，所有 Node.symbol 仅为 dotted-path 字符串（由漂移守护测试解析）。

Submodules:
    graph.py — 图值类型（frozen Node/Edge/PipelineGraph）+ Mermaid/DOT/JSON 导出器。
    topology.py — agent/retriever/service 三个拓扑构建器（duck-typed，反映真实装配）。
"""

from ragspine.pipeline.graph import Edge, Node, PipelineGraph
from ragspine.pipeline.topology import (
    agent_topology,
    retriever_topology,
    service_topology,
)

__all__ = [
    "Edge",
    "Node",
    "PipelineGraph",
    "agent_topology",
    "retriever_topology",
    "service_topology",
]
