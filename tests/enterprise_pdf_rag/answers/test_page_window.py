"""Page context woven into the hit blocks: one block per page, in reading order."""

from enterprise_pdf_rag.answers.page_window import reading_key, with_page_context
from enterprise_pdf_rag.answers.ports import MemberText
from enterprise_pdf_rag.figures.models import Verification
from enterprise_pdf_rag.processing.context_builder import (
    BlockKind,
    ContextBlock,
    PageContextBlock,
)
from enterprise_pdf_rag.processing.models import ObjectKind

SNAPSHOT = "1" * 64


def _hit(member_id: str, page_index: int = 0) -> ContextBlock:
    return ContextBlock(
        SNAPSHOT, member_id, BlockKind.TEXT, page_index, "literal", Verification.VERIFIED, "d"
    )


def _member(
    member_id: str,
    page_index: int = 0,
    text: str = "A neighbour.",
    *,
    kind: ObjectKind = ObjectKind.TEXT,
    bbox: tuple[float, float, float, float] | None = None,
    header: str = "",
    page_title: str | None = None,
    section: str | None = None,
) -> MemberText:
    return MemberText(
        member_id,
        kind,
        page_index,
        text,
        page_title=page_title,
        section=section,
        header=header,
        bbox=bbox,
    )


def test_reading_key_quantises_rows_then_reads_left_to_right() -> None:
    left = _member("m-left", bbox=(20.0, 101.0, 80.0, 113.0))
    right = _member("m-right", bbox=(120.0, 100.0, 180.0, 112.0))
    below = _member("m-below", bbox=(0.0, 140.0, 60.0, 152.0))
    # 100.0 and 101.0 quantise to the same row, so the left column comes first.
    assert reading_key(left) == (0, 25, 20.0, "m-left")
    assert reading_key(right)[1] == reading_key(left)[1] == 25
    assert reading_key(below)[1] == 35
    assert [item.member_id for item in sorted((below, right, left), key=reading_key)] == [
        "m-left",
        "m-right",
        "m-below",
    ]


def test_reading_key_puts_members_without_a_rectangle_after_every_located_one() -> None:
    located = _member("z-located", bbox=(500.0, 900.0, 520.0, 912.0))
    first = _member("a-floating")
    second = _member("b-floating")
    assert reading_key(first) == (1, 0, 0.0, "a-floating")
    assert [item.member_id for item in sorted((second, first, located), key=reading_key)] == [
        "z-located",
        "a-floating",
        "b-floating",
    ]


def test_each_page_gets_one_context_block_after_its_first_hit() -> None:
    blocks = (_hit("h-0", 0), _hit("h-1", 1))
    members = (
        _member("h-0", 0, "The hit on page zero."),
        _member("n-0", 0, "The rest of page zero."),
        _member("h-1", 1, "The hit on page one."),
        _member("n-1", 1, "The rest of page one."),
    )
    woven = with_page_context(blocks, members, max_chars=1000)
    assert [type(block) for block in woven] == [
        ContextBlock,
        PageContextBlock,
        ContextBlock,
        PageContextBlock,
    ]
    assert (woven[0], woven[2]) == blocks
    zero, one = woven[1], woven[3]
    assert isinstance(zero, PageContextBlock) and isinstance(one, PageContextBlock)
    assert (zero.page_index, one.page_index) == (0, 1)
    assert [item.member_id for item in zero.members] == ["n-0"]
    assert [item.member_id for item in one.members] == ["n-1"]


def test_a_page_with_two_hits_still_gets_exactly_one_block_after_the_first() -> None:
    blocks = (_hit("h-a", 2), _hit("h-b", 2), _hit("h-c", 5))
    members = (
        _member("h-a", 2, "The first hit."),
        _member("h-b", 2, "The second hit."),
        _member("n-1", 2, "A neighbour of both."),
        _member("h-c", 5, "A hit elsewhere."),
        _member("n-2", 5, "Another page's neighbour."),
    )
    woven = with_page_context(blocks, members, max_chars=1000)
    assert [type(block) for block in woven] == [
        ContextBlock,
        PageContextBlock,
        ContextBlock,
        ContextBlock,
        PageContextBlock,
    ]
    assert (woven[0], woven[2], woven[3]) == blocks
    pages = [block for block in woven if isinstance(block, PageContextBlock)]
    assert [block.page_index for block in pages] == [2, 5]


def test_a_member_that_already_has_its_own_block_never_repeats_in_the_page_context() -> None:
    blocks = (_hit("h-0", 0),)
    members = (
        _member("h-0", 0, "The hit's own evidence is printed above."),
        _member("n-1", 0, "The rest of the page."),
    )
    _, page = with_page_context(blocks, members, max_chars=1000)
    assert isinstance(page, PageContextBlock)
    assert [item.member_id for item in page.members] == ["n-1"]
    assert "The hit's own evidence" not in page.prompt_text()


def test_a_page_whose_only_member_is_the_hit_gets_no_block() -> None:
    blocks = (_hit("h-0", 0),)
    members = (_member("h-0", 0, "The only member of the page."),)
    assert with_page_context(blocks, members, max_chars=1000) == blocks
    # A hit whose page is unknown to ``members`` is left alone too.
    assert with_page_context(blocks, (), max_chars=1000) == blocks


def test_a_member_kind_with_no_block_kind_is_skipped_rather_than_refused() -> None:
    blocks = (_hit("h-0", 0),)
    members = (
        _member("h-0", 0, "The hit."),
        _member("img-1", 0, "An image is not retrievable.", kind=ObjectKind.IMAGE),
        _member("tbl-1", 0, "Total 1,234", kind=ObjectKind.TABLE),
    )
    _, page = with_page_context(blocks, members, max_chars=1000)
    assert isinstance(page, PageContextBlock)
    assert [(item.member_id, item.kind) for item in page.members] == [("tbl-1", BlockKind.TABLE)]


def test_the_page_heading_comes_from_the_first_member_of_that_page() -> None:
    blocks = (_hit("h-0", 3),)
    members = (
        _member("h-0", 3, "The hit.", page_title="Group performance", section="Financial review"),
        _member("n-1", 3, "The rest of the page."),
    )
    _, page = with_page_context(blocks, members, max_chars=1000)
    assert isinstance(page, PageContextBlock)
    assert (page.page_title, page.section) == ("Group performance", "Financial review")
    assert page.prompt_text().startswith(
        "[page_context page_index=3] title=Group performance section=Financial review"
    )


def test_the_page_context_prints_the_body_without_the_index_header() -> None:
    header = "Meridian 1H26 | Overview"
    blocks = (_hit("h-0", 0),)
    members = (
        _member("h-0", 0, f"{header}\nThe hit.", header=header),
        _member("n-1", 0, f"{header}\nCosts fell.", header=header),
    )
    _, page = with_page_context(blocks, members, max_chars=1000)
    assert isinstance(page, PageContextBlock)
    assert [item.text for item in page.members] == ["Costs fell."]
    assert header not in page.prompt_text()


def test_page_members_are_ordered_by_reading_key_not_by_member_id() -> None:
    blocks = (_hit("h-0", 0),)
    members = (
        _member("z-top", 0, "Top of the page.", bbox=(10.0, 20.0, 60.0, 32.0)),
        _member("a-bottom", 0, "Bottom of the page.", bbox=(10.0, 80.0, 60.0, 92.0)),
        _member("h-0", 0, "The hit.", bbox=(10.0, 50.0, 60.0, 62.0)),
    )
    _, page = with_page_context(blocks, members, max_chars=1000)
    assert isinstance(page, PageContextBlock)
    assert [item.member_id for item in page.members] == ["z-top", "a-bottom"]


def test_the_budget_truncates_one_page_block_from_its_end() -> None:
    blocks = (_hit("h-0", 0),)
    members = (
        _member("h-0", 0, "The hit."),
        _member("n-1", 0, "The first neighbour."),
        _member("n-2", 0, "The second neighbour."),
    )
    _, whole = with_page_context(blocks, members, max_chars=1000)
    assert isinstance(whole, PageContextBlock)
    assert len(whole.members) == 2 and whole.truncated is False
    _, cut = with_page_context(blocks, members, max_chars=whole.chars - 1)
    assert isinstance(cut, PageContextBlock)
    assert [item.member_id for item in cut.members] == ["n-1"]
    assert cut.truncated is True
