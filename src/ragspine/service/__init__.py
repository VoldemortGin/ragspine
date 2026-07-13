"""service —— HTTP 服务层：ServiceConfig、FastAPI app、RQ 任务队列、FAQ 短路缓存。

FAQ 层位于反幻觉 guard 之前，必须保守排除：结构化数值/竞品/实时/过期/停用/
RESTRICTED 内容一律不短路。

Submodules:
    api/ — FastAPI app（app factory + 依赖注入 + 路由 + 边界 schema）。
    dify/ — Dify 编译产物的服务端安全执行层（L0 静态闸 + 受限 runner）。
    n8n_public/ — n8n 公共 REST API 形状克隆（/api/v1/* + /webhook/*）：文件存储层 + 路由层。
    faq/ — SME 审核型 FAQ 短路缓存。
    tasks/ — 异步任务队列抽象 + worker 端 ingestion job。
    config.py — 服务层运行时配置（env RAGSPINE_*）与资源/provider 装配。
    conversation.py — opt-in 多轮会话记忆（W6c）：跟进/指代；每轮重过 security gate + isolation。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
