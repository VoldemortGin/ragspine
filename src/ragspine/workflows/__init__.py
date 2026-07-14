"""Natural-language workflow scaffolding over a curated Dify DSL catalog.

The domain is deliberately thin: it selects a bundled, Spine-authored Dify
workflow or renders a constrained ``start -> llm -> end`` fallback.  It does
not execute workflows, fetch remote templates, or introduce another graph DSL.

Submodules:
    catalog.py — 内置 Dify 模板目录装载、校验与按 ID 查询。
    errors.py — 工作流域统一异常。
    formats.py — JSON/YAML/TOML 安全解析与规范化导出。
    matching.py — 词法与可选 embedding 模板匹配器。
    model.py — 模板、来源、兼容性与脚手架结果值类型。
    planner.py — 安全的通用 Dify 工作流后备生成器。
    scaffold.py — 模板复用与后备生成的脚手架编排门面。
    templates/ — 内置目录元数据与 Spine 原创 Dify DSL 模板资源。
"""

import importlib

from ragspine import _lazy_submodules

_submodule_getattr, _submodule_dir = _lazy_submodules(__name__, __path__)

_CURATED: dict[str, str] = {
    "WorkflowCatalog": "catalog",
    "load_builtin_catalog": "catalog",
    "WorkflowTemplate": "model",
    "WorkflowSource": "model",
    "WorkflowCompatibility": "model",
    "WorkflowRequirement": "model",
    "TemplateMatch": "model",
    "ScaffoldResult": "model",
    "TemplateMatcher": "matching",
    "LexicalTemplateMatcher": "matching",
    "EmbeddingTemplateMatcher": "matching",
    "make_template_matcher": "matching",
    "scaffold_workflow": "scaffold",
    "WorkflowError": "errors",
    "WorkflowCatalogError": "errors",
    "WorkflowTemplateNotFoundError": "errors",
    "WorkflowInputError": "errors",
    "WorkflowMatcherError": "errors",
    "WorkflowFormatError": "errors",
    "parse_workflow": "formats",
    "load_workflow": "formats",
    "dump_json": "formats",
    "dump_dify_yaml": "formats",
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
