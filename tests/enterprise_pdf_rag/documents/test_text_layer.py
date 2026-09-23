"""Text-layer quality: a page's spans either carry its words, or the page says why not."""

import pytest

from enterprise_pdf_rag.documents.models import TextLayerDiagnostic, TextLayerStatus
from enterprise_pdf_rag.documents.text_layer import (
    GARBLED_PAGE_MIN_RATIO,
    OUTLINED_TEXT_DRAWINGS_PER_CHAR,
    OUTLINED_TEXT_MIN_DRAWINGS,
    TEXTLESS_OUTLINED_MIN_DRAWINGS,
    diagnose_text_layer,
    is_garbled_text,
)


@pytest.mark.parametrize(
    "text",
    [
        "Revenue grew 12% to US$1,234m",
        "新业务价值增长12%，达到1,234百万美元。",  # noqa: RUF001 — simplified Chinese, full-width punctuation
        "新業務價值「年度」增長；稅後營運溢利",  # noqa: RUF001 — traditional Chinese, corner brackets
        "売上高は前年比１２％増加しました（連結）",  # noqa: RUF001 — Japanese kana, full-width digits
        "매출 증가",  # Korean
        "€ £ ¥ ￥ ₩ ₹ $ ¢",  # currency symbols, including the full-width yen
        "∑ ≤ ≥ ± × ÷ √ ∞ ≈ ≠ ∆ ∫ ‰ °",  # noqa: RUF001 — math and measure symbols
        "“quoted” — en–dash … • ·",  # noqa: RUF001 — typographic punctuation
        "soft\u00adhyphen and zero\u200bwidth",  # format characters are real text
        "tab\tand\nnewline",
        "",
        "   ",
    ],
)
def test_real_text_in_any_script_is_never_garbled(text: str) -> None:
    assert is_garbled_text(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "\ufffd\ufffd\ufffd",  # the extractor's own "could not decode"
        "Revenue \ufffd",  # one replacement character already breaks verbatim fidelity
        "\ue000\ue001\ue002",  # private-use glyph codes from a font without ToUnicode
        "\U000f0001",  # supplementary private-use plane
        "\x01\x02\x03",  # C0 control characters
        "abc\x7f",  # DEL
        "\x90",  # C1 control (NEL, U+0085, is whitespace)
    ],
)
def test_undecodable_characters_make_a_span_garbled(text: str) -> None:
    assert is_garbled_text(text) is True


def test_normal_page_is_ok_with_its_counts() -> None:
    diagnostic = diagnose_text_layer(("Revenue 1,234", "收入 增长"), drawing_count=12)

    assert diagnostic == TextLayerDiagnostic(
        TextLayerStatus.OK,
        span_count=2,
        char_count=16,
        garbled_char_count=0,
        garbled_span_count=0,
        drawing_count=12,
    )
    assert diagnostic.needs_ocr is False


def test_page_painting_only_vector_paths_is_outlined_text() -> None:
    diagnostic = diagnose_text_layer((), drawing_count=OUTLINED_TEXT_MIN_DRAWINGS)

    assert diagnostic.status is TextLayerStatus.OUTLINED_TEXT
    assert diagnostic.needs_ocr is True


def test_outlined_body_with_a_live_running_header_is_still_outlined_text() -> None:
    header = "Interim Report 2026 page 12"  # 23 characters
    chars = sum(not c.isspace() for c in header)
    diagnostic = diagnose_text_layer(
        (header,), drawing_count=chars * OUTLINED_TEXT_DRAWINGS_PER_CHAR + 400
    )

    assert diagnostic.status is TextLayerStatus.OUTLINED_TEXT


def test_divider_page_with_only_an_outlined_heading_is_outlined_text() -> None:
    # The real AIA divider pages paint their one-line heading in 13-25 paths and no text.
    diagnostic = diagnose_text_layer((), drawing_count=13)

    assert diagnostic.status is TextLayerStatus.OUTLINED_TEXT
    assert diagnostic.needs_ocr is True


def test_textless_page_with_a_single_path_is_outlined_text() -> None:
    diagnostic = diagnose_text_layer((), drawing_count=TEXTLESS_OUTLINED_MIN_DRAWINGS)

    assert diagnostic.status is TextLayerStatus.OUTLINED_TEXT


def test_blank_page_without_text_or_paths_stays_ok() -> None:
    assert diagnose_text_layer((), drawing_count=0).status is TextLayerStatus.OK
    assert diagnose_text_layer(("  ",), drawing_count=0).status is TextLayerStatus.OK


def test_page_with_some_text_and_few_paths_is_not_outlined_text() -> None:
    diagnostic = diagnose_text_layer(("A",), drawing_count=OUTLINED_TEXT_MIN_DRAWINGS - 1)

    assert diagnostic.status is TextLayerStatus.OK


def test_vector_chart_with_real_labels_is_not_outlined_text() -> None:
    labels = ("Revenue by segment", "2022", "2023", "2024", "Asia", "Europe", "0", "50", "100")
    chars = sum(not c.isspace() for label in labels for c in label)
    # A dense bar chart: many more paths than the minimum, but well under the per-char rule.
    drawings = chars * OUTLINED_TEXT_DRAWINGS_PER_CHAR - 1
    assert drawings > OUTLINED_TEXT_MIN_DRAWINGS

    assert diagnose_text_layer(labels, drawing_count=drawings).status is TextLayerStatus.OK


def test_page_whose_undecodable_share_reaches_the_threshold_is_garbled() -> None:
    diagnostic = diagnose_text_layer(("Revenue", "\ue000\ue001\ufffd", "grew"), drawing_count=3)

    assert diagnostic.status is TextLayerStatus.GARBLED
    assert (diagnostic.char_count, diagnostic.garbled_char_count) == (14, 3)
    assert diagnostic.garbled_span_count == 1
    assert diagnostic.needs_ocr is True


def test_one_stray_undecodable_span_on_a_full_page_is_counted_but_page_stays_ok() -> None:
    body = "x" * 200
    diagnostic = diagnose_text_layer((body, "\ue000"), drawing_count=3)

    assert diagnostic.garbled_char_count / diagnostic.char_count < GARBLED_PAGE_MIN_RATIO
    assert diagnostic.status is TextLayerStatus.OK
    assert (diagnostic.garbled_char_count, diagnostic.garbled_span_count) == (1, 1)
