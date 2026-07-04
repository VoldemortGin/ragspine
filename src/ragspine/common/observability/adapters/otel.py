"""OtelTraceSink —— 把隐私安全 trace 扇出到 OpenTelemetry（span event），经隐私门而非绕过（🛡）。

B1 的 OTel 适配器，落地 docs/prd-breadth-via-adapters.md 矩阵 Trace sink 行的 OTel adapter：证明
「观测可扇出到 OTel，且任何 sink——含 OTel——都不能泄漏正文」这条 PRD 承诺。emit 先过隐私门
sink.enforce_trace_privacy（命中 corespine FORBIDDEN_KEYS 即抛 TraceError、绝不外发），通过后才把
code + 非敏感字段作为一个 span event 记到 OpenTelemetry tracer 上——故它继承同一条隐私 conformance
（tests/conformance/test_trace_sink.py），结构上无法成为泄漏面。

重依赖 opentelemetry 延迟 import（behind [otel] extra，opentelemetry-api 为 Apache-2.0，过 ADR 0009
的 ≤Apache-2.0 许可门）：核心零依赖、CI 不引；缺 extra 时抛可执行的 pip 提示而非裸 ImportError。真加载
在装了 [otel] 的环境跑（conformance 由 importorskip 门控，缺则该参数 skip，黄不红）。

follow-up：把 emit_trace 的默认落盘旁路接到 config 选中的 sink（RAGSPINE_TRACE_SINK）做真正的多出口
扇出，以及 metrics / logs 桥与 span 语义细化——本模块先落「隐私安全的 OTel span 出口」这一档。
"""

from __future__ import annotations

from typing import Any

from ragspine.common.observability.sink import enforce_trace_privacy

# OTel span attribute 接受的标量类型（其余转 str 兜底）；标量序列亦可（须同质）。
_OTEL_SCALARS = (str, bool, int, float)


def _otel_scalar(value: object) -> Any:
    """把字段值收敛为 OTel span attribute 可接受的标量 / 标量序列（其余 str() 兜底，绝不抛）。"""
    if isinstance(value, _OTEL_SCALARS):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, _OTEL_SCALARS) for v in value):
        return list(value)
    return str(value)


class OtelTraceSink:
    """privacy-filtered OpenTelemetry TraceSink：隐私门在前，span event 在后（结构上无法泄漏正文）。

    emit 首先调用 enforce_trace_privacy(fields)，含受限键即抛 TraceError，正文永远走不到 OTel 那一步；
    通过校验的载荷只含 code / 计数 / 耗时 一类元数据，逐字段作为 span attribute 记到一个短命 span 上
    （span 名默认 ragspine.trace，与 emit_trace 的 message 同源）。满足 corespine @runtime_checkable
    TraceSink 结构协议（仅需 emit(code, **fields)）。
    """

    def __init__(self, tracer: Any | None = None, *, span_name: str = "ragspine.trace") -> None:
        self._span_name = span_name
        if tracer is None:
            try:
                from opentelemetry import trace as _otel_trace
            except ImportError as exc:  # pragma: no cover - 依赖缺失路径（CI 精简门不装 [otel]）
                raise ImportError(
                    "缺少可选依赖 opentelemetry：请先 `pip install rag-spine[otel]` 再重试。"
                ) from exc
            tracer = _otel_trace.get_tracer("ragspine")
        self._tracer = tracer

    def emit(self, code: str, **fields: object) -> None:
        """记一条 trace 到 OTel：先过隐私门（含正文即抛 TraceError），再落 span event。"""
        enforce_trace_privacy(fields)
        with self._tracer.start_as_current_span(self._span_name) as span:
            span.set_attribute("code", code)
            for key, value in fields.items():
                span.set_attribute(key, _otel_scalar(value))
