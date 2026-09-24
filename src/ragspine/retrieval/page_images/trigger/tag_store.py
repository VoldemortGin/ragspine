"""页标签表 ``page_tag`` + 旧库的懒计算（只读、进程内缓存）。

表建在块库同一个 sqlite 里，写法照 ``page_image``：第一次真的写入时才建表，未关联 PDF 的库一张表都不多。
每行是一页的原始度量（:class:`~ragspine.extraction.di_markdown.page_tags.PageTagStats`），外加
``md_sha256`` + ``tags_version`` 作为签名；标签在检索时按当前阈值现算，改阈值不必重新入库。
写入方是入库侧 ``ingestion/page_images/index.py``（关联了 source PDF 的 ``.md``）。

旧库（引入标签之前入库、查不到 ``page_tag`` 行）：读叙事台账 ``narrative_doc`` 的 ``source_path`` 与
``file_hash``；文件还在且哈希一致就解析一遍，结果只放进进程内缓存（键 ``(doc_id, file_hash)``），**不回写**，
让查询路径保持只读（服务端的库归 worker 所有）。文件不在或哈希不一致，这个 doc 视为「无标签」。
"""

import hashlib
import sqlite3
import threading
import weakref
from collections.abc import Iterable
from pathlib import Path

from ragspine.extraction.di_markdown.page_tags import (
    PAGE_TAGS_VERSION,
    PageTagStats,
    document_tag_stats,
)
from ragspine.extraction.di_markdown.parse import parse_di_markdown

TAG_SOURCE_STORED = "stored"
TAG_SOURCE_LAZY = "lazy"
TAG_SOURCE_UNTAGGED = "untagged"

DocTags = dict[int, PageTagStats]

_lazy_cache: dict[tuple[str, str], DocTags] = {}
_lazy_lock = threading.Lock()


def tag_signature(md_sha256: str, tags_version: int = PAGE_TAGS_VERSION) -> str:
    return f"{md_sha256}|v{tags_version}"


def clear_lazy_tag_cache() -> None:
    """清空懒计算的进程内缓存（测试用）。"""
    with _lazy_lock:
        _lazy_cache.clear()


class PageTagStore:
    """``page_tag`` 表的读写（与 ChunkStore 同库，独立连接）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._finalizer = weakref.finalize(self, self._conn.close)

    def _has_table(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def has_schema(self) -> bool:
        return self._has_table("page_tag")

    def init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS page_tag (
                doc_id            TEXT NOT NULL,
                page              INTEGER NOT NULL,
                has_table         INTEGER NOT NULL,
                n_figures         INTEGER NOT NULL,
                figure_max_chars  INTEGER NOT NULL,
                text_chars        INTEGER NOT NULL,
                md_sha256         TEXT NOT NULL,
                tags_version      INTEGER NOT NULL,
                PRIMARY KEY (doc_id, page)
            )
            """
        )
        self._conn.commit()

    def doc_signature(self, doc_id: str) -> str | None:
        if not self.has_schema():
            return None
        row = self._conn.execute(
            "SELECT md_sha256, tags_version FROM page_tag WHERE doc_id = ? LIMIT 1", (doc_id,)
        ).fetchone()
        return None if row is None else tag_signature(row["md_sha256"], int(row["tags_version"]))

    def replace_doc(
        self,
        doc_id: str,
        stats: Iterable[PageTagStats],
        *,
        md_sha256: str,
        tags_version: int = PAGE_TAGS_VERSION,
    ) -> int:
        """整体替换一个 doc 的标签行；返回写入页数。"""
        self.init_schema()
        rows = [
            (
                doc_id,
                s.page,
                int(s.has_table),
                s.n_figures,
                s.figure_max_chars,
                s.text_chars,
                md_sha256,
                tags_version,
            )
            for s in stats
        ]
        self._conn.execute("DELETE FROM page_tag WHERE doc_id = ?", (doc_id,))
        self._conn.executemany(
            "INSERT INTO page_tag (doc_id, page, has_table, n_figures, figure_max_chars, "
            "text_chars, md_sha256, tags_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def clear_doc(self, doc_id: str) -> int:
        """删掉一个 doc 的标签行；没有表时返回 0 且不建表。"""
        if not self.has_schema():
            return 0
        cur = self._conn.execute("DELETE FROM page_tag WHERE doc_id = ?", (doc_id,))
        self._conn.commit()
        return max(cur.rowcount, 0)

    def get_doc(self, doc_id: str) -> DocTags | None:
        """``{page: PageTagStats}``；没有表或没有行时返回 None（不建表）。"""
        if not self.has_schema():
            return None
        rows = self._conn.execute(
            "SELECT * FROM page_tag WHERE doc_id = ? ORDER BY page", (doc_id,)
        ).fetchall()
        if not rows:
            return None
        return {
            int(r["page"]): PageTagStats(
                page=int(r["page"]),
                has_table=bool(r["has_table"]),
                n_figures=int(r["n_figures"]),
                figure_max_chars=int(r["figure_max_chars"]),
                text_chars=int(r["text_chars"]),
            )
            for r in rows
        }

    def registered_source(self, doc_id: str) -> tuple[str, str] | None:
        """叙事台账里登记的 ``(source_path, file_hash)``；没有台账或没登记时返回 None（只读）。"""
        if not self._has_table("narrative_doc"):
            return None
        row = self._conn.execute(
            "SELECT source_path, file_hash FROM narrative_doc WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        if row is None or not row["source_path"]:
            return None
        return str(row["source_path"]), str(row["file_hash"])

    def close(self) -> None:
        self._finalizer()


def compute_markdown_tags(md_path: str | Path) -> tuple[PageTagStats, ...]:
    """解析 markdown 文件，返回逐页原始度量。"""
    return document_tag_stats(parse_di_markdown(Path(md_path).read_text(encoding="utf-8")))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def load_doc_tags(store: PageTagStore, doc_id: str) -> tuple[DocTags | None, str]:
    """一个 doc 的标签与来源：表里有行 → ``stored``；旧库懒算成功 → ``lazy``；否则 ``(None, untagged)``。"""
    stored = store.get_doc(doc_id)
    if stored is not None:
        return stored, TAG_SOURCE_STORED
    registered = store.registered_source(doc_id)
    if registered is None:
        return None, TAG_SOURCE_UNTAGGED
    source_path, file_hash = registered
    key = (doc_id, file_hash)
    with _lazy_lock:
        cached = _lazy_cache.get(key)
    if cached is not None:
        return cached, TAG_SOURCE_LAZY
    path = Path(source_path)
    try:
        if not path.is_file() or _file_sha256(path) != file_hash:
            return None, TAG_SOURCE_UNTAGGED
        tags = {s.page: s for s in compute_markdown_tags(path)}
    except (OSError, UnicodeDecodeError):
        return None, TAG_SOURCE_UNTAGGED
    with _lazy_lock:
        _lazy_cache[key] = tags
    return tags, TAG_SOURCE_LAZY
