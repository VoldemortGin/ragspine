"""可观测性原语：request_id 生成 + 结构化 trace 发射。

隐私约定（"日志按 Restricted 对待"）由 corespine 强制兜底：
- emit_trace 只记【非敏感元数据】——request_id、route、受控代码（metric/entity/
  period/channel）、状态计数、分数、布尔标志、耗时、token 用量等；
- 绝不记原始答案正文、事实数值、chunk 正文——这些一旦进 INFO 日志即成为受限数据泄露面。

强制机制：每条 trace 先过 corespine 的 InProcessPrivacyTraceSink，载荷若含禁词键
（answer / value / text / content / prompt / completion / chunk / chunk_text / body，
大小写不敏感、精确键名匹配）会【直接抛 TraceError】、绝不写日志——隐私 by construction，
而非靠 reviewer 自觉。通过校验后再以 stdlib logging 落盘（字段经 extra 挂到 LogRecord
属性，供 caplog/宿主消费）。

实现刻意极简：只用 stdlib logging/uuid + corespine sink，不自行 basicConfig 改全局配置，
由宿主进程/测试（caplog）挂 handler；字段以 LogRecord 属性（extra=...）承载。
"""

import logging
import uuid

from corespine import InProcessPrivacyTraceSink

# 全链路 trace 专用 logger（宿主配置 handler；默认不刷屏）
TRACE_LOGGER_NAME = "ragspine.trace"

# trace message 固定为该值（不含任何敏感内容）；亦作 corespine sink 的事件 code。
_TRACE_CODE = "trace"

_trace_logger = logging.getLogger(TRACE_LOGGER_NAME)

# 进程内隐私校验 sink：emit 时扫字段键，命中禁词即抛 TraceError，拦在落盘之前。
_privacy_sink = InProcessPrivacyTraceSink()


def new_request_id() -> str:
    """生成一次请求的关联键：uuid4 hex 短码（12 位，足够唯一且便于人读）。"""
    return uuid.uuid4().hex[:12]


def emit_trace(logger: logging.Logger | None = None, **fields: object) -> None:
    """以结构化方式记一条 INFO trace：字段经 extra 挂到 LogRecord 属性上。

    message 固定为 "trace"（不含任何敏感内容）；调用方只应传入非敏感元数据。
    载荷先过 corespine 隐私 sink 强制校验：若含受限正文字段（answer/text/content/...）
    则抛 TraceError、不落盘——隐私由机制保证，而非约定。
    """
    # 隐私强制：载荷含禁词键即抛 TraceError（在落盘之前），绝不悄悄记下去。
    _privacy_sink.emit(_TRACE_CODE, **fields)
    log = logger or _trace_logger
    log.info(_TRACE_CODE, extra=fields)
