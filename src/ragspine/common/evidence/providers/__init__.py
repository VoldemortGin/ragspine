"""common.evidence.providers —— 证据链的模型访问:provider 环境、有界调用与本地模型(ADR 0022)。

Submodules:
    json_completion.py — 显式、有界的模型调用,严格 DTO 校验与不可变缓存。
    local_model_launcher.py — 把远程模型凭证注入一个白名单子进程环境。
    local_model_tunnel.py — 项目自有、只走回环的 SSH 模型隧道配置。
    local_models.py — 带鉴权的 OpenAI 兼容 embedding 与 rerank HTTP 适配器。
    providers.py — 显式 provider 环境与有界、opt-in 的连通性 smoke。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
