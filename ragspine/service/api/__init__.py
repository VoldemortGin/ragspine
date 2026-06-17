"""api —— FastAPI 应用：app factory、依赖注入、HTTP 路由、边界 schema。

路由是薄适配层（schema/DI/资源装配/FAQ 短路/错误整形/trace）；边界模型用
Pydantic v2，与内部 dataclass 解耦。

Submodules:
    app.py — FastAPI app factory：装配 config/provider/queue/faq_cache 到 app.state。
    dependencies.py — FastAPI dependency providers：从 app.state 读取已装配实例。
    routes.py — HTTP 路由：薄适配层（schema/DI/装配/FAQ 短路/错误整形/trace）。
    schemas.py — HTTP 边界模型（Pydantic v2），与内部 dataclass 解耦。
"""
