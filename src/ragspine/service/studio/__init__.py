"""service/studio —— Studio 前端的 launch-session 域：CLI serve 与前端自动加载之间的桥。

`ragspine workflow serve` 把选中的工作流登记为一次 launch session，前端用 URL 里的
不透明 token 经只读端点取回 YAML。与 wheel 内静态资源目录 studio_dist 无关。

Submodules:
    launch.py — 内存态、有界、线程安全的 LaunchSession 注册表（不透明 token → 工作流 YAML）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
