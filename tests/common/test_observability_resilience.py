"""可观测性 + 网络韧性的红色规格测试（TDD 先红后绿）。

本文件按实现契约编写【失败的】测试，覆盖：
- A) 网络韧性：ProviderError 统一边界异常、timeout/max_retries 透传、token usage 回传；
- B) 诚实降级：provider 失败时结构化/叙事两路返回固定降级文案，不崩、不编造、不漏掉逻辑 bug；
- C) 可观测性：request_id、结构化 trace 发射、日志按 Restricted 对待（不泄敏）。

约束：只写测试不改实现。防编造硬约束（not_found 一律改写"查不到"）不得被韧性兜底破坏。
"""

import logging
import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.storage.fact_store import Fact, FactStore
from ragspine.agent.llm_provider import MockProvider, ProviderResponse, ToolCall

REF = date(2026, 6, 12)

REVENUE_HK_FY2025 = Fact(
    metric_code="REVENUE", entity="ACME_HK", geography="HK", channel="TOTAL",
    period_type="FY", period="2025", value=1702.0, unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx", source_locator="slide=5,table=1,row=2,col=3",
)

# 组级 REVENUE（用于触发 CLARIFY_ANSWER_WITH_ASSUMPTIONS 路径，含敏感数值 4500）
REVENUE_GROUP_FY2025 = Fact(
    metric_code="REVENUE", entity="ACME_GROUP", geography="ASIA", channel="TOTAL",
    period_type="FY", period="2025", value=4500.0, unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx", source_locator="slide=3,table=1",
)


@pytest.fixture
def store(tmp_db_path):
    fs = FactStore(tmp_db_path)
    fs.init_schema()
    fs.upsert_facts([REVENUE_HK_FY2025])
    yield fs
    fs.close()


def _tool_use_response(input_: dict) -> ProviderResponse:
    return ProviderResponse(
        text="", stop_reason="tool_use",
        tool_calls=[ToolCall(id="toolu_1", name="query_metric", input=input_)],
        raw_content=[{"type": "tool_use", "id": "toolu_1",
                      "name": "query_metric", "input": input_}],
    )


def _text_response(text: str) -> ProviderResponse:
    return ProviderResponse(text=text, stop_reason="end_turn", tool_calls=[],
                            raw_content=[{"type": "text", "text": text}])


class FakeRetriever:
    """duck-typed NarrativeRetriever：返回固定片段。"""

    def __init__(self, snippets: list[dict] | None = None):
        self.snippets = snippets if snippets is not None else [{
            "text": "香港 REVENUE 下降主因是 MCV 客群收缩与银保渠道调整。",
            "doc_id": "HK_QBR_2025Q4.pptx",
            "locator": "slide=12",
        }]
        self.calls: list[dict] = []

    def retrieve(self, query: str, *, filters: dict | None = None, top_k: int = 50):
        self.calls.append({"query": query, "filters": filters, "top_k": top_k})
        return self.snippets


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


# ===========================================================================
# R1 — ProviderError 降级（结构化）
# user story：作为高管，当后端 AI 服务临时抖动/超时时，我宁可看到一句诚实的
# "暂时不可用"，也绝不能看到系统崩溃或被编造出来的数字。
# ===========================================================================

def test_provider_error_degrades_structured_route(store):
    from ragspine.agent.llm_provider import ProviderError

    class ErroringProvider:
        def create_message(self, *, system, messages, tools):
            raise ProviderError("模拟网关 503")

    result = answer_question(
        "香港2025年REVENUE多少", store, ErroringProvider(), reference_date=REF
    )
    assert isinstance(result, AgentResult)
    # 不抛异常、route 保持结构化
    assert result.route == "structured"
    # 固定降级文案（结构化路）
    assert "AI 服务暂时不可用" in result.answer
    assert "请稍后再试" in result.answer
    # 绝不含任何数字、绝不编造、无来源
    assert not _has_digit(result.answer)
    assert result.sources == []


# ===========================================================================
# R2 — ProviderError 降级（叙事）
# user story：归因/监管类问题走叙事合成，provider 失败时同样要诚实降级，不能崩。
# ===========================================================================

def test_provider_error_degrades_narrative_route(store):
    from ragspine.agent.llm_provider import ProviderError

    class ErroringProvider:
        def create_message(self, *, system, messages, tools):
            raise ProviderError("模拟超时")

    retriever = FakeRetriever()
    result = answer_question(
        "香港最近有什么监管动态", store, ErroringProvider(),
        reference_date=REF, narrative_retriever=retriever,
    )
    assert result.route == "narrative"
    assert "AI 服务暂时不可用" in result.answer
    assert "请稍后再试" in result.answer


# ===========================================================================
# R3 — 不掩盖逻辑 bug
# user story：韧性兜底只能吃 provider 网络/API 异常（ProviderError），编排层自身的
# 逻辑 bug（KeyError 等）必须照常冒泡，绝不能被宽泛 except 吞掉而静默降级。
# ===========================================================================

def test_logic_bug_not_swallowed_by_resilience(store):
    class BuggyProvider:
        def create_message(self, *, system, messages, tools):
            raise KeyError("内部逻辑 bug，不该被韧性吞掉")

    with pytest.raises(KeyError):
        answer_question("香港2025年REVENUE多少", store, BuggyProvider(), reference_date=REF)


# ===========================================================================
# R4 — timeout / max_retries 透传
# user story：运维要能为 Anthropic 调用配置超时与重试上限，直接交给 SDK 原生退避，
# 不自造退避逻辑——因此构造参数必须如实透传给 anthropic.Anthropic()。
# ===========================================================================

def test_anthropic_timeout_and_retries_passed_to_client(monkeypatch):
    import ragspine.agent.llm_provider as llm_provider

    captured: dict = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.messages = object()

    fake_module = type("M", (), {"Anthropic": FakeAnthropic})()
    # 延迟 import 的 `import anthropic` 命中我们注入的假模块
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake_module)

    llm_provider.AnthropicProvider(timeout=5, max_retries=4)

    assert captured.get("timeout") == 5
    assert captured.get("max_retries") == 4


# ===========================================================================
# R5 — token usage 回传
# user story：为做成本/容量可观测，ProviderResponse 要带 usage（input/output tokens），
# AnthropicProvider 从 SDK resp.usage 映射出来；缺失则 None（防御式，不崩）。
# ===========================================================================

def test_provider_response_has_usage_field():
    resp = ProviderResponse(text="hi")
    assert hasattr(resp, "usage")
    assert resp.usage is None  # 默认 None


def test_anthropic_maps_token_usage(monkeypatch):
    import ragspine.agent.llm_provider as llm_provider

    class FakeUsage:
        input_tokens = 123
        output_tokens = 45

    class FakeTextBlock:
        type = "text"
        text = "答案正文"

    class FakeResp:
        content = [FakeTextBlock()]
        stop_reason = "end_turn"
        usage = FakeUsage()

    class FakeMessages:
        def create(self, **kwargs):
            return FakeResp()

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

    class FakeAnthropic:
        def __init__(self, **kwargs):
            pass

    fake_module = type("M", (), {"Anthropic": FakeAnthropic})()
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake_module)

    provider = llm_provider.AnthropicProvider()
    provider._client = FakeClient()  # 假 client，不联网

    out = provider.create_message(system="s", messages=[], tools=[])
    assert out.usage == {"input_tokens": 123, "output_tokens": 45}


# ===========================================================================
# R6 — trace 发射 + 字段
# user story：每次 answer_question 都要在 "ragspine.trace" 上发恰好一条 INFO 结构化 trace，
# 携带 request_id / route / 指标实体代码 / tool status 计数 / 防编造触发标志，
# 以便事后做端到端追踪与质量分析。
# ===========================================================================

def test_trace_emitted_with_fields(store, caplog):
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        answer_question(
            "香港2025年REVENUE多少", store, MockProvider(reference_date=REF),
            reference_date=REF,
        )
    traces = [r for r in caplog.records if r.name == "ragspine.trace"]
    assert len(traces) == 1, f"期望恰好一条 ragspine.trace INFO，实得 {len(traces)}"
    rec = traces[0]
    assert rec.levelno == logging.INFO

    # 字段以 LogRecord 属性方式承载（structured logging：extra=...）
    assert getattr(rec, "request_id", None)  # 非空
    assert getattr(rec, "route", None) == "structured"
    assert getattr(rec, "metric", None) == "REVENUE"
    assert getattr(rec, "entity", None) == "ACME_HK"
    # tool status 计数：found/not_found/unrecognized 各几条
    tool_status = getattr(rec, "tool_status_counts", None)
    assert isinstance(tool_status, dict)
    assert tool_status.get("found") == 1
    # 防编造强制改写标志（found 路未触发）
    assert getattr(rec, "fabrication_guard_triggered", None) is False


def test_trace_fabrication_guard_flag_true_on_not_found(store, caplog):
    """user story：not_found 触发防编造强制改写时，trace 的 fabrication_guard_triggered=True，
    便于统计有多少次模型试图越界、被编排层拦下。"""
    from ragspine.agent.llm_provider import ProviderResponse, ToolCall

    class ScriptedProvider:
        def __init__(self, responses):
            self._responses = list(responses)

        def create_message(self, *, system, messages, tools):
            return self._responses.pop(0)

    provider = ScriptedProvider([
        _tool_use_response({"metric": "ROE", "entity": "ACME_CN", "period": "FY2024"}),
        _text_response("ACME 中国 FY2024 ROE 为 9999%，表现亮眼。"),  # 对抗：编造
    ])
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        result = answer_question("中国2024年ROE多少", store, provider, reference_date=REF)
    assert "查不到" in result.answer
    traces = [r for r in caplog.records if r.name == "ragspine.trace"]
    assert len(traces) == 1
    assert getattr(traces[0], "fabrication_guard_triggered", None) is True


# ===========================================================================
# R7 — 日志不泄敏（"日志按 Restricted 对待"）
# user story：trace 是给运维/审计看的元数据，绝不能把事实数值与答案正文写进 INFO 日志，
# 否则日志即成为受限数据的泄露面。
# ===========================================================================

def test_trace_does_not_leak_sensitive_values(store, caplog):
    # 组级 REVENUE（4500）会走"默认先答"，答案正文含敏感数字
    store.upsert_facts([REVENUE_GROUP_FY2025])
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        result = answer_question(
            "REVENUE多少", store, MockProvider(reference_date=REF), reference_date=REF
        )
    assert "4500" in result.answer  # 答案里确实有敏感数值
    traces = [r for r in caplog.records if r.name == "ragspine.trace"]
    assert len(traces) == 1
    blob = traces[0].getMessage() + " " + str(traces[0].__dict__)
    # 事实数值不得出现在日志里
    assert "4500" not in blob
    # 答案正文片段不得出现在日志里
    assert "假设" not in blob


# ===========================================================================
# R8 — request_id 稳定且非空
# user story：单次 answer_question 内 request_id 必须稳定唯一，作为整条链路的关联键。
# ===========================================================================

def test_request_id_present_and_stable(store, caplog):
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        answer_question(
            "香港2025年REVENUE多少", store, MockProvider(reference_date=REF),
            reference_date=REF,
        )
    traces = [r for r in caplog.records if r.name == "ragspine.trace"]
    assert len(traces) == 1
    rid = getattr(traces[0], "request_id", None)
    assert rid
    assert isinstance(rid, str)
    assert len(rid) >= 6  # uuid4 hex 短码，非空且有一定长度


def test_observability_helpers_exist():
    """user story：观测工具模块提供 new_request_id / emit_trace 两个原语供编排层调用。"""
    from ragspine.common import observability

    rid1 = observability.new_request_id()
    rid2 = observability.new_request_id()
    assert isinstance(rid1, str) and rid1
    assert rid1 != rid2  # 每次不同
    assert callable(observability.emit_trace)


def test_emit_trace_logs_info_with_fields(caplog):
    """user story：emit_trace 以结构化方式记一条 INFO，字段以 LogRecord 属性承载。"""
    from ragspine.common import observability

    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        observability.emit_trace(request_id="abc123", route="structured", metric="REVENUE")
    traces = [r for r in caplog.records if r.name == "ragspine.trace"]
    assert len(traces) == 1
    rec = traces[0]
    assert rec.levelno == logging.INFO
    assert getattr(rec, "request_id", None) == "abc123"
    assert getattr(rec, "route", None) == "structured"
    assert getattr(rec, "metric", None) == "REVENUE"


def test_emit_trace_rejects_forbidden_content_keys(caplog):
    """user story：隐私由机制强制——载荷含正文字段（answer/text/content/...）时 emit_trace
    直接抛 TraceError 且【绝不落盘】，而不是悄悄把受限正文写进 INFO 日志。"""
    from corespine import CorespineError
    from ragspine.common import observability

    for forbidden_key in ("answer", "text", "content"):
        with caplog.at_level(logging.INFO, logger="ragspine.trace"):
            with pytest.raises(CorespineError):
                observability.emit_trace(request_id="abc123", **{forbidden_key: "机密正文"})
    # 抛错路径不得有任何 trace 落盘，受限正文不进日志面
    traces = [r for r in caplog.records if r.name == "ragspine.trace"]
    assert traces == []


# ===========================================================================
# R9 — 回归守护：MockProvider 正常路径行为不变
# user story：加了观测/韧性后，既有正常路径（找数 / not_found）的对外行为必须逐字节不变。
# ===========================================================================

def test_regression_mock_found_path_unchanged(store):
    result = answer_question(
        "香港去年REVENUE多少", store, MockProvider(reference_date=REF), reference_date=REF
    )
    assert result.route == "structured"
    assert "1702" in result.answer
    assert "ACME_FY2025_Results.pptx" in result.answer
    assert result.tool_results[0]["status"] == "found"
    assert result.sources == [{"doc": "ACME_FY2025_Results.pptx",
                               "locator": "slide=5,table=1,row=2,col=3"}]


def test_regression_mock_not_found_path_unchanged(store):
    result = answer_question(
        "中国去年ROE多少", store, MockProvider(reference_date=REF), reference_date=REF
    )
    assert result.tool_results[0]["status"] == "not_found"
    assert "查不到" in result.answer
    assert result.sources == []
