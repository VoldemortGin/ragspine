"""n8n 转换域的统一异常（继承家族 corespine.CorespineError，带稳定可 grep 的 code）。

parse（边界校验）与 convert（双向转换）两段共用这一个异常类型：非法输入、缺依赖、
引用不存在的节点等一律归一到 N8nConvertError，绝不让裸 pydantic/yaml 异常逃逸到调用方。
"""

from __future__ import annotations

from corespine import CorespineError


class N8nConvertError(CorespineError):
    """n8n workflow ↔ Dify DSL 转换期错误的统一异常（parse / convert 通用）。"""

    code = "n8n.convert"
