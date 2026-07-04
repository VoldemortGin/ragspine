"""common.observability —— 隐私安全可观测性：trace 发射原语 + 可插拔 TraceSink 缝（🛡）。

trace 只记【非敏感元数据】（request_id / route / 受控代码 / 计数 / 耗时），绝不记答案正文 /
事实数值 / chunk 正文——由 corespine InProcessPrivacyTraceSink 构造即兜底（隐私 by construction）。
TraceSink 缝把这条隐私约定形式化为一个【可注册 / 可选择 / 带 conformance pack】的出口：观测可扇出
到 OTel / 文件【经隐私 conformance 而非绕过】（见 sink.py + tests/conformance/test_trace_sink.py）。

包级门面：为向后兼容（既有 `from ragspine.common.observability import emit_trace` 等）与缝的可发现性，
在此再导出 trace 发射原语与 TraceSink 缝的公开面（两条子模块都只依赖 stdlib + corespine，轻量急切）。

Submodules:
    adapters/ — 第三方 TraceSink 适配器（OTel …），延迟 import，behind [otel] extra。
    sink.py — TraceSink 缝：复用 corespine Protocol + make_trace_sink / RAGSPINE_TRACE_SINK 注册表。
    trace.py — trace 发射原语：request_id 生成 + emit_trace 结构化发射（行为不变）。
"""

from ragspine.common.observability.sink import (
    FORBIDDEN_KEYS,
    TRACE_SINK_ENTRY_POINT_GROUP,
    TRACE_SINK_ENV,
    InProcessPrivacyTraceSink,
    TraceError,
    TraceEvent,
    TraceSink,
    enforce_trace_privacy,
    make_trace_sink,
)
from ragspine.common.observability.trace import (
    TRACE_LOGGER_NAME,
    emit_trace,
    new_request_id,
)

__all__ = [
    "FORBIDDEN_KEYS",
    "TRACE_LOGGER_NAME",
    "TRACE_SINK_ENTRY_POINT_GROUP",
    "TRACE_SINK_ENV",
    "InProcessPrivacyTraceSink",
    "TraceError",
    "TraceEvent",
    "TraceSink",
    "emit_trace",
    "enforce_trace_privacy",
    "make_trace_sink",
    "new_request_id",
]
