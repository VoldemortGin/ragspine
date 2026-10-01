"""兼容入口:实现已迁到 ``ragspine.common.evidence.configs``,这里只 re-export 同一批对象。"""

from ragspine.common.evidence.configs import (
    DATA_DIR,
    LOG_DIR,
    ROOT_DIR,
    Settings,
    get_settings,
    resource_path,
    settings,
)

__all__ = [
    "DATA_DIR",
    "LOG_DIR",
    "ROOT_DIR",
    "Settings",
    "get_settings",
    "resource_path",
    "settings",
]
