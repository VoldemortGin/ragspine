"""A corpus unit carries the header it embedded, so its body can be read without it."""

from enterprise_pdf_rag.answers.ports import MemberText
from ragspine.extraction.evidence.page.models import ObjectKind


def _member(text: str, header: str = "") -> MemberText:
    return MemberText("obj-1", ObjectKind.TEXT, 0, text, header=header)


def test_body_drops_the_declared_header_line() -> None:
    member = _member("Meridian 1H26 | Page 2\nRevenue grew.", "Meridian 1H26 | Page 2")
    assert member.body == "Revenue grew."


def test_body_is_the_whole_text_when_no_header_was_prefixed() -> None:
    member = _member("Revenue grew.")
    assert (member.header, member.body, member.bbox) == ("", "Revenue grew.", None)


def test_an_empty_header_never_eats_the_first_character() -> None:
    assert _member("\nRevenue grew.").body == "\nRevenue grew."


def test_body_keeps_its_own_line_breaks() -> None:
    member = _member("Meridian 1H26\nfirst line\nsecond line", "Meridian 1H26")
    assert member.body == "first line\nsecond line"


def test_body_is_untouched_when_the_text_does_not_start_with_the_header() -> None:
    member = _member("Revenue grew.", "Meridian 1H26")
    assert member.body == "Revenue grew."
