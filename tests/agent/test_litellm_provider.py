"""LiteLLMProvider 测试：litellm.completion 全部 monkeypatch，不发真实网络请求。

真实调用的 smoke 用例标 network 且需 RAGSPINE_LITELLM_SMOKE=1 显式开启（模型取 RAGSPINE_LITELLM_MODEL，
缺省 deepseek/deepseek-chat，key 由 litellm 读厂商环境变量），默认跳过。
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import date
from types import SimpleNamespace

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion, ProviderError

from ragspine.agent import litellm_provider as mod
from ragspine.agent.agent import answer_question
from ragspine.agent.litellm_provider import DEFAULT_LITELLM_MODEL, LiteLLMProvider
from ragspine.agent.llm_provider import StreamingProvider, provider_supports_images
from ragspine.agent.query_tools import QUERY_METRIC_TOOL_OPENAI
from ragspine.storage.fact_store import Fact, SqliteFactStore

try:
    litellm = mod._load_litellm()  # 经 provider 自己的加载器：本地价格表、不联网
except ImportError:
    pytest.skip("未安装 [litellm] extra", allow_module_level=True)

TOOLS = [QUERY_METRIC_TOOL_OPENAI]


def _resp(content=None, tool_calls=None, finish_reason="stop", usage=(11, 7)):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        id="chatcmpl-1",
        model="deepseek-chat",
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
    )


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments)
    )


class FakeCompletion:
    """记录每次 litellm.completion 调用，按脚本依次返回（异常则抛出）。"""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outputs.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


@pytest.fixture
def fake(monkeypatch):
    def install(*outputs):
        f = FakeCompletion(*outputs)
        monkeypatch.setattr(litellm, "completion", f)
        return f

    return install


def test_defaults_are_deepseek_text_only_and_lazy():
    p = LiteLLMProvider()
    assert p.model == DEFAULT_LITELLM_MODEL == "deepseek/deepseek-chat"
    assert p.supports_image_input is False
    assert provider_supports_images(p) is False
    assert isinstance(p, StreamingProvider)


def test_import_and_construct_do_not_load_litellm():
    code = (
        "import sys;"
        "from ragspine.agent.litellm_provider import LiteLLMProvider;"
        "LiteLLMProvider('openai/x', api_base='http://127.0.0.1:1/v1');"
        "print('litellm' in sys.modules)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT_DIR)
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"


def test_missing_litellm_raises_clear_import_error(monkeypatch):
    monkeypatch.setattr(mod, "_litellm", None)
    monkeypatch.setitem(sys.modules, "litellm", None)
    with pytest.raises(ImportError, match=r"ragspine\[litellm\]"):
        LiteLLMProvider().chat([{"role": "user", "content": "hi"}])


def test_load_turns_off_telemetry_callbacks_and_message_logging(monkeypatch, fake):
    monkeypatch.setattr(mod, "_litellm", None)
    monkeypatch.setattr(litellm, "telemetry", True)
    monkeypatch.setattr(litellm, "success_callback", ["langfuse"])
    monkeypatch.setattr(litellm, "failure_callback", ["langfuse"])
    monkeypatch.setattr(litellm, "callbacks", ["langfuse"])
    monkeypatch.setattr(litellm, "turn_off_message_logging", False)
    fake(_resp("ok"))
    LiteLLMProvider().chat([{"role": "user", "content": "hi"}])
    assert litellm.telemetry is False
    assert litellm.turn_off_message_logging is True
    assert litellm.suppress_debug_info is True
    assert litellm.success_callback == litellm.failure_callback == litellm.callbacks == []
    assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == "True"


def test_text_completion_passes_model_timeout_retries_and_maps_usage(fake):
    f = fake(_resp("你好"))
    p = LiteLLMProvider("openai/qwen", api_base="http://gw/v1", timeout=9.0, num_retries=3)
    out = p.chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}])
    assert isinstance(out, ChatCompletion)
    assert out.choices[0].message.content == "你好"
    assert out.choices[0].message.tool_calls is None
    assert out.choices[0].finish_reason == "stop"
    assert (out.usage.prompt_tokens, out.usage.completion_tokens, out.usage.total_tokens) == (
        11,
        7,
        18,
    )
    call = f.calls[0]
    assert call["model"] == "openai/qwen"
    assert call["api_base"] == "http://gw/v1"
    assert call["timeout"] == 9.0
    assert call["num_retries"] == 3
    assert "tools" not in call and "api_key" not in call
    assert call["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_tool_calls_round_trip_in_openai_shape(fake):
    f = fake(
        _resp(
            None,
            [_tool_call("call_1", "query_metric", '{"metric": "REVENUE"}')],
            finish_reason="tool_calls",
        )
    )
    out = LiteLLMProvider().chat([{"role": "user", "content": "q"}], tools=TOOLS)
    assert f.calls[0]["tools"] == TOOLS
    msg = out.choices[0].message
    assert msg.content is None
    assert out.choices[0].finish_reason == "tool_calls"
    (tc,) = msg.tool_calls
    assert tc.id == "call_1"
    assert tc.function.name == "query_metric"
    assert json.loads(tc.function.arguments) == {"metric": "REVENUE"}


def test_empty_tool_arguments_normalize_to_empty_object(fake):
    fake(_resp(None, [_tool_call("c", "query_metric", "")], finish_reason="tool_calls"))
    out = LiteLLMProvider().chat([{"role": "user", "content": "q"}], tools=TOOLS)
    assert out.choices[0].message.tool_calls[0].function.arguments == "{}"


def test_tool_loop_messages_pass_through_unchanged(fake):
    f = fake(_resp("done"))
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "query_metric", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"status": "found"}'},
    ]
    LiteLLMProvider().chat(messages, tools=TOOLS)
    assert f.calls[0]["messages"] == messages


@pytest.mark.parametrize(
    "exc",
    [
        litellm.exceptions.Timeout(message="t", model="m", llm_provider="deepseek"),
        litellm.exceptions.APIConnectionError(message="c", llm_provider="deepseek", model="m"),
        litellm.exceptions.RateLimitError(message="r", llm_provider="deepseek", model="m"),
        litellm.exceptions.AuthenticationError(message="a", llm_provider="deepseek", model="m"),
    ],
)
def test_network_api_timeout_errors_become_provider_error(fake, exc):
    fake(exc)
    with pytest.raises(ProviderError):
        LiteLLMProvider().chat([{"role": "user", "content": "q"}])


def test_program_errors_propagate(fake):
    fake(KeyError("bug"))
    with pytest.raises(KeyError):
        LiteLLMProvider().chat([{"role": "user", "content": "q"}])


def test_concurrency_is_capped(monkeypatch):
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def slow(**kwargs):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return _resp("ok")

    monkeypatch.setattr(litellm, "completion", slow)
    p = LiteLLMProvider(max_concurrency=2)
    threads = [
        threading.Thread(target=p.chat, args=([{"role": "user", "content": "q"}],))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] == 2


def test_max_concurrency_must_be_positive():
    with pytest.raises(ValueError):
        LiteLLMProvider(max_concurrency=0)


def _image_message(tmp_path):
    png = tmp_path / "p18.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return png, [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "片段\n图：p18.png"},
                {"type": "image", "path": str(png), "name": "p18.png", "doc_id": "d", "page": 18},
            ],
        }
    ]


def test_image_parts_become_base64_image_url_when_enabled(fake, tmp_path):
    import base64

    png, messages = _image_message(tmp_path)
    f = fake(_resp("ok"))
    p = LiteLLMProvider("openai/gpt-4o", image_input=True)
    assert provider_supports_images(p) is True
    p.chat(messages)
    content = f.calls[0]["messages"][0]["content"]
    data = base64.b64encode(png.read_bytes()).decode("ascii")
    assert content == [
        {"type": "text", "text": "片段\n图：p18.png"},
        {"type": "text", "text": "p18.png"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
    ]
    assert str(png) not in json.dumps(f.calls[0])  # 本地存储路径不进请求


def test_image_parts_flatten_to_text_when_disabled(fake, tmp_path):
    _, messages = _image_message(tmp_path)
    f = fake(_resp("ok"))
    LiteLLMProvider().chat(messages)
    assert f.calls[0]["messages"][0]["content"] == "片段\n图：p18.png"


def test_chat_stream_yields_text_deltas(fake):
    def chunk(text):
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])

    f = fake(iter([chunk("你"), chunk(None), SimpleNamespace(choices=[]), chunk("好")]))
    out = list(LiteLLMProvider().chat_stream([{"role": "user", "content": "q"}]))
    assert out == ["你", "好"]
    assert f.calls[0]["stream"] is True


def test_chat_stream_maps_errors(fake):
    fake(litellm.exceptions.Timeout(message="t", model="m", llm_provider="deepseek"))
    with pytest.raises(ProviderError):
        list(LiteLLMProvider().chat_stream([{"role": "user", "content": "q"}]))


def test_from_env_reads_model_api_base_and_image_flag():
    p = LiteLLMProvider.from_env(
        {
            "RAGSPINE_LITELLM_MODEL": "ollama/qwen3",
            "RAGSPINE_LITELLM_API_BASE": "http://127.0.0.1:11434",
            "RAGSPINE_LITELLM_IMAGE_INPUT": "true",
        }
    )
    assert (p.model, p.api_base, p.supports_image_input) == (
        "ollama/qwen3",
        "http://127.0.0.1:11434",
        True,
    )
    d = LiteLLMProvider.from_env({})
    assert (d.model, d.api_base, d.supports_image_input) == (DEFAULT_LITELLM_MODEL, None, False)


def test_answer_question_tool_loop_with_litellm(fake, tmp_path):
    """编排层经 LiteLLMProvider 走完 tool-use 循环：found 值来自事实表并带血缘。"""
    store = SqliteFactStore(tmp_path / "f.db")
    store.init_schema()
    store.upsert_facts(
        [
            Fact(
                metric_code="REVENUE",
                entity="ACME_HK",
                geography="HK",
                channel="TOTAL",
                period_type="FY",
                period="2025",
                value=1702.0,
                unit="USD_M",
                source_doc_id="ACME_FY2025_Results.pptx",
                source_locator="slide=5,table=1,row=2,col=3",
            )
        ]
    )
    args = {"metric": "REVENUE", "entity": "ACME_HK", "period": "FY2025", "channel": "TOTAL"}
    fake(
        _resp(
            None,
            [_tool_call("c1", "query_metric", json.dumps(args))],
            finish_reason="tool_calls",
        ),
        _resp("香港 FY2025 REVENUE 为 1702 USD_M"),
    )
    try:
        result = answer_question(
            "香港FY2025的REVENUE是多少", store, LiteLLMProvider(), reference_date=date(2026, 6, 12)
        )
    finally:
        store.close()
    assert "1702" in result.answer
    assert result.sources and result.sources[0]["doc"] == "ACME_FY2025_Results.pptx"


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("RAGSPINE_LITELLM_SMOKE") != "1",
    reason="真实 litellm 调用：设 RAGSPINE_LITELLM_SMOKE=1 开启",
)
def test_real_litellm_smoke():
    p = LiteLLMProvider.from_env()
    out = p.chat([{"role": "user", "content": "只回复两个字：你好"}])
    assert out.choices[0].message.content
    out = p.chat(
        [{"role": "user", "content": "查一下香港 FY2025 的 REVENUE（TOTAL 渠道）"}], tools=TOOLS
    )
    assert out.choices[0].message.tool_calls
    assert out.choices[0].message.tool_calls[0].function.name == "query_metric"
