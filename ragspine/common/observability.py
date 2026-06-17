"""可观测性原语：request_id 生成 + 结构化 trace 发射。

约定（"日志按 Restricted 对待"）：
- emit_trace 只记【非敏感元数据】——request_id、route、受控代码（metric/entity/
  period/channel）、状态计数、分数、布尔标志、耗时、token 用量等；
- 绝不记原始答案正文、事实数值、chunk 正文——这些一旦进 INFO 日志即成为受限数据泄露面。

实现刻意极简：只用 stdlib logging/uuid，不自行 basicConfig 改全局配置，
由宿主进程/测试（caplog）挂 handler；字段以 LogRecord 属性（extra=...）承载。
"""

import logging
import uuid

# 全链路 trace 专用 logger（宿主配置 handler；默认不刷屏）
TRACE_LOGGER_NAME = "ragspine.trace"

_trace_logger = logging.getLogger(TRACE_LOGGER_NAME)


def new_request_id() -> str:
    """生成一次请求的关联键：uuid4 hex 短码（12 位，足够唯一且便于人读）。"""
    return uuid.uuid4().hex[:12]


def emit_trace(logger: logging.Logger | None = None, **fields) -> None:
    """以结构化方式记一条 INFO trace：字段经 extra 挂到 LogRecord 属性上。

    message 固定为 "trace"（不含任何敏感内容）；调用方只应传入非敏感元数据。
    """
    log = logger or _trace_logger
    log.info("trace", extra=fields)
