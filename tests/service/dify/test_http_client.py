"""http-request 安全层测试：L0 http 闸（默认关）+ 受控 urllib 客户端 + runner 注入。

测试禁止真实网络：受控客户端一律注入假 opener；runner 注入用 monkeypatch 假客户端。
"""

import io
import urllib.error
import urllib.request

import pytest

from ragspine.agent.llm_provider import MockProvider
from ragspine.dify.api import compile_dify_yaml
from ragspine.dify.codegen.emitter import GeneratedCode
from ragspine.service.dify import runner as dify_runner
from ragspine.service.dify.http_client import (
    MAX_BODY_BYTES,
    MAX_TIMEOUT_S,
    GuardedRedirectHandler,
    build_http_client,
)
from ragspine.service.dify.runner import run_generated
from ragspine.service.dify.safety import (
    ALLOWED_IMPORT_ROOTS,
    HTTP_ENABLED_ENV,
    DifyUnsafeError,
    assert_runnable,
    http_enabled,
)

HTTP_YAML = """
app:
  mode: workflow
  name: http-gate
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: q, type: text-input}]}
      - id: http_1
        data: {type: http-request, title: 调接口, method: get, url: 'http://example.com/api'}
      - id: end_1
        data:
          type: end
          title: 结束
          outputs:
            - {variable: status, value_selector: [http_1, status_code]}
            - {variable: body, value_selector: [http_1, body]}
    edges:
      - {source: start_1, target: http_1, sourceHandle: source}
      - {source: http_1, target: end_1, sourceHandle: source}
"""


def _http_code() -> GeneratedCode:
    return compile_dify_yaml(HTTP_YAML).code


@pytest.fixture(autouse=True)
def _http_env_unset(monkeypatch):
    """默认环境：HTTP 开关未设（各用例按需 setenv 显式开启）。"""
    monkeypatch.delenv(HTTP_ENABLED_ENV, raising=False)


# ---------------------------------------------------------------------------
# L0 闸 3：http-request 默认拒跑（RAGSPINE_DIFY_HTTP_ENABLED 显式开启才放行）
# ---------------------------------------------------------------------------
def test_http_gate_rejects_by_default():
    code = _http_code()
    assert code.requires_http is True
    with pytest.raises(DifyUnsafeError) as exc:
        assert_runnable(code)
    assert exc.value.code == "dify.unsafe"
    assert HTTP_ENABLED_ENV in str(exc.value)  # 错误信息明确说明开启方式


def test_http_gate_passes_when_enabled(monkeypatch):
    monkeypatch.setenv(HTTP_ENABLED_ENV, "true")
    assert_runnable(_http_code())  # 不抛即通过


def test_http_gate_detects_slot_even_if_flag_lost():
    # worker / L2 子进程从纯可序列化 payload 重建 GeneratedCode 时 requires_http 不随行；
    # 闸 3 从源码静态检出 _HTTP_CLIENT 槽位，防御式复检照样拦截。
    rebuilt = GeneratedCode(source=_http_code().source)
    assert rebuilt.requires_http is False
    with pytest.raises(DifyUnsafeError):
        assert_runnable(rebuilt)


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", " True "])
def test_http_enabled_truthy_values(raw):
    assert http_enabled({HTTP_ENABLED_ENV: raw}) is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "nonsense"])
def test_http_enabled_falsy_values(raw):
    assert http_enabled({HTTP_ENABLED_ENV: raw}) is False


def test_import_allowlist_not_widened_for_http():
    # 生成代码不 import 网络模块正是为了让白名单零扩widen。
    assert ALLOWED_IMPORT_ROOTS.isdisjoint({"urllib", "socket", "http", "ssl"})


# ---------------------------------------------------------------------------
# 受控客户端：假 opener 单测（零真实网络）
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, body: bytes = b"ok", status: int = 200, headers=None):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": "text/plain"} if headers is None else headers

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def close(self) -> None:
        pass


class FakeOpener(urllib.request.OpenerDirector):
    """假 opener（零真实网络）；继承 OpenerDirector 以满足受控客户端的注入签名。"""

    def __init__(self, response=None, exc=None):
        super().__init__()
        self.calls: list[tuple[urllib.request.Request, float]] = []
        self._response = response or FakeResponse()
        self._exc = exc

    def open(self, req, data=None, timeout=None):
        self.calls.append((req, timeout))
        if self._exc is not None:
            raise self._exc
        return self._response


def test_client_basic_get():
    opener = FakeOpener(FakeResponse(b"hello", 200))
    client = build_http_client(opener)
    out = client({"method": "get", "url": "http://e.com/a", "body_type": "none"})
    assert out == {
        "status_code": 200,
        "body": "hello",
        "headers": {"Content-Type": "text/plain"},
    }
    req, timeout = opener.calls[0]
    assert req.get_method() == "GET"
    assert req.data is None
    assert timeout == 10.0  # 缺省超时


def test_client_clamps_timeout_to_30s():
    opener = FakeOpener()
    client = build_http_client(opener)
    client({"method": "get", "url": "http://e.com", "timeout_s": 999})
    assert opener.calls[0][1] == MAX_TIMEOUT_S  # 999 → 钳到 30.0
    client({"method": "get", "url": "http://e.com", "timeout_s": 5})
    assert opener.calls[1][1] == 5.0


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://e.com/x", "data:text/plain,hi", ""])
def test_client_rejects_non_http_schemes(url):
    opener = FakeOpener()
    client = build_http_client(opener)
    with pytest.raises(ValueError):
        client({"method": "get", "url": url})
    assert opener.calls == []  # 拒绝发生在任何连接之前


def test_client_caps_response_body_at_1mb():
    opener = FakeOpener(FakeResponse(b"x" * (2 * MAX_BODY_BYTES)))
    client = build_http_client(opener)
    out = client({"method": "get", "url": "http://e.com"})
    assert len(out["body"]) == MAX_BODY_BYTES  # 读满 1MB 即断


def test_client_appends_params_to_query_string():
    opener = FakeOpener()
    client = build_http_client(opener)
    client(
        {
            "method": "get",
            "url": "http://e.com/a?b=1",
            "params": "k: v\n空: 值",
        }
    )
    url = opener.calls[0][0].full_url
    assert url.startswith("http://e.com/a?b=1&")
    assert "k=v" in url and "%" in url  # 非 ASCII 已 urlencode


def test_client_urlencodes_form_body_and_sets_content_type():
    opener = FakeOpener()
    client = build_http_client(opener)
    client(
        {
            "method": "post",
            "url": "http://e.com",
            "body_type": "x-www-form-urlencoded",
            "body": "a: 1\nb: 两",
        }
    )
    req = opener.calls[0][0]
    assert req.data == b"a=1&b=%E4%B8%A4"
    assert req.get_header("Content-type") == "application/x-www-form-urlencoded"


def test_client_sends_raw_json_body():
    opener = FakeOpener()
    client = build_http_client(opener)
    client(
        {
            "method": "post",
            "url": "http://e.com",
            "body_type": "json",
            "body": '{"q": 1}',
        }
    )
    req = opener.calls[0][0]
    assert req.data == b'{"q": 1}'
    assert req.get_header("Content-type") == "application/json"


def test_client_get_head_never_send_body():
    opener = FakeOpener()
    client = build_http_client(opener)
    for method in ("get", "head"):
        client(
            {
                "method": method,
                "url": "http://e.com",
                "body_type": "json",
                "body": '{"q": 1}',
            }
        )
    assert all(req.data is None for req, _ in opener.calls)


def test_client_returns_4xx_as_response():
    err = urllib.error.HTTPError(
        "http://e.com", 404, "not found", {"X-E": "1"}, io.BytesIO(b"missing")
    )
    client = build_http_client(FakeOpener(exc=err))
    out = client({"method": "get", "url": "http://e.com"})
    assert out["status_code"] == 404
    assert out["body"] == "missing"


def test_redirect_handler_blocks_escape_from_http():
    handler = GuardedRedirectHandler()
    req = urllib.request.Request("http://e.com/a")
    with pytest.raises(ValueError):
        handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "file:///etc/passwd")
    # http(s) 内跳转照常放行（交还标准库逻辑）。
    followed = handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "https://e.com/b")
    assert followed is not None
    assert followed.full_url == "https://e.com/b"


# ---------------------------------------------------------------------------
# runner 注入：启用时 _HTTP_CLIENT 槽位生效；未启用时 L0 闸先拒
# ---------------------------------------------------------------------------
def test_run_generated_rejects_http_workflow_when_disabled():
    with pytest.raises(DifyUnsafeError):
        run_generated(_http_code(), {"q": "x"}, MockProvider())


def test_run_generated_injects_controlled_client_when_enabled(monkeypatch):
    monkeypatch.setenv(HTTP_ENABLED_ENV, "1")
    seen: list[dict] = []

    def fake_build_http_client():
        def _client(request: dict) -> dict:
            seen.append(request)
            return {"status_code": 200, "body": "pong", "headers": {}}

        return _client

    monkeypatch.setattr(dify_runner, "build_http_client", fake_build_http_client)
    out = run_generated(_http_code(), {"q": "x"}, MockProvider())
    assert out == {"status": 200, "body": "pong"}
    assert seen[0]["url"] == "http://example.com/api"


def test_run_generated_uninjected_slot_raises_clear_error(monkeypatch):
    # 纵深防御：即便有人绕过闸 3（此处直接放行 http_enabled 但让注入失效），
    # 槽位保持 None，生成代码调用 _dify_http 即抛清晰 RuntimeError（整形为 dify.run_error）。
    monkeypatch.setenv(HTTP_ENABLED_ENV, "1")
    monkeypatch.setattr(dify_runner, "http_enabled", lambda env=None: False)
    from ragspine.service.dify.runner import DifyRunError

    with pytest.raises(DifyRunError) as exc:
        run_generated(_http_code(), {"q": "x"}, MockProvider())
    assert "RAGSPINE_DIFY_HTTP_ENABLED" in str(exc.value)
