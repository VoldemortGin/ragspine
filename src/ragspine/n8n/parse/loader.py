"""parse 段入口：n8n workflow（dict / JSON / YAML 文本）→ 校验过的 N8nWorkflow。

str 入参先试 `json.loads`（stdlib，n8n 导出即 JSON），失败再试 `yaml.safe_load`
（PyYAML 延迟 import，[dify] extra 带入）。非法输入一律归一到域异常 N8nConvertError，
绝不让裸 json/yaml/pydantic 异常逃逸到调用方：

- JSON/YAML 都解析不动 / 顶层不是映射 → N8nConvertError
- 缺 nodes / 节点缺 name/type → N8nConvertError
- connections 引用了不存在的节点 name → N8nConvertError

另提供 load_dify_document：dify_to_n8n 的入参侧边界（str→dict + DifyDoc 只读校验），
让 pydantic 的 ValidationError 捕获也收在 parse 段（镜像 dify 域「pydantic 只在 parse 段」纪律）。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from ragspine.dify.parse.schema import DifyDoc
from ragspine.n8n.errors import N8nConvertError
from ragspine.n8n.parse.schema import N8nNode, N8nWorkflow


def parse_n8n_workflow(source: dict[str, Any] | str) -> N8nWorkflow:
    """把 n8n workflow（dict，或 JSON/YAML 文本）解析并校验为 N8nWorkflow。"""
    data = _load_text(source) if isinstance(source, str) else source
    if "nodes" not in data:
        raise N8nConvertError("n8n workflow 缺少 nodes 字段。")
    try:
        workflow = N8nWorkflow.model_validate(data)
    except ValidationError as exc:
        # pydantic 校验失败归一到域异常，不外泄 pydantic 类型。
        raise N8nConvertError(f"n8n workflow 校验失败：{exc}") from exc
    _validate_connections(workflow)
    return workflow


def load_dify_document(source: dict[str, Any] | str) -> dict[str, Any]:
    """把 Dify DSL（dict，或 YAML/JSON 文本）归一为【校验过的】裸 dict。

    只读复用 ragspine.dify.parse.schema.DifyDoc 做形状校验（app/workflow.graph），
    返回原始 dict 供 convert 段使用（转换需要 pydantic 之外的全部原样键）。
    """
    data = _load_text(source) if isinstance(source, str) else source
    try:
        DifyDoc.model_validate(data)
    except ValidationError as exc:
        raise N8nConvertError(f"Dify DSL 校验失败：{exc}") from exc
    return data


def _load_text(text: str) -> dict[str, Any]:
    """文本 → dict：先试 JSON，失败再试 YAML；顶层非映射 → N8nConvertError。"""
    loaded: Any
    try:
        loaded = json.loads(text)
    except ValueError:
        loaded = _load_yaml(text)
    if not isinstance(loaded, dict):
        raise N8nConvertError(
            f"workflow 顶层必须是映射（mapping），实际为 {type(loaded).__name__}。"
        )
    return loaded


def _load_yaml(text: str) -> Any:
    """yaml.safe_load 文本（PyYAML 延迟 import）；语法错/空内容 → N8nConvertError。"""
    try:
        import yaml
    except ImportError as exc:  # 未装 PyYAML（[dify] extra）
        raise N8nConvertError(
            "输入不是合法 JSON，且未安装 PyYAML 无法按 YAML 解析："
            "pip install 'rag-spine[dify]' 或 pip install PyYAML 后重试。",
            code="n8n.missing_dependency",
        ) from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise N8nConvertError(f"输入既不是合法 JSON 也不是合法 YAML：{exc}") from exc
    if data is None:
        raise N8nConvertError("输入内容为空。")
    return data


def _validate_connections(workflow: N8nWorkflow) -> None:
    """校验 connections 引用的节点 name 都存在、形状合法（容忍 null 空端口）。"""
    names = {node.name for node in workflow.nodes}
    _ensure_unique_names(workflow.nodes)
    for source_name, conn_types in workflow.connections.items():
        if source_name not in names:
            raise N8nConvertError(
                f"connections 引用了不存在的节点 {source_name!r}。", node=source_name
            )
        if not isinstance(conn_types, dict):
            raise N8nConvertError(
                f"connections[{source_name!r}] 必须是映射（连接类型 → 端口数组）。"
            )
        for conn_type, ports in conn_types.items():
            if not isinstance(ports, list):
                raise N8nConvertError(
                    f"connections[{source_name!r}][{conn_type!r}] 必须是数组。"
                )
            for port in ports:
                if port is None:
                    continue  # n8n 导出里的空端口
                if not isinstance(port, list):
                    raise N8nConvertError(
                        f"connections[{source_name!r}][{conn_type!r}] 的端口必须是数组。"
                    )
                for target in port:
                    if not isinstance(target, dict) or "node" not in target:
                        raise N8nConvertError(
                            f"connections[{source_name!r}][{conn_type!r}] 的目标必须带 node 键。"
                        )
                    if target["node"] not in names:
                        raise N8nConvertError(
                            f"connections 引用了不存在的节点 {target['node']!r}。",
                            node=str(target["node"]),
                        )


def _ensure_unique_names(nodes: list[N8nNode]) -> None:
    """n8n 以 name 为图内主键：重名会让 connections 歧义，直接拒绝。"""
    seen: set[str] = set()
    for node in nodes:
        if node.name in seen:
            raise N8nConvertError(f"节点 name 重复：{node.name!r}。", node=node.name)
        seen.add(node.name)
