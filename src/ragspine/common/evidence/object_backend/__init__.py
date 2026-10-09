"""common.evidence.object_backend —— store 持久化原语的后端缝(sqlite 对象库 PR-1)。

Submodules:
    protocol.py — ``ObjectBackend`` / ``ModelCacheBackend`` Protocol、数据类与异常。
    files.py — 文件布局实现(现有三处写路径原样搬入,字节不变)。
    sqlite.py — sqlite 实现(每 store 根一个 db;读穿旧文件布局)。
    probe.py — 目录上 sqlite 可用性的真实探测(结果缓存;失败码不含路径)。
    lease.py — O_EXCL 写者租约(ADR 0023 的持有者 JSON + 租约 + 接管代次,通用化)。
    registry.py — ``open_backend(root, kind)``:读设置 → 探测 → 返回实现。
    staged.py — opt-in 分阶段后端(ADR 0044):本地盘 sqlite 工作副本 + 阶段结束整文件发布。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
