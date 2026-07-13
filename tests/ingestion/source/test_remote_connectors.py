"""HttpConnector / NotionConnector（惰性 import httpx 的真实远程拉取连接器）的行为契约。

用注入的 fake client 做离线确定性验证（不联网）：断言 source_doc_id 派生口径、locator、
content 字节、file_hash 血缘、给定顺序，以及工厂解析（'http' / 'notion'）。
另外断言 httpx 是【惰性】依赖：import connector + 建 InMemoryConnector 不需要 httpx。
"""

import hashlib
import json
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.ingestion.source.connector import SourceConnector, make_source_connector
from ragspine.ingestion.source.remote import HttpConnector, NotionConnector


# --------------------------------------------------------------------------- #
# fake httpx client：暴露 .get(url, headers=...) -> 带 .content/.headers/.json() 的响应。
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, content: bytes, headers: dict | None = None, payload=None):
        self.content = content
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeHttpClient:
    """记录每次 GET 的 (url, headers)，按 url 返回预置响应。"""

    def __init__(self, responses: dict[str, _FakeResponse]):
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None):
        self.calls.append((url, dict(headers or {})))
        return self._responses[url]


# ===========================================================================
# HttpConnector
# ===========================================================================
def test_http_structural_conformance():
    assert isinstance(HttpConnector(["https://x/y.pdf"]), SourceConnector)


def test_http_derives_doc_id_locator_content_hash():
    """source_doc_id = URL path 末段；locator = URL；content = 响应字节；file_hash = sha256。"""
    url = "https://example.com/data/report.pdf"
    body = b"%PDF-report-bytes"
    client = _FakeHttpClient(
        {url: _FakeResponse(body, headers={"Content-Type": "application/pdf"})}
    )
    [doc] = list(HttpConnector([url], client=client).iter_documents())
    assert doc.source_doc_id == "report.pdf"
    assert doc.locator == url
    assert doc.content == body
    assert doc.content_type == "application/pdf"
    assert doc.metadata["file_hash"] == hashlib.sha256(body).hexdigest()


def test_http_content_type_falls_back_to_suffix():
    """无 Content-Type 头时，content_type 回退到 URL 末段后缀。"""
    url = "https://example.com/x/a.xlsx"
    client = _FakeHttpClient({url: _FakeResponse(b"xx", headers={})})
    [doc] = list(HttpConnector([url], client=client).iter_documents())
    assert doc.content_type == ".xlsx"


def test_http_slug_fallback_when_no_path_segment():
    """URL path 末段为空时，source_doc_id 回退到稳定 slug（非空、可回溯）。"""
    url = "https://example.com/"
    client = _FakeHttpClient({url: _FakeResponse(b"root", headers={})})
    [doc] = list(HttpConnector([url], client=client).iter_documents())
    assert doc.source_doc_id  # 非空
    assert doc.locator == url


def test_http_deterministic_given_order_and_headers():
    """按给定 URL 顺序产出；自定义 headers 透传到每次 GET。"""
    u1 = "https://example.com/a.txt"
    u2 = "https://example.com/b.txt"
    client = _FakeHttpClient(
        {u1: _FakeResponse(b"a", headers={}), u2: _FakeResponse(b"b", headers={})}
    )
    conn = HttpConnector([u1, u2], headers={"X-Token": "t"}, client=client)
    ids = [d.source_doc_id for d in conn.iter_documents()]
    assert ids == ["a.txt", "b.txt"]
    assert all(hdr.get("X-Token") == "t" for _url, hdr in client.calls)


def test_http_factory_resolves():
    """make_source_connector('http', urls=[...], client=fake) 解析到 HttpConnector。"""
    url = "https://example.com/z.txt"
    client = _FakeHttpClient({url: _FakeResponse(b"z", headers={})})
    conn = make_source_connector("http", urls=[url], client=client)
    assert isinstance(conn, HttpConnector)
    assert [d.source_doc_id for d in conn.iter_documents()] == ["z.txt"]


# ===========================================================================
# NotionConnector
# ===========================================================================
def _notion_client(page_id: str, base: str = "https://api.notion.com/v1"):
    page = {"object": "page", "id": page_id, "url": f"https://www.notion.so/{page_id}"}
    blocks = {"object": "list", "results": [{"type": "paragraph", "id": "blk1"}]}
    return _FakeHttpClient(
        {
            f"{base}/pages/{page_id}": _FakeResponse(b"", payload=page),
            f"{base}/blocks/{page_id}/children": _FakeResponse(b"", payload=blocks),
        }
    ), page, blocks


def test_notion_structural_conformance():
    client, _p, _b = _notion_client("pg1")
    assert isinstance(
        NotionConnector(token="t", page_ids=["pg1"], client=client), SourceConnector
    )


def test_notion_yields_page_json_with_lineage():
    """source_doc_id = page_id；locator = notion URI；content = 页面 JSON 字节；file_hash = sha256。"""
    client, page, blocks = _notion_client("pg1")
    [doc] = list(
        NotionConnector(token="secret", page_ids=["pg1"], client=client).iter_documents()
    )
    assert doc.source_doc_id == "pg1"
    assert doc.locator.startswith("notion://") or doc.locator == page["url"]
    assert doc.content_type == "application/json"
    payload = json.loads(doc.content.decode("utf-8"))
    assert payload["page"] == page
    assert payload["blocks"] == blocks
    assert doc.metadata["file_hash"] == hashlib.sha256(doc.content).hexdigest()


def test_notion_sends_auth_headers():
    """每次 GET 带 Authorization: Bearer + Notion-Version 头。"""
    client, _p, _b = _notion_client("pg1")
    list(NotionConnector(token="secret", page_ids=["pg1"], client=client).iter_documents())
    assert client.calls
    for _url, hdr in client.calls:
        assert hdr.get("Authorization") == "Bearer secret"
        assert hdr.get("Notion-Version") == "2022-06-28"


def test_notion_deterministic_given_page_order():
    """按给定 page_ids 顺序产出。"""
    base = "https://api.notion.com/v1"
    responses = {}
    for pid in ("p1", "p2"):
        responses[f"{base}/pages/{pid}"] = _FakeResponse(b"", payload={"id": pid})
        responses[f"{base}/blocks/{pid}/children"] = _FakeResponse(b"", payload={"results": []})
    client = _FakeHttpClient(responses)
    conn = NotionConnector(token="t", page_ids=["p1", "p2"], client=client)
    assert [d.source_doc_id for d in conn.iter_documents()] == ["p1", "p2"]


def test_notion_factory_resolves():
    """make_source_connector('notion', token=..., page_ids=[...], client=fake) 解析到 NotionConnector。"""
    client, _p, _b = _notion_client("pg1")
    conn = make_source_connector("notion", token="t", page_ids=["pg1"], client=client)
    assert isinstance(conn, NotionConnector)
    assert [d.source_doc_id for d in conn.iter_documents()] == ["pg1"]


# ===========================================================================
# 惰性依赖：httpx 不在 import 面上（只在远程 connector 真正迭代时才需要）
# ===========================================================================
def test_httpx_is_a_lazy_dependency():
    """import connector + 建 InMemoryConnector 不触发 httpx import（离线默认零三方依赖）。"""
    import ragspine.ingestion.source.connector as connector_mod  # noqa: F401
    from ragspine.ingestion.source.memory import InMemoryConnector

    # 注入 client 的远程 connector 迭代也不需要 httpx（走注入客户端，不建真 httpx.Client）。
    url = "https://example.com/y.txt"
    fake = _FakeHttpClient({url: _FakeResponse(b"y", headers={})})
    list(HttpConnector([url], client=fake).iter_documents())
    assert isinstance(InMemoryConnector([]), SourceConnector)
