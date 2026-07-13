"""n8n 公共 REST API 形状克隆（/api/v1/* + /webhook/*）：文件存储层 + 路由层。

Submodules:
    store.py — n8n workflow / execution 文件存储（纯存储层，无 HTTP 概念）。
    router.py — n8n 公共 REST API 形状克隆路由：n8n 客户端/脚本零改动直连本服务。
"""
