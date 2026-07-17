"""受控 HTTP 客户端：http-request 节点的唯一出网面（受信 runner 侧，纯标准库 urllib）。

生成代码从不 import 网络模块（L0 import 白名单零放宽），只调用模块级 `_dify_http` 槽位；
本模块构造的客户端由 runner 在 RAGSPINE_DIFY_HTTP_ENABLED 显式开启时注入该槽位
（`_exec_in_sandbox`）。PRD 硬性约束全部在此落地：

- 超时钳制 ≤30s：节点 timeout_s 与默认值统一 min(·, MAX_TIMEOUT_S)。
- 仅 http/https：其它 scheme（file/ftp/data/...）直接拒绝，不发起任何连接。
- 重定向不得离开 http(s)：自定义 HTTPRedirectHandler，跳转目标非 http(s) 即拒。
- 响应体 1MB 上限：读满 MAX_BODY_BYTES 即断，不再继续读。

请求形状（生成代码组装的 dict，见 codegen/nodes._emit_http_request）：
{method, url, headers('k: v' 行), params('k: v' 行，拼 query string),
 body_type('none'|'raw-text'|'json'|'x-www-form-urlencoded'), body(文本),
 ssl_verify, timeout_s}。返回 {status_code, body(文本), headers(dict)}；
4xx/5xx 按 Dify 语义作为响应返回（不抛异常）。ssl_verify=False 刻意忽略——
受控客户端始终走系统默认证书校验，绝不降级。
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

# PRD 硬性上限：单次请求超时钳制（秒）与响应体读取上限（字节）。
MAX_TIMEOUT_S: float = 30.0
MAX_BODY_BYTES: int = 1024 * 1024
# 节点未配置超时时的默认值（同样受 MAX_TIMEOUT_S 钳制）。
DEFAULT_TIMEOUT_S: float = 10.0

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
# 无请求体的方法（GET/HEAD 等语义上不携带 body，一律不发）。
_BODYLESS_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


class GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向护栏：跳转目标必须仍是 http(s)，否则整个请求失败（绝不静默跟随）。

    标准库默认 handler 允许重定向到 ftp——这里收紧为仅 http/https。
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        absolute = urllib.parse.urljoin(req.full_url, newurl)
        scheme = urllib.parse.urlsplit(absolute).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise ValueError(f"http-request 重定向到非 http(s) URL，已拒绝：{absolute!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _parse_kv_lines(text: str) -> list[tuple[str, str]]:
    """Dify 的 'key: value' 行文本 → (key, value) 列表（无冒号/空行跳过，顺序保留）。"""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key:
            out.append((key, value.strip()))
    return out


def _clamp_timeout(raw: Any) -> float:
    """节点 timeout_s → 实际超时秒数（缺失/非法 → 默认；一律钳制 ≤ MAX_TIMEOUT_S）。"""
    timeout = DEFAULT_TIMEOUT_S
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        timeout = float(raw)
    return min(timeout, MAX_TIMEOUT_S)


def _encode_body(method: str, body_type: str, body: Any, headers: dict[str, str]) -> bytes | None:
    """请求体编码：x-www-form-urlencoded 的 'k: v' 行在此 urlencode；json/raw-text 原样
    utf-8 字节体；无体方法（GET/HEAD 等）/ body_type none 一律不发。就地补默认 Content-Type。"""
    if method in _BODYLESS_METHODS or body_type in ("", "none") or body is None:
        return None
    if body_type == "x-www-form-urlencoded":
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        return urllib.parse.urlencode(_parse_kv_lines(str(body))).encode("utf-8")
    if body_type == "json":
        headers.setdefault("Content-Type", "application/json")
    return str(body).encode("utf-8")


def build_http_client(
    opener: urllib.request.OpenerDirector | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """构造受控 HTTP 客户端（注入生成模块 _HTTP_CLIENT 槽位的 callable）。

    opener 可注入（测试用假 opener，零真实网络）；默认 build_opener(GuardedRedirectHandler)。
    """
    director = (
        opener if opener is not None else urllib.request.build_opener(GuardedRedirectHandler())
    )

    def _client(request: dict[str, Any]) -> dict[str, Any]:
        method = str(request.get("method", "get") or "get").upper()
        url = str(request.get("url", "") or "").strip()
        if urllib.parse.urlsplit(url).scheme.lower() not in _ALLOWED_SCHEMES:
            raise ValueError(f"http-request 仅允许 http/https URL，已拒绝：{url!r}")
        params = _parse_kv_lines(str(request.get("params", "") or ""))
        if params:
            sep = "&" if urllib.parse.urlsplit(url).query else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(params)}"
        headers = dict(_parse_kv_lines(str(request.get("headers", "") or "")))
        data = _encode_body(
            method,
            str(request.get("body_type", "none") or "none"),
            request.get("body"),
            headers,
        )
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp: Any = director.open(req, timeout=_clamp_timeout(request.get("timeout_s")))
        except urllib.error.HTTPError as exc:
            resp = exc  # 4xx/5xx 也是响应：按 Dify 语义回 status/body，不抛
        try:
            raw: bytes = resp.read(MAX_BODY_BYTES)  # 读满 1MB 即断，不再继续读
            status = int(getattr(resp, "status", None) or getattr(resp, "code", 0) or 0)
            headers_out = dict(resp.headers.items()) if resp.headers else {}
        finally:
            resp.close()
        return {
            "status_code": status,
            "body": raw.decode("utf-8", "replace"),
            "headers": headers_out,
        }

    return _client
