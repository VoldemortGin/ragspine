"""标题进索引（RAGSPINE_CONTEXTUAL_INDEX）的共享语料：DI markdown 风格的按页块，带标题路径。

第 1 页：一个只有标题文字的块、一个图表块（正文只剩标签和数字，"Distribution Mix" 只出现在标题里）、
一段叙述；第 2 页：一张表 + 一块 RESTRICTED（它的标题含机密代号 Falcon）；另有一个没有页码、没有标题的旧式块。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import Chunk

DOC = "deck.md"
SECRET_HEADING = "Project Falcon Acquisition"
FIGURE_HEADING = "New Business Profile > Distribution Mix"


def _chunk(doc: str, seq: int, text: str, locator: str, heading: str = "", **kw) -> Chunk:
    return Chunk(
        chunk_id=f"{doc}#c{seq}",
        doc_id=doc,
        seq=seq,
        text=text,
        source_locator=locator,
        para_start=1,
        para_end=1,
        title=doc.split(".")[0],
        heading=heading,
        **kw,
    )


def heading_corpus() -> dict[str, list[Chunk]]:
    return {
        DOC: [
            _chunk(DOC, 0, "New Business Profile", f"{DOC}@page=1#para1", "New Business Profile"),
            _chunk(
                DOC,
                1,
                "Agency\n72%\nPartnerships\n28%\n2,928",
                f"{DOC}@page=1#para1-5",
                FIGURE_HEADING,
            ),
            _chunk(
                DOC,
                2,
                "Singapore revenue grew 10 percent on higher sales.",
                f"{DOC}@page=1#para1",
                "New Business Profile > Commentary",
            ),
            _chunk(
                DOC,
                3,
                "Singapore VONB | 1H26: 500",
                f"{DOC}@page=2#para1",
                "Segment Results > Singapore",
            ),
            _chunk(
                DOC,
                4,
                "Board memo on a pending deal.",
                f"{DOC}@page=2#para2",
                SECRET_HEADING,
                sensitivity="RESTRICTED",
            ),
        ],
        "legacy.pdf": [
            _chunk("legacy.pdf", 0, "Singapore revenue legacy narrative.", "legacy.pdf#para1"),
        ],
    }


def load_heading_corpus(store: ChunkStore) -> None:
    store.init_schema()
    for doc_id, chunks in heading_corpus().items():
        store.replace_doc_chunks(doc_id, chunks)


@pytest.fixture
def heading_store(tmp_path):
    store = ChunkStore(tmp_path / "chunks.db")
    load_heading_corpus(store)
    yield store
    store.close()
