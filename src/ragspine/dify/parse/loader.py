"""parse 段入口：Dify `.yml` 文本/路径 → 校验过的 DifyDoc。

`yaml.safe_load`（PyYAML 延迟 import，[dify] extra）把 DSL 读成裸 dict，再经 pydantic 边界模型
（schema.py）校验形状，最后判 app.mode 是否在已支持集合（不在 → UnsupportedAppMode）。

非法输入一律归一到域异常（DifyCompileError 系），绝不让裸 yaml/pydantic 异常逃逸到调用方：
- YAML 语法错 / 顶层不是映射 → DifyCompileError
- 缺 app / app.mode → DifyCompileError
- mode 合法但不在支持集合 → UnsupportedAppMode
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ragspine.dify.errors import DifyCompileError, UnsupportedAppMode
from ragspine.dify.parse.schema import SUPPORTED_APP_MODES, DifyDoc


def _load_yaml(text: str) -> dict[str, Any]:
    """yaml.safe_load 文本为 dict（PyYAML 延迟 import）；非映射或语法错 → DifyCompileError。"""
    try:
        import yaml
    except ImportError as exc:  # 未装 [dify] extra
        raise DifyCompileError(
            "未安装 PyYAML：pip install 'rag-spine[dify]' 或 pip install PyYAML 后重试。",
            code="dify.missing_dependency",
        ) from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DifyCompileError(f"YAML 解析失败：{exc}") from exc

    if data is None:
        raise DifyCompileError("YAML 内容为空。")
    if not isinstance(data, dict):
        raise DifyCompileError(
            f"Dify DSL 顶层必须是映射（mapping），实际为 {type(data).__name__}。"
        )
    return data


def parse_dify_yaml(source: str | Path) -> DifyDoc:
    """把 Dify DSL（YAML 文本或 .yml 文件路径）解析并校验为 DifyDoc。

    入参 source 既可是 YAML 文本字符串，也可是指向 `.yml` 文件的 str/Path：
    凡能作为【存在的文件】打开的，按文件读；否则当作 YAML 文本直接解析。
    """
    text = _read_source(source)
    data = _load_yaml(text)

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


def _read_source(source: str | Path) -> str:
    """把 source 归一为 YAML 文本：存在的文件则读其内容，否则视作 YAML 文本本身。"""
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8")
    # str：可能是路径，也可能是 YAML 文本。仅当它是一个【存在的文件】时按文件读。
    try:
        candidate = Path(source)
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except OSError:
        # 过长/含非法字符的字符串当作 YAML 文本，不报路径错。
        pass
    return source
