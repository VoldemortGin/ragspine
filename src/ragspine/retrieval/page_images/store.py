"""页图映射表 + 内容寻址的 PNG 文件。

表建在块库同一个 sqlite 里（只在第一次真的关联了 PDF 时才建，未关联的库一张表都不多）：

- ``page_image_doc``：每个 doc 一行，记录 PDF sha256、页数、渲染参数（dpi / max_side）和同步签名；
  签名不变即幂等跳过。
- ``page_image``：``(doc_id, page)`` → 图片相对路径、图片 sha256、PDF sha256、dpi、宽高。

PNG 按内容寻址落在 ``image_dir/<sha256 前两位>/<sha256>.png``（默认 ``<块库目录>/page_images``）；
页码一律是物理页序（1 起），与块 locator 的 ``page=N`` 一致。替换或清除某个 doc 时，只删除不再被
任何行引用的文件（多个 doc 可能共用同一张图）。
"""

import hashlib
import os
import sqlite3
import weakref
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PAGE_IMAGE_DIRNAME = "page_images"


@dataclass(frozen=True)
class RenderedPage:
    """一页渲染结果（入库写入的输入）。"""

    page: int
    png: bytes
    width: int
    height: int


@dataclass(frozen=True)
class PageImage:
    """映射表里的一页图（provenance：doc_id + 物理页码 + 来源 PDF 的 sha256）。"""

    doc_id: str
    page: int
    path: Path
    image_sha256: str
    pdf_sha256: str
    dpi: int
    width: int
    height: int


def default_page_image_dir(chunk_db_path: str | Path) -> Path:
    """默认页图目录：块库同目录下的 ``page_images/``。"""
    return Path(chunk_db_path).parent / PAGE_IMAGE_DIRNAME


class PageImageStore:
    """页图映射表的读写（与 ChunkStore 同库，独立连接）。"""

    def __init__(self, db_path: str | Path, image_dir: str | Path | None = None):
        self.db_path = str(db_path)
        self.image_dir = (
            Path(image_dir) if image_dir is not None else default_page_image_dir(db_path)
        )
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._finalizer = weakref.finalize(self, self._conn.close)

    def has_schema(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'page_image'"
        ).fetchone()
        return row is not None

    def init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS page_image_doc (
                doc_id      TEXT PRIMARY KEY,
                pdf_sha256  TEXT NOT NULL,
                pdf_pages   INTEGER NOT NULL,
                dpi         INTEGER NOT NULL,
                max_side    INTEGER NOT NULL,
                signature   TEXT NOT NULL,
                indexed_at  TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS page_image (
                doc_id        TEXT NOT NULL,
                page          INTEGER NOT NULL,
                image_path    TEXT NOT NULL,
                image_sha256  TEXT NOT NULL,
                pdf_sha256    TEXT NOT NULL,
                dpi           INTEGER NOT NULL,
                width         INTEGER NOT NULL,
                height        INTEGER NOT NULL,
                PRIMARY KEY (doc_id, page)
            )
            """
        )
        self._conn.commit()

    def doc_signature(self, doc_id: str) -> str | None:
        if not self.has_schema():
            return None
        row = self._conn.execute(
            "SELECT signature FROM page_image_doc WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return None if row is None else str(row["signature"])

    def replace_doc(
        self,
        doc_id: str,
        *,
        pdf_sha256: str,
        pdf_pages: int,
        dpi: int,
        max_side: int,
        signature: str,
        pages: Iterable[RenderedPage],
    ) -> int:
        """整体替换一个 doc 的页图；返回写入页数。先落文件再换行，最后清理孤儿文件。"""
        self.init_schema()
        rows = []
        for page in pages:
            sha = hashlib.sha256(page.png).hexdigest()
            rel = f"{sha[:2]}/{sha}.png"
            self._write_blob(self.image_dir / rel, page.png)
            rows.append((doc_id, page.page, rel, sha, pdf_sha256, dpi, page.width, page.height))
        old = self._doc_paths(doc_id)
        self._conn.execute("DELETE FROM page_image WHERE doc_id = ?", (doc_id,))
        self._conn.executemany(
            "INSERT INTO page_image (doc_id, page, image_path, image_sha256, pdf_sha256, dpi, "
            "width, height) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO page_image_doc (doc_id, pdf_sha256, pdf_pages, dpi, max_side, "
            "signature, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                pdf_sha256,
                pdf_pages,
                dpi,
                max_side,
                signature,
                datetime.now(UTC).isoformat(),
            ),
        )
        self._conn.commit()
        self._remove_orphans(old)
        return len(rows)

    def clear_doc(self, doc_id: str) -> int:
        """撤下一个 doc 的全部页图；返回删掉的行数（没有表 / 没有行时为 0，且不建表）。"""
        if not self.has_schema():
            return 0
        old = self._doc_paths(doc_id)
        cur = self._conn.execute("DELETE FROM page_image WHERE doc_id = ?", (doc_id,))
        doc_rows = self._conn.execute("DELETE FROM page_image_doc WHERE doc_id = ?", (doc_id,))
        self._conn.commit()
        self._remove_orphans(old)
        return max(cur.rowcount, doc_rows.rowcount, 0)

    def get(self, doc_id: str, page: int) -> PageImage | None:
        if not self.has_schema():
            return None
        row = self._conn.execute(
            "SELECT * FROM page_image WHERE doc_id = ? AND page = ?", (doc_id, page)
        ).fetchone()
        return None if row is None else self._to_image(row)

    def list_doc(self, doc_id: str) -> list[PageImage]:
        if not self.has_schema():
            return []
        rows = self._conn.execute(
            "SELECT * FROM page_image WHERE doc_id = ? ORDER BY page", (doc_id,)
        ).fetchall()
        return [self._to_image(r) for r in rows]

    def close(self) -> None:
        self._finalizer()

    def _to_image(self, row: sqlite3.Row) -> PageImage:
        return PageImage(
            doc_id=row["doc_id"],
            page=int(row["page"]),
            path=self.image_dir / row["image_path"],
            image_sha256=row["image_sha256"],
            pdf_sha256=row["pdf_sha256"],
            dpi=int(row["dpi"]),
            width=int(row["width"]),
            height=int(row["height"]),
        )

    def _doc_paths(self, doc_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT image_path FROM page_image WHERE doc_id = ?", (doc_id,)
        ).fetchall()
        return {str(r["image_path"]) for r in rows}

    def _remove_orphans(self, candidates: set[str]) -> None:
        for rel in sorted(candidates):
            still = self._conn.execute(
                "SELECT 1 FROM page_image WHERE image_path = ? LIMIT 1", (rel,)
            ).fetchone()
            if still is None:
                (self.image_dir / rel).unlink(missing_ok=True)

    @staticmethod
    def _write_blob(path: Path, data: bytes) -> None:
        if path.is_file():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
