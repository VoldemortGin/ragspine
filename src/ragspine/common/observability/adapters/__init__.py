"""common.observability.adapters —— 第三方 TraceSink 适配器（延迟 import、behind extra）。

每个适配器都是 TraceSink 缝的一个薄壳，扇出前先过隐私门（sink.enforce_trace_privacy），故都继承
tests/conformance/test_trace_sink.py 的隐私 conformance pack——扇出经隐私 conformance 而非绕过。

Submodules:
    otel.py — OtelTraceSink：把隐私安全 trace 扇出到 OpenTelemetry（span event），behind [otel] extra。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
