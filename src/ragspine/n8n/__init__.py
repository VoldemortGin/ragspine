"""ragspine.n8n —— n8n workflow JSON ↔ Dify DSL 双向转换器。

把 n8n 导出的 workflow JSON 转成可直接喂给 ragspine.dify 编译器的 Dify DSL dict，
以及反向把 Dify DSL 转回可导入 n8n 的 workflow JSON。round-trip 无损机制：正向转换时
每个 dify 节点 data._n8n 保留原始 n8n 节点完整 dict，workflow 级其余键存顶层 x_n8n；
反向优先按 _n8n 还原，无 _n8n 才走映射表新建。无法语义映射处一律出 warning，绝不静默丢弃。

两段布局（镜像 dify 域）：

    parse/    dict / JSON / YAML 文本 → N8nWorkflow（pydantic v2 边界，extra='allow'；
              PyYAML 延迟 import；本域 pydantic 只出现在这一段）
    convert/  双向转换核心（mapping 映射表 / variables 表达式转换 / to_dify / to_n8n，纯 stdlib）

公开门面（api.py）：n8n_to_dify / dify_to_n8n / parse_n8n_workflow；域异常 N8nConvertError。
详见子包宪章 CLAUDE.md。

Submodules:
    parse/    — 边界装载 + pydantic 校验（loader.py / schema.py）。
    convert/  — 双向转换核心（mapping.py / variables.py / to_dify.py / to_n8n.py）。
    api.py    — 门面：n8n_to_dify / dify_to_n8n / parse_n8n_workflow。
    errors.py — 域统一异常（N8nConvertError，code="n8n.convert"）。
"""

import importlib

from ragspine import _lazy_submodules

_submodule_getattr, _submodule_dir = _lazy_submodules(__name__, __path__)

# 门面 API curated 暴露：`from ragspine.n8n import n8n_to_dify`。仍走惰性解析——
# `import ragspine.n8n` 不急切 import api.py（也就不急切拉起 parse 段的 pydantic）。
_CURATED: dict[str, str] = {
    "n8n_to_dify": "api",
    "dify_to_n8n": "api",
    "parse_n8n_workflow": "api",
    "N8nConvertError": "errors",
}

__all__ = list(_CURATED)


def __getattr__(name: str) -> object:
    module_name = _CURATED.get(name)
    if module_name is not None:
        module = importlib.import_module(f"{__name__}.{module_name}")
        return getattr(module, name)
    return _submodule_getattr(name)


def __dir__() -> list[str]:
    return sorted({*__all__, *_submodule_dir()})
