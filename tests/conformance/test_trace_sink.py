"""TraceSink 缝 conformance（B1，🛡）：把 privacy-aware traces 不变量绑死在缝上。

落地 docs/prd-breadth-via-adapters.md「Conformance: privacy trace —— 每个注册 TraceSink 都
必须【拒绝或擦除】含 answer / fact value / chunk text 的载荷」（line 278）与矩阵 Trace sink 行。

两层互补（同 VectorStore / GraphStore 缝的范式）：
  - 机制层：corespine.ConformanceSuite 把【每个注册实现】×隐私不变量绑成笛卡尔积，
    parametrize_kwargs() 两行消费（范围收敛在零依赖 in_process 隐私默认，见 conftest 注释）。
  - 领域层：fixture 形态参数化在 trace_sink 夹具上（in_process + otel 两实现各跑一遍），
    用【单一判定核】断言「绝不泄漏正文」——任何 sink 只要登记进 conftest.TRACE_SINK_IMPLS
    就必须证明它拒绝或擦除受限正文，不通过的 adapter（含 OTel）直接 CI 红，而非泄漏面。

非空泛证明（同 GraphStore/Chunker 的「诚实反证」手法）：两个【故意】泄漏正文的 stub 喂进
【同一判定核】必须 AssertionError——证明隐私断言不是空泛通过：
  - _LeakyTraceSink：把含 answer/value/text 的载荷【原样】落库 -> 触判定核 FAIL。
  - _ValueSmugglingTraceSink：删受限【键】骗过键扫描，却把受限【取值】塞进 benign 键偷运 -> 值扫描抓到 FAIL。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.common.observability.sink import (
    FORBIDDEN_KEYS,
    InProcessPrivacyTraceSink,
    TraceError,
    TraceEvent,
    TraceSink,
    make_trace_sink,
)
from tests.conformance.conftest import TRACE_SINK_SUITE

# 三类 PRD 明列的正文（answer / fact value / chunk text）+ 其余 FORBIDDEN_KEYS，各造一个含受限
# 键的载荷；_SECRET 取一段独一无二的正文，便于「取值偷运」检测扫描其原文是否落进记录。
_SECRET = "机密答案正文：香港 REVENUE=4500 与整段 chunk 全文——绝不得进 trace"
_SECRET_NUM = 424242.0
_FORBIDDEN_PAYLOADS: tuple[dict[str, object], ...] = (
    {"answer": _SECRET},        # 答案正文
    {"value": _SECRET_NUM},     # 事实数值
    {"text": _SECRET},          # chunk 文本
    {"content": _SECRET},
    {"chunk": _SECRET},
    {"chunk_text": _SECRET},
    {"body": _SECRET},
    {"prompt": _SECRET},
    {"completion": _SECRET},
)


# ===========================================================================
# 领域层判定核：「绝不泄漏正文」（参数化用例与反证 stub 共用同一核）
# ===========================================================================

def _recorded_leaks(sink: object, payload: dict[str, object]) -> bool:
    """未拒绝的 sink 必须【可自证未泄漏】：其记录里既无受限键、又无受限取值，否则判为泄漏。

    既不拒绝、又不可查（无 events 可读）的 sink 保守判为泄漏——「未证明安全」即不安全。
    OTel 一类只转发不留档的 sink 从不走到此分支：它对受限载荷【先拒绝】（见判定核）。
    """
    events = getattr(sink, "events", None)
    if events is None:
        return True
    for ev in events:
        fields = getattr(ev, "fields", {})
        if any(str(k).strip().lower() in FORBIDDEN_KEYS for k in fields):
            return True  # 受限键落进记录 = 泄漏
        blob = repr(fields)
        if any(str(v) and str(v) in blob for v in payload.values()):
            return True  # 受限取值（换个 benign 键）落进记录 = 偷运泄漏
    return False


def _sink_leaks(sink: object, payload: dict[str, object]) -> bool:
    """喂一个含受限正文的载荷：sink 若拒绝（抛 TraceError）视为不泄漏；否则查其记录有无正文。"""
    try:
        sink.emit("trace", request_id="r1", **payload)  # type: ignore[attr-defined]
    except TraceError:
        return False  # 拒绝 = 结构上不泄漏
    return _recorded_leaks(sink, payload)


def _assert_sink_never_leaks(sink: object) -> None:
    """判定核：对每个含 answer / fact value / chunk text 的载荷，sink 必须拒绝或擦除，绝不泄漏。"""
    for payload in _FORBIDDEN_PAYLOADS:
        assert not _sink_leaks(sink, payload), (
            f"TraceSink 泄漏了受限正文载荷 {list(payload)}"
            "（隐私 trace 必须拒绝或擦除答案正文 / 事实数值 / chunk 文本）"
        )


# ===========================================================================
# 机制层：corespine 套件消费者（实现 × 隐私不变量 笛卡尔积；范式同 test_vector_store_suite）
# ===========================================================================

@pytest.mark.parametrize(**TRACE_SINK_SUITE.parametrize_kwargs())
def test_trace_sink_conformance(case):
    """每个 (实现 × 隐私不变量) 格子：调用 thunk，满足静默、违反原样抛。"""
    case()


# ===========================================================================
# 领域层：在每个注册实现（in_process + otel）上各跑一遍
# ===========================================================================

def test_registered_sink_never_leaks(trace_sink):
    """每个注册 TraceSink：含 answer/fact value/chunk text 的载荷都被拒绝或擦除，绝不泄漏。"""
    _assert_sink_never_leaks(trace_sink)


def test_registered_sink_is_runtime_checkable(trace_sink):
    """每个注册 TraceSink 都结构匹配 @runtime_checkable TraceSink Protocol（复用 corespine）。"""
    assert isinstance(trace_sink, TraceSink)


def test_registered_sink_accepts_metadata(trace_sink):
    """每个注册 TraceSink：只含 code/count/timing 元数据的载荷 emit 不抛（元数据是允许面）。"""
    trace_sink.emit("trace", request_id="r1", route="structured", n_hits=3, took_ms=12)


# ===========================================================================
# 诚实反证：故意泄漏正文的 stub 喂进同一判定核必须 FAIL（证明隐私断言非空泛）
# ===========================================================================

class _LeakyTraceSink:
    """反证 stub：既不拒绝也不擦除——把含受限正文的载荷【原样】记进 events（故意泄漏正文）。"""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def emit(self, code: str, **fields: object) -> None:
        self.events.append(TraceEvent(code=code, fields=dict(fields)))


class _ValueSmugglingTraceSink:
    """反证 stub：删掉受限【键】以骗过键扫描，却把受限【取值】塞进 benign 键 note 偷运出去。"""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def emit(self, code: str, **fields: object) -> None:
        safe = {k: v for k, v in fields.items() if k.strip().lower() not in FORBIDDEN_KEYS}
        smuggled = [v for k, v in fields.items() if k.strip().lower() in FORBIDDEN_KEYS]
        if smuggled:
            safe["note"] = smuggled[0]  # 正文换个 benign 键偷运 -> 值扫描应抓到
        self.events.append(TraceEvent(code=code, fields=safe))


def test_leaky_sink_fails_privacy_core():
    """原样泄漏正文的 stub 喂进同一判定核必须 AssertionError——证明隐私断言非空泛。"""
    with pytest.raises(AssertionError):
        _assert_sink_never_leaks(_LeakyTraceSink())


def test_value_smuggling_sink_fails_privacy_core():
    """把受限取值换 benign 键偷运的 stub 喂进同一判定核必须 AssertionError——证明值扫描有牙齿。"""
    with pytest.raises(AssertionError):
        _assert_sink_never_leaks(_ValueSmugglingTraceSink())


# ===========================================================================
# 注册表：make_trace_sink 把「用哪个出口」从改代码降为一个 spec/env（范式同 make_vector_store）
# ===========================================================================

def test_make_trace_sink_default_is_none():
    """None / 'none' -> None（不注入具体 sink；emit_trace 仍走内置隐私兜底，默认行为字节不变）。"""
    assert make_trace_sink() is None
    assert make_trace_sink("none") is None
    assert make_trace_sink("  NONE  ") is None


def test_make_trace_sink_in_process_aliases():
    """内置 in_process 别名（大小写 / 留白 / 连字符）都解析到 InProcessPrivacyTraceSink 隐私默认。"""
    for spec in ("in_process", "in-process", "IN_PROCESS", "  memory  ", "privacy"):
        sink = make_trace_sink(spec)
        assert isinstance(sink, InProcessPrivacyTraceSink)
        assert isinstance(sink, TraceSink)


def test_make_trace_sink_env_selected(monkeypatch):
    """缺省 spec 时从 RAGSPINE_TRACE_SINK 环境变量读取（范式同 RAGSPINE_VECTOR_STORE）。"""
    monkeypatch.setenv("RAGSPINE_TRACE_SINK", "in_process")
    assert isinstance(make_trace_sink(), InProcessPrivacyTraceSink)


def test_make_trace_sink_unknown_raises():
    """既非内置也非已注册 entry point 的名字 -> ValueError（列出可选名字，不让人猜）。"""
    with pytest.raises(ValueError, match="trace sink"):
        make_trace_sink("no_such_sink_backend")


# ---------------------------------------------------------------------------
# entry-point 自动发现：第三方装包即可按名字注册一个隐私安全 sink（无需核心 PR）。
# ---------------------------------------------------------------------------
class _DummyTraceSink:
    """测试用最小 TraceSink（拒受限键、记允许元数据；记 kwargs 以验证透传）。"""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.events: list[TraceEvent] = []

    def emit(self, code: str, **fields: object) -> None:
        offending = [k for k in fields if k.strip().lower() in FORBIDDEN_KEYS]
        if offending:
            raise TraceError(f"含受限键 {offending}")
        self.events.append(TraceEvent(code=code, fields=dict(fields)))


class _FakeEntryPoint:
    """importlib.metadata.EntryPoint 的最小替身（只用到 .name + .load()）。"""

    def __init__(self, name: str, target: object) -> None:
        self.name = name
        self._target = target

    def load(self) -> object:
        return self._target


def _patch_entry_points(monkeypatch, eps: list[_FakeEntryPoint]) -> None:
    """把 importlib.metadata.entry_points 替换成只对 ragspine.trace_sinks group 返回 eps。"""
    import importlib.metadata

    from ragspine.common.observability.sink import TRACE_SINK_ENTRY_POINT_GROUP

    def _fake(*, group=None):
        return list(eps) if group == TRACE_SINK_ENTRY_POINT_GROUP else []

    monkeypatch.setattr(importlib.metadata, "entry_points", _fake)


def test_entry_point_sink_is_selectable_by_name(monkeypatch):
    """注册一个名为 dummy 的 entry point 后，make_trace_sink('dummy') 返回其实例（第三方装包即扩展）。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("dummy", _DummyTraceSink)])
    sink = make_trace_sink("dummy")
    assert isinstance(sink, _DummyTraceSink)
    assert isinstance(sink, TraceSink)  # 满足 runtime_checkable 结构协议


def test_entry_point_kwargs_passthrough(monkeypatch):
    """选用 entry-point sink 时 **kwargs 原样透传给其构造函数。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("dummy", _DummyTraceSink)])
    sink = make_trace_sink("dummy", endpoint="http://collector:4317")
    assert sink.kwargs == {"endpoint": "http://collector:4317"}


def test_builtin_name_wins_over_entry_point(monkeypatch):
    """内置名字优先于同名 entry point（第三方不能劫持内置隐私默认的语义）。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("in_process", _DummyTraceSink)])
    sink = make_trace_sink("in_process")
    assert isinstance(sink, InProcessPrivacyTraceSink)
    assert not isinstance(sink, _DummyTraceSink)


def test_unknown_name_lists_discovered(monkeypatch):
    """未知名字的 ValueError 同时列出内置 + 已发现的 entry-point 名字（不臆造、不让人猜）。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("dummy", _DummyTraceSink)])
    with pytest.raises(ValueError) as excinfo:
        make_trace_sink("nope_no_such_sink")
    msg = str(excinfo.value)
    assert "in_process" in msg  # 内置名字被列出
    assert "dummy" in msg       # 已发现的 entry-point 名字也被列出
