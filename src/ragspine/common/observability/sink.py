"""TraceSink 缝：可插拔、隐私强制的 trace 出口（形式化 common/observability 的隐私 trace，🛡）。

落地 docs/prd-breadth-via-adapters.md 的 `TraceSink`（NEW Protocol）行：把「trace 只记元数据、
绝不记正文」这条隐私约定形式化为一个【可注册 / 可选择 / 带 conformance pack】的缝——五段式范式
同 make_vector_store / make_graph_store：

    1. Protocol —— 直接【复用】corespine 的 @runtime_checkable TraceSink（emit(code, **fields)），
       不重复定义；ragspine 侧只在此把它连同隐私门与注册表一并抬到缝的高度。
    2. 离线默认 —— corespine InProcessPrivacyTraceSink（进程内、构造即隐私安全，命中禁词键即抛
       TraceError），与 common/observability.emit_trace 的默认兜底同一实现。
    3. 薄 adapter —— adapters/otel.py 的 OtelTraceSink（behind [otel] extra，延迟 import，扇出前先过隐私门）。
    4. 注册表 —— make_trace_sink / RAGSPINE_TRACE_SINK，内置 in_process / otel + entry-point 自动
       发现（group ragspine.trace_sinks），第三方装包即可按名字注册一个隐私安全 sink，无需核心 PR。
    5. conformance —— tests/conformance/test_trace_sink.py 对【每个注册 sink】参数化断言：含 answer /
       fact value / chunk text 的载荷必须被拒绝或擦除，绝不外泄；泄漏的 stub 直接 CI 红。

隐私门 enforce_trace_privacy 是缝的牙齿：任何要扇出到 OTel / 文件的 sink 都先过它（命中 corespine
FORBIDDEN_KEYS 即抛 TraceError），故「扇出经隐私 conformance 而非绕过」——没有任何 sink 能成为泄漏面。

默认行为字节不变：make_trace_sink 缺省（None/'none'）返回 None——emit_trace 仍走其内置
InProcessPrivacyTraceSink 隐私兜底，本缝是【形式化 + 可选注册】，不改现有 trace 记录路径。
"""

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from corespine import (
    FORBIDDEN_KEYS,
    InProcessPrivacyTraceSink,
    TraceError,
    TraceEvent,
    TraceSink,
)

# 工厂缺省 spec 时读取的环境变量名（范式同 store.VECTOR_STORE_ENV / graph.GRAPH_STORE_ENV）。
TRACE_SINK_ENV = "RAGSPINE_TRACE_SINK"

# 第三方 TraceSink 自动发现的 entry-point group：一个包在此 group 下注册一行
# （pyproject `[project.entry-points."ragspine.trace_sinks"]`），make_trace_sink 就能按名字选中它——
# 核心零改动、零 SDK import（范式同 store.VECTOR_STORE_ENTRY_POINT_GROUP）。
TRACE_SINK_ENTRY_POINT_GROUP = "ragspine.trace_sinks"

__all__ = [
    "FORBIDDEN_KEYS",
    "TRACE_SINK_ENTRY_POINT_GROUP",
    "TRACE_SINK_ENV",
    "InProcessPrivacyTraceSink",
    "TraceError",
    "TraceEvent",
    "TraceSink",
    "enforce_trace_privacy",
    "make_trace_sink",
]


def enforce_trace_privacy(fields: Mapping[str, object]) -> None:
    """隐私门：载荷键命中 corespine FORBIDDEN_KEYS 即抛 TraceError（归一小写后精确匹配）。

    这是每个 TraceSink 扇出前的强制关卡——「答案正文 / 事实数值 / chunk 正文」一类禁词键宁可报错
    也绝不外发。复用 corespine 的 FORBIDDEN_KEYS 与 TraceError，语义与 InProcessPrivacyTraceSink
    逐字一致，故任何自定义 / OTel sink 只要先调用它，就【经】同一条隐私门——扇出而不绕过。
    """
    offending = sorted(k for k in fields if k.strip().lower() in FORBIDDEN_KEYS)
    if offending:
        raise TraceError(
            f"trace 载荷含受限字段 {offending}：trace 只记 code / 计数 / 耗时，"
            "不得携带答案正文 / 字段取值 / chunk 正文。"
        )


# ---------------------------------------------------------------------------
# 注册表：内置 TraceSink 名字 -> 惰性 loader（返回 TraceSink【类】，尚不实例化）。范式同 store.py：
# 核心 import 本模块零重依赖；第三方 sink【不】登记此表，而是经 entry-point 自动发现，无需核心 PR。
# 别名共指同一 loader（大小写 / 留白 / 连字符由 make_trace_sink 归一化时以显式别名键覆盖）。
# ---------------------------------------------------------------------------
def _load_in_process() -> type[TraceSink]:
    return InProcessPrivacyTraceSink


def _load_otel() -> type[TraceSink]:
    """惰性加载 OTel 适配器；重依赖 opentelemetry 延迟 import（behind [otel] extra，保本模块零依赖）。"""
    from ragspine.common.observability.adapters.otel import OtelTraceSink

    return OtelTraceSink


_BUILTIN_LOADERS: dict[str, Callable[[], type[TraceSink]]] = {
    "in_process": _load_in_process,
    "in-process": _load_in_process,
    "inprocess": _load_in_process,
    "memory": _load_in_process,
    "privacy": _load_in_process,
    "otel": _load_otel,
    "opentelemetry": _load_otel,
}

# 错误信息中展示的内置规范名（别名不重复列出，保持可读）。
_BUILTIN_DISPLAY_NAMES = ("none", "in_process", "otel")


def _discover_entry_points() -> Sequence[Any]:
    """发现第三方在 TRACE_SINK_ENTRY_POINT_GROUP 下注册的 TraceSink 实现。

    在函数内 import entry_points，使 monkeypatch importlib.metadata.entry_points 在测试中生效，
    也让发现成本只在真正回落时付出（范式同 store._discover_entry_points）。
    """
    from importlib.metadata import entry_points

    return list(entry_points(group=TRACE_SINK_ENTRY_POINT_GROUP))


def _resolve_factory(normalized: str) -> Callable[..., TraceSink]:
    """归一化后的名字 -> 一个可 **kwargs 调用得到 TraceSink 的工厂（内置类或 entry-point 目标）。

    先查内置注册表（内置名字优先于同名 entry point，第三方不能劫持内置隐私默认语义）；未命中再回落
    entry-point 自动发现，按名字（大小写 / 留白不敏感）匹配后 .load()。两者皆不命中 -> ValueError，
    列出内置 + 已发现的 entry-point 名字。本函数只【解析】不【实例化】——故对 otel 内置 adapter 不会
    触发 opentelemetry import（SDK 由返回类 __init__ 在实例化时延迟 import）。
    """
    loader = _BUILTIN_LOADERS.get(normalized)
    if loader is not None:
        return loader()
    discovered = _discover_entry_points()
    for entry_point in discovered:
        if entry_point.name.strip().lower() == normalized:
            factory: Callable[..., TraceSink] = entry_point.load()
            return factory
    names = sorted({entry_point.name for entry_point in discovered})
    raise ValueError(
        f"未知 trace sink spec：{normalized!r}"
        f"（内置可选 {' / '.join(_BUILTIN_DISPLAY_NAMES)}；"
        f"已发现的 entry-point 后端：{names or '无'}；"
        f"第三方包可在 entry-point group {TRACE_SINK_ENTRY_POINT_GROUP!r} 下注册一个 sink）"
    )


def make_trace_sink(spec: str | None = None, **kwargs: Any) -> TraceSink | None:
    """TraceSink 工厂：把「用哪个 trace 出口」从改代码降为一个 spec/env（范式同 make_vector_store）。

    spec 取值（大小写 / 留白不敏感；缺省读环境变量 RAGSPINE_TRACE_SINK）：
        - None / 'none'                       -> None（不注入具体 sink；observability.emit_trace 仍走
          其内置 InProcessPrivacyTraceSink 隐私兜底，默认行为字节不变）。
        - 'in_process' / 'memory' / 'privacy' -> InProcessPrivacyTraceSink（零依赖、构造即隐私安全）。
        - 'otel' / 'opentelemetry'            -> OtelTraceSink（behind [otel] extra，延迟 import，扇出前先过隐私门）。
        - 其余                                -> entry-point 自动发现（第三方包在 TRACE_SINK_ENTRY_POINT_GROUP
          下注册即可被选中）；都不命中 -> ValueError 列出可选名字。

    名字经注册表解析（内置 loader 或 entry point），再以 **kwargs 实例化。返回 TraceSink 实例或 None。
    """
    if spec is None:
        spec = os.environ.get(TRACE_SINK_ENV)
    normalized = (spec or "none").strip().lower()
    if normalized == "none":
        return None
    factory = _resolve_factory(normalized)
    return factory(**kwargs)
