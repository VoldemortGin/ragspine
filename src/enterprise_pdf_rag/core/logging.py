"""日志配置的唯一来源 + AI 产物的血缘与隐私纪律。

纪律:
- 库代码(src/ 内)只 `logging.getLogger(__name__)`,绝不在本模块之外配置日志。
- `setup_logging()` 由入口在启动时调用一次,幂等。
- trace 只记**码值 / 计数 / 耗时**,绝不落答案 / 原文 / 向量值等敏感载荷。
  禁记字段清单见 SENSITIVE_FIELDS。
- 每个 AI 产物带来源 + 产出它的实现/版本号(多实现可并存可审计)——用 log_provenance。
"""

import logging
from logging.config import dictConfig

from enterprise_pdf_rag.core.settings import get_settings

# 禁记字段:这些一律不进 log(隐私 + 体积)
SENSITIVE_FIELDS = frozenset({"answer", "text", "content", "prompt", "embedding", "value"})

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    level = "DEBUG" if get_settings().is_debug else "INFO"
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}
            },
            "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "default"}},
            "root": {"handlers": ["console"], "level": level},
        }
    )
    _configured = True


def log_provenance(source: str, impl: str, version: str, *, count: int | None = None) -> None:
    """记录 AI 产物的来源元数据(来源 / 实现 / 版本 / 计数),绝不记录载荷本身。"""
    logging.getLogger("provenance").info(
        "source=%s impl=%s version=%s count=%s", source, impl, version, count
    )
