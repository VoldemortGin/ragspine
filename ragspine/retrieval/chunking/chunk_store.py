"""叙事块库（sqlite，模式仿 fact_store：显式 schema、参数化 SQL、execute_read 只读入口）。

存切块结果 + 文档元数据 + valid_as_of / ingested_at / 版本。同 doc 重新入库走版本替换：
旧版本整体置 inactive、新版本整体写入（幂等，活跃集始终等于最近一次入库的块集）。
检索层通过 iter_chunks 的元数据过滤拿活跃块（预过滤在打分之前）。
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ragspine.retrieval.chunking.chunking import Chunk


@dataclass
class StoredChunk:
    """库内的一个块：Chunk 全部字段 + 入库元信息。

    字段语义约定：
        valid_as_of: 该文档内容的业务时效日期（调用方传入，ISO 串，可为空）。
        ingested_at: 入库时间（UTC ISO 串，库内生成）。
        version:     同 doc 的入库版本（1 起，重新入库递增）。
        active:      是否当前活跃版本（旧版本失效后保留可溯源）。
    """

    chunk_id: str
    doc_id: str
    seq: int
    text: str
    source_locator: str
    para_start: int
    para_end: int
    title: str = ""
    topic: str = ""
    entity: str = ""
    geography: str = ""
    period: str = ""
    language: str = ""
    sensitivity: str = "INTERNAL"
    valid_as_of: str = ""
    ingested_at: str = ""
    version: int = 1
    active: bool = True


class ChunkStore:
    """narrative_chunk 表的读写。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        """建块表 + 索引（幂等）。"""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS narrative_chunk (
                chunk_id       TEXT NOT NULL,
                doc_id         TEXT NOT NULL,
                seq            INTEGER NOT NULL,
                text           TEXT NOT NULL,
                source_locator TEXT NOT NULL,
                para_start     INTEGER NOT NULL,
                para_end       INTEGER NOT NULL,
                title          TEXT NOT NULL DEFAULT '',
                topic          TEXT NOT NULL DEFAULT '',
                entity         TEXT NOT NULL DEFAULT '',
                geography      TEXT NOT NULL DEFAULT '',
                period         TEXT NOT NULL DEFAULT '',
                language       TEXT NOT NULL DEFAULT '',
                sensitivity    TEXT NOT NULL DEFAULT 'INTERNAL',
                valid_as_of    TEXT NOT NULL DEFAULT '',
                ingested_at    TEXT NOT NULL,
                version        INTEGER NOT NULL,
                active         INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_narrative_chunk
            ON narrative_chunk (doc_id, version, seq)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_narrative_chunk_active
            ON narrative_chunk (active, doc_id, seq)
            """
        )
        self._conn.commit()

    def replace_doc_chunks(
        self, doc_id: str, chunks: list[Chunk], valid_as_of: str = ""
    ) -> int:
        """同一文档的块整体替换入库（幂等重入）。

        旧活跃版本置 inactive，新块以 version=旧最大+1、active=1 写入；
        传空列表等价于把该文档从活跃集中撤下。返回写入条数。
        """
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM narrative_chunk WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        version = int(row[0]) + 1
        ingested_at = datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            "UPDATE narrative_chunk SET active = 0 WHERE doc_id = ? AND active = 1",
            (doc_id,),
        )
        self._conn.executemany(
            """
            INSERT INTO narrative_chunk (
                chunk_id, doc_id, seq, text, source_locator, para_start, para_end,
                title, topic, entity, geography, period, language, sensitivity,
                valid_as_of, ingested_at, version, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            [
                (
                    c.chunk_id, c.doc_id, c.seq, c.text, c.source_locator,
                    c.para_start, c.para_end, c.title, c.topic, c.entity,
                    c.geography, c.period, c.language, c.sensitivity,
                    valid_as_of, ingested_at, version,
                )
                for c in chunks
            ],
        )
        self._conn.commit()
        return len(chunks)

    def iter_chunks(
        self,
        *,
        doc_id: str | None = None,
        topic: str | None = None,
        entity: str | None = None,
        geography: str | None = None,
        period: str | None = None,
        language: str | None = None,
        include_inactive: bool = False,
    ) -> list[StoredChunk]:
        """按元数据任意组合过滤（AND）遍历块，默认只看活跃版本。

        结果按 (doc_id, seq) 排序，供检索层做打分前预过滤。
        """
        conds: list[str] = []
        params: list[str] = []
        for col, val in (
            ("doc_id", doc_id),
            ("topic", topic),
            ("entity", entity),
            ("geography", geography),
            ("period", period),
            ("language", language),
        ):
            if val is not None:
                conds.append(f"{col} = ?")
                params.append(val)
        if not include_inactive:
            conds.append("active = 1")
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        rows = self._conn.execute(
            f"SELECT * FROM narrative_chunk{where} ORDER BY doc_id, seq, version",
            tuple(params),
        ).fetchall()
        return [self._to_stored(r) for r in rows]

    @staticmethod
    def _to_stored(row: sqlite3.Row) -> StoredChunk:
        return StoredChunk(
            chunk_id=row["chunk_id"],
            doc_id=row["doc_id"],
            seq=row["seq"],
            text=row["text"],
            source_locator=row["source_locator"],
            para_start=row["para_start"],
            para_end=row["para_end"],
            title=row["title"],
            topic=row["topic"],
            entity=row["entity"],
            geography=row["geography"],
            period=row["period"],
            language=row["language"],
            sensitivity=row["sensitivity"],
            valid_as_of=row["valid_as_of"],
            ingested_at=row["ingested_at"],
            version=row["version"],
            active=bool(row["active"]),
        )

    def count(self, include_inactive: bool = False) -> int:
        """块条数（默认仅活跃）。"""
        where = "" if include_inactive else " WHERE active = 1"
        row = self._conn.execute(f"SELECT COUNT(*) FROM narrative_chunk{where}").fetchone()
        return int(row[0])

    def execute_read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """只读查询入口：跑参数化 SELECT 返回行列表（与 fact_store 约定一致）。"""
        return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self._conn.close()
