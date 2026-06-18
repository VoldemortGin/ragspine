"""service —— HTTP 服务层：ServiceConfig、FastAPI app、RQ 任务队列、FAQ 短路缓存。

FAQ 层位于反幻觉 guard 之前，必须保守排除：结构化数值/竞品/实时/过期/停用/
RESTRICTED 内容一律不短路。

Submodules:
    api/ — FastAPI app（app factory + 依赖注入 + 路由 + 边界 schema）。
    faq/ — SME 审核型 FAQ 短路缓存。
    tasks/ — 异步任务队列抽象 + worker 端 ingestion job。
    config.py — 服务层运行时配置（env RAGSPINE_*）与资源/provider 装配。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
