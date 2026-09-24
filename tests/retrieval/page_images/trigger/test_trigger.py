"""页图按需附图（ADR 0025）：模式 / 触发条件解析、tagged 选择、候选窗口、上限截断、去重、只删不增、
``on`` ≡ ``all`` 快照、trace 只记计数。"""

import hashlib
import json
import logging
import os
from dataclasses import replace
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import answer_question
from ragspine.agent.intent import ROUTE_NARRATIVE, RuleIntentParser
from ragspine.agent.llm_provider import MockProvider, split_message_content
from ragspine.agent.number_guard import NARRATIVE_NUMBER_GUARD_ENV
from ragspine.extraction.di_markdown.page_tags import PageTagStats
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever
from ragspine.retrieval.page_images.attach import PageImageRetriever
from ragspine.retrieval.page_images.store import PageImageStore, RenderedPage
from ragspine.retrieval.page_images.trigger.retriever import (
    DEFAULT_PAGE_IMAGES_TRIGGER,
    PageImageTriggerRetriever,
    make_page_images_policy,
    make_triggered_page_image_retriever,
    parse_page_image_trigger,
)
from ragspine.retrieval.page_images.trigger.tag_store import PageTagStore, clear_lazy_tag_cache
from ragspine.service.config import ServiceConfig, open_narrative_retriever
from ragspine.storage.fact_store import SqliteFactStore

from ...page_parent.conftest import load_page_corpus

_REF = date(2026, 9, 1)
# 页 1：长文本、无表无图（无标签）；页 2：有表；页 3：低文字。
_TAGS = [
    PageTagStats(1, False, 0, 0, 1200),
    PageTagStats(2, True, 0, 0, 900),
    PageTagStats(3, False, 1, 40, 120),
]


def _png(tag: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + tag.encode()


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_lazy_tag_cache()
    yield
    clear_lazy_tag_cache()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "knowledge.db"
    store = ChunkStore(path)
    load_page_corpus(store)
    store.close()
    images = PageImageStore(path)
    images.replace_doc(
        "deck.md",
        pdf_sha256="f" * 64,
        pdf_pages=3,
        dpi=144,
        max_side=1568,
        signature="s",
        pages=[RenderedPage(p, _png(f"deck-{p}"), 10, 5) for p in (1, 2, 3)],
    )
    images.close()
    tags = PageTagStore(path)
    tags.replace_doc("deck.md", _TAGS, md_sha256="a" * 64)
    tags.close()
    return path


class _Stub:
    """固定返回给定 snippets 的检索器（每次返回新副本）。"""

    def __init__(self, snippets):
        self.snippets = snippets

    def retrieve(self, query, *, filters=None, top_k=50):
        return [dict(s) for s in self.snippets]


def _snip(page: int, sha: str | None = None, doc: str = "deck.md", image: bool = True):
    s = {
        "doc_id": doc,
        "source_locator": f"{doc}@page={page}#para1",
        "text": f"page {page}",
    }
    if image:
        s["page_image"] = {
            "path": f"/x/{page}.png",
            "doc_id": doc,
            "page": page,
            "image_sha256": sha or f"sha{page}",
            "pdf_sha256": "f" * 64,
        }
    return s


def _images(snippets) -> list[int]:
    return [s["page_image"]["page"] for s in snippets if "page_image" in s]


# ---------------------------------------------------------------- 解析


def test_mode_parsing():
    assert make_page_images_policy(None) == "off"
    assert make_page_images_policy("") == "off"
    assert make_page_images_policy("none") == "off"
    assert make_page_images_policy("OFF") == "off"
    assert make_page_images_policy("on") == "all"
    assert make_page_images_policy("all") == "all"
    assert make_page_images_policy(" Tagged ") == "tagged"
    with pytest.raises(ValueError):
        make_page_images_policy("auto")


def test_trigger_parsing():
    assert DEFAULT_PAGE_IMAGES_TRIGGER == "has_table,low_text"
    assert parse_page_image_trigger("has_table,low_text") == frozenset({"has_table", "low_text"})
    assert parse_page_image_trigger(" HAS_FIGURE ") == frozenset({"has_figure"})
    assert parse_page_image_trigger("any") == frozenset({"has_table", "has_figure", "low_text"})
    for bad in ("", " , ", "has_chart", "any,foo"):
        with pytest.raises(ValueError):
            parse_page_image_trigger(bad)


def test_factory_off_and_all_without_max(db):
    base = _Stub([_snip(1)])
    assert make_triggered_page_image_retriever(base, "off", chunk_db_path=db) is base
    for spec in ("on", "all"):
        r = make_triggered_page_image_retriever(base, spec, chunk_db_path=db, page_parent="dedup")
        # all 不设上限：就是原来的 PageImageRetriever，不包层
        assert type(r) is PageImageRetriever
    r = make_triggered_page_image_retriever(
        base, "all", chunk_db_path=db, page_parent="dedup", max_images=1
    )
    assert isinstance(r, PageImageTriggerRetriever)
    r = make_triggered_page_image_retriever(base, "tagged", chunk_db_path=db, page_parent="dedup")
    assert isinstance(r, PageImageTriggerRetriever)
    assert r.max_images == 3  # 不设上限 = top_n


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_images": -1},
        {"low_text_chars": -1},
        {"figure_min_chars": -1},
        {"trigger": "nope"},
        {"top_n": -1},
    ],
)
def test_factory_rejects_invalid_values(db, kwargs):
    with pytest.raises(ValueError):
        make_triggered_page_image_retriever(_Stub([]), "tagged", chunk_db_path=db, **kwargs)


# ---------------------------------------------------------------- 选择


def _wrap(db, snippets, **kw):
    kw.setdefault("mode", "tagged")
    kw.setdefault("max_images", 10)
    return PageImageTriggerRetriever(_Stub(snippets), chunk_db_path=db, **kw)


def test_tagged_keeps_only_trigger_pages(db):
    out = _wrap(db, [_snip(1), _snip(2), _snip(3)]).retrieve("q")
    assert _images(out) == [2, 3]
    out = _wrap(db, [_snip(1), _snip(2), _snip(3)], trigger="has_table").retrieve("q")
    assert _images(out) == [2]
    out = _wrap(db, [_snip(1), _snip(2), _snip(3)], trigger="has_figure").retrieve("q")
    assert _images(out) == [3]
    # 阈值可配置：图文字 40 < 50 → 不算 has_figure；低文字阈值抬到 1000 → 页 2 也算 low_text
    out = _wrap(
        db, [_snip(1), _snip(2), _snip(3)], trigger="has_figure", figure_min_chars=50
    ).retrieve("q")
    assert _images(out) == []
    out = _wrap(
        db, [_snip(1), _snip(2), _snip(3)], trigger="low_text", low_text_chars=1000
    ).retrieve("q")
    assert _images(out) == [2, 3]


def test_max_truncates_in_rank_order(db):
    out = _wrap(db, [_snip(3), _snip(2), _snip(1)], mode="all", max_images=2).retrieve("q")
    assert _images(out) == [3, 2]
    out = _wrap(db, [_snip(3), _snip(2)], max_images=0).retrieve("q")
    assert _images(out) == []


def test_dedup_by_page_and_image_sha(db):
    snippets = [_snip(2), _snip(2), _snip(3, sha="same"), _snip(1, sha="same")]
    out = _wrap(db, snippets, mode="all").retrieve("q")
    assert [("page_image" in s) for s in out] == [True, False, True, False]


def test_untagged_doc_gets_no_image_in_tagged_mode(db):
    out = _wrap(db, [_snip(1, doc="other.md"), _snip(2)]).retrieve("q")
    assert [("page_image" in s) for s in out] == [False, True]
    # all 模式不看标签
    out = _wrap(db, [_snip(1, doc="other.md")], mode="all").retrieve("q")
    assert _images(out) == [1]


def test_only_removes_never_adds(db):
    snippets = [_snip(1), _snip(2, image=False), _snip(3), _snip(2)]
    for kw in ({}, {"mode": "all"}, {"trigger": "any"}, {"max_images": 1}):
        out = _wrap(db, snippets, **kw).retrieve("q")
        assert len(out) == len(snippets)
        for before, after in zip(snippets, out, strict=True):
            # 去掉 page_image 之外逐项不变；没图的绝不会多出图；有图的只可能原样保留或被去掉
            strip = {k: v for k, v in before.items() if k != "page_image"}
            assert {k: v for k, v in after.items() if k != "page_image"} == strip
            assert list(after) == list(before) or list(after) == list(strip)
            if "page_image" in after:
                assert after["page_image"] == before["page_image"]


def test_subset_of_real_page_image_retriever(db):
    store = ChunkStore(db)
    try:
        base = NarrativeIndexRetriever(NarrativeIndex(store, page_parent="dedup"))
        inner = PageImageRetriever(base, chunk_db_path=db, top_n=10, page_parent="dedup")
        full = inner.retrieve("Singapore VONB Hong Kong revenue")
        wrapped = PageImageTriggerRetriever(inner, chunk_db_path=db, mode="tagged", max_images=10)
        picked = wrapped.retrieve("Singapore VONB Hong Kong revenue")
    finally:
        store.close()
    assert len(picked) == len(full)
    for a, b in zip(full, picked, strict=True):
        assert {k: v for k, v in b.items() if k != "page_image"} == {
            k: v for k, v in a.items() if k != "page_image"
        }
        if "page_image" in b:
            assert b["page_image"] == a["page_image"]
    assert set(_images(picked)) <= set(_images(full))
    assert 1 not in _images(picked)  # 页 1 无标签


def test_trace_counts_only(db, caplog):
    with caplog.at_level(logging.INFO):
        _wrap(
            db,
            [_snip(1), _snip(2), _snip(3), _snip(3), _snip(1, sha="o1", doc="other.md")],
            max_images=1,
        ).retrieve("q")
    recs = [r for r in caplog.records if getattr(r, "op", "") == "narrative.page_image_trigger"]
    assert len(recs) == 1
    rec = recs[0]
    assert (rec.mode, rec.n_candidates, rec.n_kept) == ("tagged", 5, 1)
    assert rec.drop_reasons == {"dup": 1, "not_tagged": 1, "over_max": 1, "untagged": 1}
    assert rec.n_dropped == 4
    assert rec.trigger == ["has_table", "low_text"]
    assert rec.tag_sources == {"stored": 1, "untagged": 1}
    dumped = repr(rec.__dict__)
    assert ".png" not in dumped and "page 1" not in dumped and str(db.parent) not in dumped


# ---------------------------------------------------------------- 装配 + on ≡ all 快照


class _ImageRecorder(MockProvider):
    supports_image_input = True

    def __init__(self) -> None:
        super().__init__(reference_date=_REF)
        self.seen: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.seen.append(messages)
        text, _ = split_message_content(messages[-1]["content"])
        return super().chat([*messages[:-1], {"role": "user", "content": text}], tools=tools)


class _Forced:
    def parse(self, question, *, reference_date=None):
        parsed = RuleIntentParser().parse(question, reference_date=reference_date)
        return replace(parsed, route=ROUTE_NARRATIVE)


_QUESTIONS = ("Singapore VONB", "Hong Kong revenue", "Singapore growth outlook")
# 引入触发策略之前（main@0b27888）page_images="on" 的摘要；tmp 路径已归一。
_FROZEN_ON = "51e70ca9ae07a20ec229fce3c07cb09f97858b394a765c58bae354d1a006b7ea"


def _digest(db, **config_kwargs) -> str:
    facts = SqliteFactStore(db)
    facts.init_schema()
    dumps: list[object] = []
    try:
        for mode in ("dedup", "page+child"):
            config = ServiceConfig(
                db_path=str(db),
                chunk_db_path=str(db),
                embedding="none",
                page_parent=mode,
                **config_kwargs,
            )
            provider = _ImageRecorder()
            with open_narrative_retriever(config, provider) as retriever:
                for question in _QUESTIONS:
                    result = answer_question(
                        question,
                        facts,
                        provider,
                        reference_date=_REF,
                        narrative_retriever=retriever,
                        intent_parser=_Forced(),
                    )
                    dumps.append([result.answer, result.sources])
            dumps.append(provider.seen)
    finally:
        facts.close()
    payload = json.dumps(dumps, sort_keys=True, ensure_ascii=False, default=str)
    payload = payload.replace(str(db.parent.resolve()), "<tmp>").replace(str(db.parent), "<tmp>")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_on_and_all_are_byte_identical_to_frozen(db, monkeypatch):
    monkeypatch.setenv(NARRATIVE_NUMBER_GUARD_ENV, "off")
    assert _digest(db, page_images="on") == _FROZEN_ON
    assert _digest(db, page_images="all") == _FROZEN_ON
    # all 显式把上限设成 top_n：包了一层，但本语料没有重复图，输出仍一致
    assert _digest(db, page_images="all", page_images_max=3) == _FROZEN_ON


def test_service_config_tagged_sends_only_tagged_pages(db):
    provider = _ImageRecorder()
    config = ServiceConfig(
        db_path=str(db),
        chunk_db_path=str(db),
        embedding="none",
        page_parent="dedup",
        page_images="tagged",
        page_images_top_n=10,
    )
    facts = SqliteFactStore(db)
    facts.init_schema()
    try:
        with open_narrative_retriever(config, provider) as retriever:
            assert isinstance(retriever, PageImageTriggerRetriever)
            answer_question(
                "Singapore VONB Hong Kong revenue",
                facts,
                provider,
                reference_date=_REF,
                narrative_retriever=retriever,
                intent_parser=_Forced(),
            )
    finally:
        facts.close()
    _, parts = split_message_content(provider.seen[-1][-1]["content"])
    pages = [int(p["page"]) for p in parts]
    assert pages and 1 not in pages and set(pages) <= {2, 3}
