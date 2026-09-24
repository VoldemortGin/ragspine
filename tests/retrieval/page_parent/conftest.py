"""页级父子 + 按页去重的共享语料：手工构造的块，覆盖同页多块（含段落回带重叠）、同页多段、
同页 RESTRICTED 块、整页 RESTRICTED 文档、以及没有页码的旧式 locator。"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import Chunk

SECRET = "Confidential Singapore VONB acquisition plan codename Falcon."


def _chunk(doc: str, seq: int, text: str, locator: str, p0: int, p1: int, **kw) -> Chunk:
    return Chunk(
        chunk_id=f"{doc}#c{seq}",
        doc_id=doc,
        seq=seq,
        text=text,
        source_locator=locator,
        para_start=p0,
        para_end=p1,
        title=doc,
        **kw,
    )


def page_corpus() -> dict[str, list[Chunk]]:
    """{doc_id: chunks}。deck.md 的第 1 页有三块：c0/c1 同段、c1 以 c0 的末段开头（回带重叠），
    c2 是同页的下一段（段号重新从 1 起）。第 2 页混有一块 RESTRICTED。"""
    deck = "deck.md"
    return {
        deck: [
            _chunk(
                deck,
                0,
                "Singapore revenue grew 10 percent.\nVONB rose strongly in Singapore.",
                f"{deck}@page=1#para1-2",
                1,
                2,
            ),
            _chunk(
                deck,
                1,
                "VONB rose strongly in Singapore.\nMargins improved in Hong Kong.",
                f"{deck}@page=1#para2-3",
                2,
                3,
            ),
            _chunk(
                deck,
                2,
                "Outlook\nWe expect Singapore growth to continue.",
                f"{deck}@page=1#para1-2",
                1,
                2,
            ),
            _chunk(deck, 3, "Singapore VONB | 1H26: 500", f"{deck}@page=2#para1", 1, 1),
            _chunk(
                deck,
                4,
                SECRET,
                f"{deck}@page=2#para2",
                2,
                2,
                sensitivity="RESTRICTED",
            ),
            _chunk(deck, 5, "Hong Kong revenue commentary.", f"{deck}@page=3#para1", 1, 1),
        ],
        "legacy.pdf": [
            _chunk(
                "legacy.pdf",
                0,
                "Singapore revenue legacy narrative.",
                "legacy.pdf#para1",
                1,
                1,
            ),
            _chunk(
                "legacy.pdf",
                1,
                "Singapore VONB legacy table.",
                "legacy.pdf#para2",
                2,
                2,
            ),
        ],
        "secret.md": [
            _chunk(
                "secret.md",
                0,
                "Singapore VONB restricted board memo.",
                "secret.md@page=1#para1",
                1,
                1,
                sensitivity="RESTRICTED",
            ),
        ],
    }


def load_page_corpus(store: ChunkStore) -> None:
    store.init_schema()
    for doc_id, chunks in page_corpus().items():
        store.replace_doc_chunks(doc_id, chunks)


@pytest.fixture
def page_store(tmp_path):
    store = ChunkStore(tmp_path / "chunks.db")
    load_page_corpus(store)
    yield store
    store.close()
