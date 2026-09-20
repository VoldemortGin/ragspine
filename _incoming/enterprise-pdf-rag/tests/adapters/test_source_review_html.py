"""Navigation and source identity are independent of the optional AIA PDF corpus."""

from dataclasses import replace
from hashlib import sha256
from html.parser import HTMLParser

import pytest

from enterprise_pdf_rag.adapters.source_review_html import render_index, render_page
from enterprise_pdf_rag.documents.models import (
    AssetRef,
    DocumentManifest,
    DocumentSnapshot,
    PageRecord,
    RegionRecord,
    TextSidecar,
    TextSpan,
)

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 540"><path id="source-path" d="M0 0L10 10"/></svg>'


class _Links(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            value = dict(attrs).get("href")
            if value is not None:
                self.hrefs.append(value)


def _snapshot() -> DocumentSnapshot:
    source = AssetRef(sha256(b"source fixture").hexdigest(), "application/pdf", 14)
    svg = AssetRef(sha256(SVG.encode()).hexdigest(), "image/svg+xml", len(SVG.encode()))
    text = AssetRef(sha256(b"text").hexdigest(), "application/json", 4)
    pages = tuple(PageRecord(i, 960.0, 540.0, 0, svg, text, 1, ()) for i in range(71))
    region = RegionRecord(24, (731.5, 145.0, 944.2, 385.0), svg, svg, text, ())
    return DocumentSnapshot(
        "manifest-fixture",
        DocumentManifest(
            "source-document-v1",
            "source.pdf",
            source,
            "fixture-only",
            pages,
            region,
        ),
    )


def _text(
    snapshot: DocumentSnapshot, page_index: int, value: str = "source words"
) -> TextSidecar:
    return TextSidecar(
        "source-text-v1",
        snapshot.manifest.source.sha256,
        page_index,
        (TextSpan(f"p{page_index}", value, (10.0, 20.0, 90.0, 35.0)),),
    )


def test_index_links_all_pages_without_embedding_all_page_svgs() -> None:
    snapshot = _snapshot()
    html = render_index(
        snapshot=snapshot, region_svg=SVG, region_text=_text(snapshot, 24)
    )
    links = _Links(html).hrefs

    assert {f"pages/page-{number:03d}.html" for number in range(1, 72)} <= set(links)
    assert html.count("<svg ") == 1
    assert "重点选区" in html and "第 25 页" in html
    assert "71 页" in html
    assert "基线" in html
    assert "visual_completeness: pending" in html
    assert "span_to_svg_mapping: pending" in html
    assert "semantics: pending" in html


def test_page_keeps_its_svg_text_identity_and_adjacent_navigation() -> None:
    snapshot = _snapshot()
    html = render_page(
        snapshot=snapshot, page_index=24, svg=SVG, text=_text(snapshot, 24, "49.00")
    )
    links = _Links(html).hrefs

    assert SVG in html
    assert "第 25 页" in html and "共 71 页" in html
    assert "49.00" in html and snapshot.manifest.source.sha256 in html
    assert {
        "page-024.html",
        "page-026.html",
        "../review.html",
        "../source.pdf#page=25",
    } <= set(links)
    assert "不是 LLM 描述" in html


@pytest.mark.parametrize(
    ("page_index", "present", "absent"),
    [
        (0, "page-002.html", "page-000.html"),
        (70, "page-070.html", "page-072.html"),
    ],
)
def test_first_and_last_page_do_not_link_outside_the_document(
    page_index: int, present: str, absent: str
) -> None:
    snapshot = _snapshot()
    links = _Links(
        render_page(
            snapshot=snapshot,
            page_index=page_index,
            svg=SVG,
            text=_text(snapshot, page_index),
        )
    ).hrefs
    assert present in links
    assert absent not in links


def test_source_text_and_filename_are_html_escaped() -> None:
    snapshot = _snapshot()
    snapshot = replace(
        snapshot,
        manifest=replace(
            snapshot.manifest, filename='<script>alert("filename")</script>.pdf'
        ),
    )
    html = render_page(
        snapshot=snapshot,
        page_index=0,
        svg=SVG,
        text=_text(snapshot, 0, '<script>alert("source")</script> & <value>'),
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; &lt;value&gt;" in html


@pytest.mark.parametrize("wrong_source", [False, True])
def test_page_rejects_mislabeled_source_text(wrong_source: bool) -> None:
    snapshot = _snapshot()
    text = _text(snapshot, 24)
    text = (
        replace(text, source_sha256="f" * 64)
        if wrong_source
        else replace(text, page_index=23)
    )
    with pytest.raises(ValueError, match="identity"):
        render_page(snapshot=snapshot, page_index=24, svg=SVG, text=text)
