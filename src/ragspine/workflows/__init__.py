"""Natural-language workflow scaffolding over a curated Dify DSL catalog.

The domain is deliberately thin: it selects a bundled, Spine-authored Dify
workflow or renders a constrained ``start -> llm -> end`` fallback.  It does
not execute workflows, fetch remote templates, or introduce another graph DSL.

Submodules:
    catalog.py — 内置 Dify 模板目录装载、校验与按 ID 查询。
    errors.py — 工作流域统一异常。
    formats.py — JSON/YAML/TOML 安全解析与规范化导出。
    generated_catalog.py — 生成型模板 taxonomy、描述符与 release-time 目录构造。
    matching.py — 词法与可选 embedding 模板匹配器。
    model.py — 模板、来源、兼容性与脚手架结果值类型。
    planner.py — 安全的通用 Dify 工作流后备生成器。
    preview.py — 版本化、隐私最小化的只读流程图预览契约与生成器。
    readiness.py — 工作流格式、编译与 L0 可运行性预检。
    scaffold.py — 模板复用与后备生成的脚手架编排门面。
    source_policy.py — 外部模板来源、许可证与重写策略的发布期安全门。
    templates/ — 内置目录元数据与 Spine 原创 Dify DSL 模板资源。
"""

import importlib

from ragspine import _lazy_submodules

_submodule_getattr, _submodule_dir = _lazy_submodules(__name__, __path__)

_CURATED: dict[str, str] = {
    "WorkflowCatalog": "catalog",
    "load_builtin_catalog": "catalog",
    "clear_builtin_catalog_cache": "catalog",
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
    "PREVIEW_SCHEMA_VERSION": "preview",
    "WorkflowPreview": "preview",
    "WorkflowPreviewNode": "preview",
    "WorkflowPreviewEdge": "preview",
    "WorkflowPreviewError": "preview",
    "build_workflow_preview": "preview",
    "READINESS_SCHEMA_VERSION": "readiness",
    "WorkflowReadiness": "readiness",
    "check_workflow": "readiness",
    "check_workflow_document": "readiness",
    "WorkflowPackage": "packaging",
    "package_workflow_document": "packaging",
    "package_workflow_readiness": "packaging",
    "workflow_package_zip": "packaging",
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
