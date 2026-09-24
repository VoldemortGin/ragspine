"""图文混合上下文的组装：带页图引用的检索结果 → user 消息的 text + image 部件；不支持图片的 provider 降级计数。"""

import logging
import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import answer_question
from ragspine.agent.intent import ROUTE_NARRATIVE, RuleIntentParser
from ragspine.agent.llm_provider import (
    IMAGE_PART_TYPE,
    MockProvider,
    provider_supports_images,
    split_message_content,
)
from ragspine.storage.fact_store import SqliteFactStore

_REF = date(2026, 9, 1)


class _Forced:
    def parse(self, question, *, reference_date=None):
        from dataclasses import replace

        return replace(
            RuleIntentParser().parse(question, reference_date=reference_date), route=ROUTE_NARRATIVE
        )


class _Recording(MockProvider):
    def __init__(self) -> None:
        super().__init__(reference_date=_REF)
        self.seen: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.seen.append(messages)
        text, _ = split_message_content(messages[-1]["content"])
        return super().chat([*messages[:-1], {"role": "user", "content": text}], tools=tools)


class _ImageProvider(_Recording):
    supports_image_input = True


class _Retriever:
    def __init__(self, snippets):
        self.snippets = snippets

    def retrieve(self, query, *, filters=None, top_k=50):
        return [dict(s) for s in self.snippets]


def _snippet(doc: str, page: int, text: str, image: str | None = None) -> dict:
    s = {
        "text": text,
        "doc_id": doc,
        "source_locator": f"{doc}@page={page}#para1",
        "parent_locator": f"{doc}@page={page}",
        "chunk_id": f"{doc}#c{page}",
    }
    if image is not None:
        s["page_image"] = {
            "path": image,
            "doc_id": doc,
            "page": page,
            "image_sha256": "a" * 64,
            "pdf_sha256": "b" * 64,
        }
    return s


@pytest.fixture
def facts(tmp_path):
    store = SqliteFactStore(tmp_path / "facts.db")
    store.init_schema()
    yield store
    store.close()


def _ask(facts, provider, snippets):
    return answer_question(
        "What percentage of VONB came from Agency?",
        facts,
        provider,
        reference_date=_REF,
        narrative_retriever=_Retriever(snippets),
        intent_parser=_Forced(),
    )


def test_capability_probe():
    assert provider_supports_images(_ImageProvider())
    assert not provider_supports_images(MockProvider())
    assert not provider_supports_images(object())


def test_split_message_content():
    assert split_message_content("plain") == ("plain", [])
    parts = [
        {"type": "text", "text": "a"},
        {"type": IMAGE_PART_TYPE, "path": "/x/p1.png", "name": "p1.png"},
        {"type": "text", "text": "b"},
    ]
    text, images = split_message_content(parts)
    assert text == "a\nb"
    assert images == [parts[1]]


def test_text_and_images_are_paired(facts, tmp_path):
    img18 = tmp_path / "a.png"
    img18.write_bytes(b"png")
    img2 = tmp_path / "b.png"
    img2.write_bytes(b"png")
    provider = _ImageProvider()
    result = _ask(
        facts,
        provider,
        [
            _snippet("deck.md", 18, "Distribution Mix Agency 72%", str(img18)),
            _snippet("other.md", 18, "Other deck page 18", str(img2)),
            _snippet("deck.md", 3, "No image here"),
        ],
    )
    content = provider.seen[0][-1]["content"]
    assert isinstance(content, list)
    text = content[0]["text"]
    assert content[0]["type"] == "text"
    assert (
        "[1] Distribution Mix Agency 72%（来源：deck.md deck.md@page=18#para1）\n图：p18.png"
        in text
    )
    assert "[2] Other deck page 18（来源：other.md other.md@page=18#para1）\n图：p18-2.png" in text
    assert text.endswith("[3] No image here（来源：deck.md deck.md@page=3#para1）")
    assert "图：p3" not in text
    images = content[1:]
    assert [p["type"] for p in images] == [IMAGE_PART_TYPE, IMAGE_PART_TYPE]
    assert [(p["name"], p["doc_id"], p["page"], p["path"]) for p in images] == [
        ("p18.png", "deck.md", 18, str(img18)),
        ("p18-2.png", "other.md", 18, str(img2)),
    ]
    # 来源（provenance）不变：仍是块级 locator
    assert result.sources[0] == {"doc": "deck.md", "locator": "deck.md@page=18#para1"}


def test_unsupported_provider_gets_plain_text_and_a_counted_trace(facts, tmp_path, caplog):
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    with_images = [_snippet("deck.md", 18, "Agency 72%", str(img)), _snippet("deck.md", 3, "x")]
    without = [_snippet("deck.md", 18, "Agency 72%"), _snippet("deck.md", 3, "x")]

    plain = _Recording()
    _ask(facts, plain, without)
    degraded = _Recording()
    with caplog.at_level(logging.INFO):
        _ask(facts, degraded, with_images)

    assert degraded.seen == plain.seen  # 逐字节退回纯文本
    assert isinstance(degraded.seen[0][-1]["content"], str)
    rec = next(r for r in caplog.records if hasattr(r, "request_id"))
    assert rec.page_images == {"sent": 0, "dropped": 1, "dropped_reason": "provider_no_image_input"}
    assert str(tmp_path) not in repr(rec.__dict__)


def test_supported_provider_trace_counts_only(facts, tmp_path, caplog):
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    with caplog.at_level(logging.INFO):
        _ask(facts, _ImageProvider(), [_snippet("deck.md", 18, "Agency 72%", str(img))])
    rec = next(r for r in caplog.records if hasattr(r, "request_id"))
    assert rec.page_images == {"sent": 1, "dropped": 0, "dropped_reason": ""}
    assert str(tmp_path) not in repr(rec.__dict__)


def test_no_images_means_no_trace_field(facts, caplog):
    with caplog.at_level(logging.INFO):
        _ask(facts, _ImageProvider(), [_snippet("deck.md", 3, "x")])
    rec = next(r for r in caplog.records if hasattr(r, "request_id"))
    assert not hasattr(rec, "page_images")
