"""远程拉取型 SourceConnector：HttpConnector（通用 HTTP）+ NotionConnector（Notion REST）。

两者都把 httpx **惰性 import 进方法体**（绝不在模块顶层），使 `import
ragspine.ingestion.source.connector` 与建 FilesystemConnector / InMemoryConnector 的离线默认
路径【零三方依赖】——httpx 是 [connectors] extra 的延迟依赖，只有真正远程迭代时才需要。
测试可注入 fake client（暴露 `.get(url, headers=...)`）做离线确定性验证，不联网。

血缘口径（同 FilesystemConnector）：每个 RawDoc 带非空 source_doc_id + locator +
metadata['file_hash']（响应字节 sha256，口径同 extraction.compute_file_hash）。
"""

import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from ragspine.ingestion.source.connector import RawDoc

# Notion REST 固定 API 版本头（Notion 要求每次请求携带，锚定响应 schema）。
_NOTION_VERSION = "2022-06-28"


def _http_client(injected: Any) -> tuple[Any, bool]:
    """返回 (client, owns)：注入了就用注入的（owns=False）；否则惰性建 httpx.Client（owns=True）。

    httpx 在此**函数体内** import（延迟依赖），离线默认路径与注入 client 的测试都不触发它。
    owns=True 时调用方负责在迭代结束后 close，避免泄漏连接。
    """
    if injected is not None:
        return injected, False
    import httpx

    return httpx.Client(), True


def _doc_id_from_url(url: str) -> str:
    """从 URL 派生 source_doc_id：path 末段（文件名）；为空则回退到稳定 slug（非空、可回溯）。"""
    parsed = urlparse(url)
    name = PurePosixPath(parsed.path).name
    if name:
        return name
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{parsed.netloc}{parsed.path}").strip("-")
    return slug or hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


class HttpConnector:
    """通用 HTTP 拉取：对每个 URL GET 一次，产出带齐血缘的 RawDoc（按给定 URL 顺序）。

    source_doc_id = URL path 末段（缺则稳定 slug）；locator = URL 原串；content = 响应字节；
    content_type = 响应 Content-Type 头（缺则 URL 末段后缀）；metadata['file_hash'] = 字节 sha256。
    client 可注入（fake httpx.Client）做离线确定性测试；缺省惰性建真 httpx.Client。
    """

    def __init__(
        self,
        urls: Sequence[str],
        *,
        headers: Mapping[str, str] | None = None,
        client: Any | None = None,
    ) -> None:
        self._urls: tuple[str, ...] = tuple(urls)
        self._headers: dict[str, str] = dict(headers) if headers else {}
        self._client = client

    def iter_documents(self) -> Iterator[RawDoc]:
        """按给定 URL 顺序 GET 并惰性产出 RawDoc（确定性顺序 = 给定 URL 顺序）。"""
        client, owns = _http_client(self._client)
        try:
            for url in self._urls:
                resp = client.get(url, headers=self._headers)
                content: bytes = resp.content
                content_type = resp.headers.get("Content-Type") or ""
                source_doc_id = _doc_id_from_url(url)
                if not content_type:
                    content_type = PurePosixPath(urlparse(url).path).suffix.lower()
                yield RawDoc(
                    source_doc_id=source_doc_id,
                    locator=url,
                    content=content,
                    content_type=content_type,
                    metadata={"file_hash": hashlib.sha256(content).hexdigest()},
                )
        finally:
            if owns:
                client.close()


class NotionConnector:
    """Notion REST 拉取：对每个 page_id 取页面 + 其子 block，序列化为 JSON 字节产出 RawDoc。

    每个 page：GET {base_url}/pages/{id}（页面属性）+ {base_url}/blocks/{id}/children（正文块），
    合并成 {'page': …, 'blocks': …} 确定性序列化（sort_keys）为 content。
    source_doc_id = page_id；locator = 页面 url 字段（缺则 'notion://page/{id}'）；
    content_type = 'application/json'；metadata['file_hash'] = 字节 sha256。
    auth：每次请求带 Authorization: Bearer {token} + Notion-Version 头。client 可注入做离线测试。
    """

    def __init__(
        self,
        *,
        token: str,
        page_ids: Sequence[str],
        base_url: str = "https://api.notion.com/v1",
        client: Any | None = None,
    ) -> None:
        self._token = token
        self._page_ids: tuple[str, ...] = tuple(page_ids)
        self._base_url = base_url.rstrip("/")
        self._client = client

    def iter_documents(self) -> Iterator[RawDoc]:
        """按给定 page_ids 顺序拉取并惰性产出 RawDoc（确定性顺序 = 给定 page 顺序）。"""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": _NOTION_VERSION,
        }
        client, owns = _http_client(self._client)
        try:
            for page_id in self._page_ids:
                page = client.get(
                    f"{self._base_url}/pages/{page_id}", headers=headers
                ).json()
                blocks = client.get(
                    f"{self._base_url}/blocks/{page_id}/children", headers=headers
                ).json()
                content = json.dumps(
                    {"page": page, "blocks": blocks},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
                locator = (page or {}).get("url") or f"notion://page/{page_id}"
                yield RawDoc(
                    source_doc_id=page_id,
                    locator=locator,
                    content=content,
                    content_type="application/json",
                    metadata={"file_hash": hashlib.sha256(content).hexdigest()},
                )
        finally:
            if owns:
                client.close()
