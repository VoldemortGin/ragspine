"""图文混合上下文的 RESTRICTED 隔离不变量绑定（conformance）。

页图是整页内容，范围比出口处理的单个块更大：只要一页里有任何 RESTRICTED 块，这一页的图就绝不能进 prompt。

钉死（页级父子 dedup / page+child 两种模式，端到端 open_narrative_retriever → answer_question）：
    - 同页有 RESTRICTED 块时，即使映射表里有这一页的图（例如敏感度在渲染之后才改），也不发图；
    - 同页公开块的文本照常进上下文（隔离的是图，不是整页）；
    - 反向证明：同一语料把那一块改成 INTERNAL，这一页的图就会发出去（测试有牙齿）。
"""

import os
from dataclasses import replace
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import answer_question
from ragspine.agent.intent import ROUTE_NARRATIVE, RuleIntentParser
from ragspine.agent.llm_provider import MockProvider, split_message_content
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.page_images.store import PageImageStore, RenderedPage
from ragspine.service.config import ServiceConfig, open_narrative_retriever
from ragspine.storage.fact_store import SqliteFactStore

SECRET = "Falcon acquisition Singapore VONB board plan."
MODES = ("dedup", "page+child")
_REF = date(2026, 9, 1)


class _Forced:
    def parse(self, question, *, reference_date=None):
        parsed = RuleIntentParser().parse(question, reference_date=reference_date)
        return replace(parsed, route=ROUTE_NARRATIVE)


class _ImageRecorder(MockProvider):
    supports_image_input = True

    def __init__(self) -> None:
        super().__init__(reference_date=_REF)
        self.seen: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.seen.append(messages)
        text, _ = split_message_content(messages[-1]["content"])
        return super().chat([*messages[:-1], {"role": "user", "content": text}], tools=tools)


def _chunk(seq: int, text: str, page: int, sensitivity: str = "INTERNAL") -> Chunk:
    return Chunk(
        chunk_id=f"deck.md#c{seq}",
        doc_id="deck.md",
        seq=seq,
        text=text,
        source_locator=f"deck.md@page={page}#para{seq}",
        para_start=seq,
        para_end=seq,
        sensitivity=sensitivity,
    )


def _setup(tmp_path, secret_sensitivity: str):
    db = tmp_path / "knowledge.db"
    store = ChunkStore(db)
    store.init_schema()
    store.replace_doc_chunks(
        "deck.md",
        [
            _chunk(0, "Singapore VONB overview.", 4),
            _chunk(1, SECRET, 4, sensitivity=secret_sensitivity),
            _chunk(2, "Hong Kong outlook.", 5),
        ],
    )
    store.close()
    images = PageImageStore(db)
    images.replace_doc(
        "deck.md",
        pdf_sha256="0" * 64,
        pdf_pages=5,
        dpi=144,
        max_side=1568,
        signature="pre-existing",
        pages=[RenderedPage(p, b"\x89PNG" + bytes([p]), 8, 4) for p in (4, 5)],
    )
    images.close()
    facts = SqliteFactStore(db)
    facts.init_schema()
    return db, facts


def _ask(db, facts, mode: str) -> _ImageRecorder:
    provider = _ImageRecorder()
    config = ServiceConfig(
        db_path=str(db),
        chunk_db_path=str(db),
        embedding="none",
        page_parent=mode,
        page_images="on",
        page_images_top_n=5,
    )
    with open_narrative_retriever(config, provider) as retriever:
        answer_question(
            "Singapore VONB overview",
            facts,
            provider,
            reference_date=_REF,
            narrative_retriever=retriever,
            intent_parser=_Forced(),
        )
    return provider


def _sent_pages(provider: _ImageRecorder) -> list[int]:
    pages: list[int] = []
    for messages in provider.seen:
        _, parts = split_message_content(messages[-1]["content"])
        pages.extend(int(p["page"]) for p in parts)
    return pages


@pytest.mark.parametrize("mode", MODES)
def test_page_with_restricted_chunk_never_sends_its_image(tmp_path, mode):
    db, facts = _setup(tmp_path, "RESTRICTED")
    try:
        provider = _ask(db, facts, mode)
    finally:
        facts.close()
    assert provider.seen, "叙事路应调用 provider"
    assert 4 not in _sent_pages(provider)
    for messages in provider.seen:
        text, _ = split_message_content(messages[-1]["content"])
        assert "Falcon" not in text
    text, _ = split_message_content(provider.seen[-1][-1]["content"])
    assert "Singapore VONB overview." in text  # 公开的同页文本照常可用


@pytest.mark.parametrize("mode", MODES)
def test_reverse_proof_same_page_image_is_sent_when_not_restricted(tmp_path, mode):
    db, facts = _setup(tmp_path, "INTERNAL")
    try:
        provider = _ask(db, facts, mode)
    finally:
        facts.close()
    assert 4 in _sent_pages(provider)
