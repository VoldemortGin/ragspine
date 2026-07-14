"""parse 段入口：Dify YAML 正文/显式 Path → 校验过的 DifyDoc。

`workflows.formats.parse_workflow` 把正文解析成受大小/复杂度约束的裸 dict，再经 pydantic
边界模型（schema.py）校验形状，最后判 app.mode 是否在已支持集合。str 永远是正文；只有
显式 Path 才经 nofollow/fstat 的有界文件读取，HTTP 字符串不能探测服务端本地文件。

非法输入一律归一到域异常（DifyCompileError 系），绝不让裸 yaml/pydantic 异常逃逸到调用方：
- YAML 语法错 / 顶层不是映射 → DifyCompileError
- 缺 app / app.mode → DifyCompileError
- mode 合法但不在支持集合 → UnsupportedAppMode
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ragspine.dify.errors import DifyCompileError, UnsupportedAppMode
from ragspine.dify.parse.schema import SUPPORTED_APP_MODES, DifyDoc
from ragspine.workflows.errors import WorkflowFormatError


def _load_yaml(source: str | bytes) -> dict[str, object]:
    """用共享安全格式边界解析 YAML，并统一归一为 DifyCompileError。"""
    try:
        from ragspine.workflows.formats import parse_workflow
    except ImportError as exc:  # 未装 [dify] extra
        raise DifyCompileError(
            "未安装 PyYAML：pip install 'rag-spine[dify]' 或 pip install PyYAML 后重试。",
            code="dify.missing_dependency",
        ) from exc

    try:
        return parse_workflow(source, format="yaml")
    except WorkflowFormatError as exc:
        raise DifyCompileError(f"YAML 解析失败：{exc}") from exc


def parse_dify_yaml(source: str | Path) -> DifyDoc:
    """把 Dify DSL（YAML 正文或显式 Path）解析并校验为 DifyDoc。

    str 无条件按正文处理；只有 Path 才允许读取本地文件，避免服务请求把字符串伪装成
    服务端路径。Path 经共享的有界 nofollow/fstat 读取器，限制为 1 MiB。
    """
    data = _load_yaml(_read_source(source))

    try:
        doc = DifyDoc.model_validate(data)
    except ValidationError as exc:
        # pydantic 校验失败（缺 app / app.mode 空等）归一到域异常，不外泄 pydantic 类型。
        raise DifyCompileError(f"Dify DSL 校验失败：{exc}") from exc

    if doc.mode not in SUPPORTED_APP_MODES:
        supported = "、".join(sorted(SUPPORTED_APP_MODES))
        raise UnsupportedAppMode(
            f"暂不支持 app.mode='{doc.mode}'（当前支持：{supported}）。",
            mode=doc.mode,
        )
    return doc


def _read_source(source: str | Path) -> str | bytes:
    """str 原样返回；显式 Path 才经共享安全读取器读取。"""
    if isinstance(source, str):
        return source
    try:
        from ragspine.workflows.formats import MAX_WORKFLOW_BYTES, read_bounded_file
    except ImportError as exc:  # 未装 [dify] extra
        raise DifyCompileError(
            "未安装 PyYAML：pip install 'rag-spine[dify]' 或 pip install PyYAML 后重试。",
            code="dify.missing_dependency",
        ) from exc
    try:
        return read_bounded_file(
            source,
            limit=MAX_WORKFLOW_BYTES,
            label=str(source),
        )
    except WorkflowFormatError as exc:
        raise DifyCompileError(f"Dify DSL 文件读取失败：{exc}") from exc
