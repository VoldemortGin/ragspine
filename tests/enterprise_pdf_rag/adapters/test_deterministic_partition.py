"""确定性切分器: 文本页零模型调用、同 schema 产物; 拿不准的页带原因码回退模型."""

import base64
from dataclasses import replace
from pathlib import Path
from typing import cast

import pdfspine
import pytest

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.deterministic_partition import (
    DETERMINISTIC_PRODUCER_PREFIX,
    make_text_page_partitioner,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.pdf_ingestion import ingest_pdf
from enterprise_pdf_rag.adapters.pdfspine_tables import PdfspineTableAdapter
from ragspine.common.evidence.settings import ROOT_DIR
from ragspine.extraction.evidence.document.models import DocumentSnapshot
from ragspine.extraction.evidence.figures.models import Verification
from ragspine.extraction.evidence.page.models import ObjectKind, PageInput, PagePartition
from ragspine.extraction.evidence.page.ports import PagePartitioner
from ragspine.extraction.evidence.page.service import validate_partition
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import (
    TABLE_COLUMNS,
    TABLE_ROWS,
    authored_pdf,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_password import (
    PASSWORD,
    encrypted_pdf,
    set_password,
)
from tests.enterprise_pdf_rag.adapters.test_source_paint import _FontInsertionPage

# 一张 8x8 红色 JPEG(内联字节, 不依赖 PIL).
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAIAAgDASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDzOiii"
    "vzM/pM//2Q=="
)

HEADER_TEXT = "ACME GROUP INTERIM REPORT"
PAGE_W, PAGE_H = 612.0, 792.0


class StubModelPartitioner:
    """记录被调用的页; 产出一个合法的"全部未归属"划分, 代表模型版面."""

    fingerprint = "stub-model-layout-v1"

    def __init__(self) -> None:
        self.pages: list[int] = []

    def partition(self, page: PageInput) -> PagePartition:
        self.pages.append(page.page_index)
        return PagePartition(
            "layout-enrichment-v2",
            page.source_manifest_id,
            page.source_sha256,
            page.page_index,
            self.fingerprint,
            (),
            tuple(span.span_id for span in page.text.spans),
            diagnostics=("stub model layout",),
        )


def _margins(page: pdfspine.Page, number: int, fontname: str) -> None:
    page.insert_text((60, 30), HEADER_TEXT, fontsize=8, fontname=fontname)
    page.insert_text((300, 780), str(number), fontsize=8, fontname=fontname)


def _text_body(page: pdfspine.Page, fontname: str) -> None:
    page.insert_text((60, 90), "Results overview", fontsize=16, fontname=fontname)
    page.insert_text(
        (60, 115),
        "Revenue grew across every market this period.",
        fontsize=10,
        fontname=fontname,
    )
    page.insert_text(
        (60, 130),
        "Margins stayed broadly stable year on year.",
        fontsize=10,
        fontname=fontname,
    )
    page.insert_text((60, 170), "Highlights", fontsize=16, fontname=fontname)
    page.insert_text((60, 195), "- Strong growth in new business", fontsize=10, fontname=fontname)
    page.insert_text((60, 210), "- Dividend raised by ten percent", fontsize=10, fontname=fontname)


def authored_report(
    path: Path,
    *,
    page_count: int = 4,
    image_page: int | None = None,
    bars_page: int | None = None,
    aligned_columns_page: int | None = None,
    columns_page: int | None = None,
    borderless_page: int | None = None,
    separators: bool = False,
    sidebar: bool = False,
    embedded_font: bool = False,
) -> Path:
    with pdfspine.open() as document:
        for number in range(1, page_count + 1):
            page = document.new_page(width=PAGE_W, height=PAGE_H)
            fontname = "helv"
            if embedded_font:
                fontname = "Authored"
                cast(_FontInsertionPage, page).insert_font(
                    fontname=fontname,
                    fontbuffer=(
                        ROOT_DIR / "tests/enterprise_pdf_rag/fixtures/authored-donut-ascii.ttf"
                    ).read_bytes(),
                )
            _margins(page, number, fontname)
            if sidebar:
                page.draw_rect((0, 0, 14, PAGE_H), color=None, fill=(0.1, 0.3, 0.6), width=0)
            if number - 1 == image_page:
                page.insert_image((60, 300, 220, 420), stream=TINY_JPEG)
                page.insert_text((60, 450), "Figure 1 caption", fontsize=10, fontname=fontname)
            elif number - 1 == bars_page:
                for index in range(3):
                    x0 = 80.0 + 60.0 * index
                    page.draw_rect(
                        (x0, 360.0 - 40.0 * index, x0 + 30.0, 400.0),
                        color=None,
                        fill=(0.2, 0.4, 0.8),
                        width=0,
                    )
                page.insert_text((60, 430), "VONB by market", fontsize=10, fontname=fontname)
            elif number - 1 == aligned_columns_page:
                sentence = "Narrative sentence that fills the whole column width here"
                for row in range(8):
                    top = 300.0 + 14.0 * row
                    page.insert_text((50, top), sentence, fontsize=8, fontname=fontname)
                    page.insert_text((320, top), sentence, fontsize=8, fontname=fontname)
            elif number - 1 == columns_page:
                sentence = "Narrative sentence that fills the whole column width here"
                for row in range(8):
                    top = 300.0 + 14.0 * row
                    page.insert_text((50, top), sentence, fontsize=8, fontname=fontname)
                    page.insert_text((320, top + 7.0), sentence, fontsize=8, fontname=fontname)
            elif number - 1 == borderless_page:
                for row, (label, value) in enumerate(
                    (("Revenue", "1,234"), ("Margin", "12%"), ("Expenses", "456"))
                ):
                    page.insert_text((60, 300 + 20 * row), label, fontsize=10, fontname=fontname)
                    page.insert_text((300, 300 + 20 * row), value, fontsize=10, fontname=fontname)
            else:
                _text_body(page, fontname)
            if separators:
                page.draw_line((60, 250), (552, 250), width=0.8)
        # tobytes after building every page
        path.write_bytes(document.tobytes())
    return path


def _snapshot(tmp_path: Path, pdf: Path) -> tuple[LocalDocumentStore, DocumentSnapshot]:
    summary = ingest_pdf(pdf=pdf, output_dir=tmp_path / "output")
    sources = LocalDocumentStore(Path(summary.source_store), activate_on_publish=False)
    return sources, sources.load(summary.source_manifest_id)


def _page_input(sources: LocalDocumentStore, snapshot: DocumentSnapshot, index: int) -> PageInput:
    record = snapshot.manifest.pages[index]
    return PageInput(
        snapshot.manifest_id,
        snapshot.manifest.source.sha256,
        index,
        record.width,
        record.height,
        record.svg,
        read_text_sidecar(sources, snapshot, index),
    )


def _router(
    sources: LocalDocumentStore, snapshot: DocumentSnapshot
) -> tuple[StubModelPartitioner, PagePartitioner]:
    stub = StubModelPartitioner()
    return stub, make_text_page_partitioner(stub, sources, snapshot)


def _fallback_reason(partition: PagePartition) -> str | None:
    for diagnostic in partition.diagnostics:
        if diagnostic.startswith("deterministic-partition: model fallback ("):
            return diagnostic.removeprefix("deterministic-partition: model fallback (")[:-1]
    return None


def test_pure_text_page_partitions_without_the_model_and_covers_every_span(
    tmp_path: Path,
) -> None:
    pdf = authored_report(tmp_path / "report.pdf")
    sources, snapshot = _snapshot(tmp_path, pdf)
    stub, router = _router(sources, snapshot)
    page = _page_input(sources, snapshot, 1)
    partition = router.partition(page)

    assert stub.pages == []
    assert partition.producer.startswith(DETERMINISTIC_PRODUCER_PREFIX)
    assert partition.schema_version == "layout-enrichment-v2"
    assert partition.unassigned_span_ids == ()
    validate_partition(page, partition)
    kinds = [item.kind for item in partition.objects]
    assert kinds.count(ObjectKind.LIST) == 1
    assert kinds.count(ObjectKind.TEXT) >= 3  # 页眉 + 两个标题段落 + 页码
    # 页眉在最前, 页码在最后, 都是独立对象.
    texts = {span.span_id: span.text for span in page.text.spans}
    first = " ".join(texts[s] for s in partition.objects[0].source_span_ids)
    last = " ".join(texts[s] for s in partition.objects[-1].source_span_ids)
    assert first == HEADER_TEXT
    assert last == "2"
    # 标题与其后段落合并在一个对象里.
    merged = [
        " ".join(texts[s] for s in item.source_span_ids)
        for item in partition.objects
        if item.kind is ObjectKind.TEXT
    ]
    assert any(value.startswith("Results overview Revenue grew") for value in merged), merged


def test_list_object_carries_items_and_unordered_flag(tmp_path: Path) -> None:
    pdf = authored_report(tmp_path / "report.pdf")
    sources, snapshot = _snapshot(tmp_path, pdf)
    _stub, router = _router(sources, snapshot)
    page = _page_input(sources, snapshot, 0)
    partition = router.partition(page)
    (list_object,) = [i for i in partition.objects if i.kind is ObjectKind.LIST]
    assert len(list_object.list_item_span_ids) == 2
    assert list_object.list_ordered is False
    assert set(s for item in list_object.list_item_span_ids for s in item) == set(
        list_object.source_span_ids
    )


def test_replay_is_deterministic(tmp_path: Path) -> None:
    pdf = authored_report(tmp_path / "report.pdf")
    sources, snapshot = _snapshot(tmp_path, pdf)
    _stub, router = _router(sources, snapshot)
    page = _page_input(sources, snapshot, 2)
    assert router.partition(page) == router.partition(page)
    _stub2, second = _router(sources, snapshot)
    assert second.partition(page) == router.partition(page)


def test_image_page_falls_back_with_reason(tmp_path: Path) -> None:
    pdf = authored_report(tmp_path / "report.pdf", image_page=2)
    sources, snapshot = _snapshot(tmp_path, pdf)
    stub, router = _router(sources, snapshot)
    partition = router.partition(_page_input(sources, snapshot, 2))
    assert stub.pages == [2]
    assert partition.producer == StubModelPartitioner.fingerprint
    assert _fallback_reason(partition) == "has_image"
    # 同一份文档的纯文本页仍然确定性处理.
    clean = router.partition(_page_input(sources, snapshot, 0))
    assert clean.producer.startswith(DETERMINISTIC_PRODUCER_PREFIX)
    assert stub.pages == [2]


def test_vector_bar_page_falls_back_as_residual_graphics(tmp_path: Path) -> None:
    pdf = authored_report(tmp_path / "report.pdf", bars_page=1)
    sources, snapshot = _snapshot(tmp_path, pdf)
    stub, router = _router(sources, snapshot)
    partition = router.partition(_page_input(sources, snapshot, 1))
    assert stub.pages == [1]
    assert _fallback_reason(partition) == "residual_graphics"


def test_rotated_text_falls_back(tmp_path: Path) -> None:
    # pdfspine 自己拒绝整页旋转的源抽取, 入库到不了切分; 这里直接给出一个带旋转
    # 方向 span 的 sidecar, 钉住 span 级旋转文字的回退分支.
    pdf = authored_report(tmp_path / "report.pdf", page_count=2)
    sources, snapshot = _snapshot(tmp_path, pdf)
    _stub, router = _router(sources, snapshot)
    page = _page_input(sources, snapshot, 0)
    rotated = replace(
        page,
        text=replace(
            page.text,
            spans=tuple(replace(span, direction=(0.0, 1.0)) for span in page.text.spans),
        ),
    )
    partition = router.partition(rotated)
    assert _fallback_reason(partition) == "rotated_text"


def test_aligned_narrative_columns_fall_back_as_ambiguous(tmp_path: Path) -> None:
    pdf = authored_report(tmp_path / "report.pdf", aligned_columns_page=1)
    sources, snapshot = _snapshot(tmp_path, pdf)
    _stub, router = _router(sources, snapshot)
    partition = router.partition(_page_input(sources, snapshot, 1))
    assert _fallback_reason(partition) == "ambiguous_columns"


def test_clean_two_columns_read_left_then_right(tmp_path: Path) -> None:
    pdf = authored_report(tmp_path / "report.pdf", columns_page=1)
    sources, snapshot = _snapshot(tmp_path, pdf)
    stub, router = _router(sources, snapshot)
    page = _page_input(sources, snapshot, 1)
    partition = router.partition(page)
    assert stub.pages == []
    validate_partition(page, partition)
    spans = {span.span_id: span for span in page.text.spans}
    body = [
        item
        for item in partition.objects
        if all(spans[s].bbox[1] > 100 for s in item.source_span_ids)
        and not all(spans[s].bbox[1] > 700 for s in item.source_span_ids)
    ]
    assert len(body) == 2
    left, right = body
    assert max(spans[s].bbox[2] for s in left.source_span_ids) < 320
    assert min(spans[s].bbox[0] for s in right.source_span_ids) >= 320


def test_separators_and_repeated_sidebar_do_not_trigger_fallback(tmp_path: Path) -> None:
    pdf = authored_report(tmp_path / "report.pdf", separators=True, sidebar=True)
    sources, snapshot = _snapshot(tmp_path, pdf)
    stub, router = _router(sources, snapshot)
    partition = router.partition(_page_input(sources, snapshot, 0))
    assert stub.pages == []
    assert partition.producer.startswith(DETERMINISTIC_PRODUCER_PREFIX)


def test_borderless_rows_stay_text_objects_without_a_table(tmp_path: Path) -> None:
    pdf = authored_report(tmp_path / "report.pdf", borderless_page=1)
    sources, snapshot = _snapshot(tmp_path, pdf)
    _stub, router = _router(sources, snapshot)
    page = _page_input(sources, snapshot, 1)
    partition = router.partition(page)
    assert partition.producer.startswith(DETERMINISTIC_PRODUCER_PREFIX)
    assert all(item.kind is not ObjectKind.TABLE for item in partition.objects)
    texts = {span.span_id: span.text for span in page.text.spans}
    merged = [" ".join(texts[s] for s in item.source_span_ids) for item in partition.objects]
    assert any("Revenue 1,234" in value for value in merged), merged


def test_ruled_table_becomes_a_table_object_the_grid_proof_accepts(tmp_path: Path) -> None:
    pdf = authored_pdf(tmp_path / "table.pdf", page_count=2, label="Tables", table_page=True)
    sources, snapshot = _snapshot(tmp_path, pdf)
    stub, router = _router(sources, snapshot)
    page = _page_input(sources, snapshot, 1)
    partition = router.partition(page)
    assert stub.pages == []
    validate_partition(page, partition)
    (table,) = [item for item in partition.objects if item.kind is ObjectKind.TABLE]
    assert table.bbox == (TABLE_COLUMNS[0], TABLE_ROWS[0], TABLE_COLUMNS[-1], TABLE_ROWS[-1])
    result = PdfspineTableAdapter().extract(pdf.read_bytes(), page=page, item=table)
    assert result.table is not None
    assert result.table.verification is Verification.VERIFIED


def test_encrypted_text_pages_partition_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = encrypted_pdf(tmp_path / "secret.pdf")
    set_password(monkeypatch, PASSWORD)
    sources, snapshot = _snapshot(tmp_path, pdf)
    stub, router = _router(sources, snapshot)
    page = _page_input(sources, snapshot, 0)
    partition = router.partition(page)
    assert stub.pages == []
    assert partition.producer.startswith(DETERMINISTIC_PRODUCER_PREFIX)
    validate_partition(page, partition)


def test_router_fingerprint_names_both_producers(tmp_path: Path) -> None:
    pdf = authored_report(tmp_path / "report.pdf", page_count=1)
    sources, snapshot = _snapshot(tmp_path, pdf)
    _stub, router = _router(sources, snapshot)
    fingerprint = router.fingerprint
    assert DETERMINISTIC_PRODUCER_PREFIX in fingerprint
    assert StubModelPartitioner.fingerprint in fingerprint
