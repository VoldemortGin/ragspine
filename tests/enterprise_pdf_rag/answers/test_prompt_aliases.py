"""A short per-request name for a block: minted in prompt order, resolved before verification."""

from enterprise_pdf_rag.answers.prompt import (
    ModelAnswer,
    ModelClaim,
    build_prompt,
    member_aliases,
    resolve_member_aliases,
)
from enterprise_pdf_rag.figures.models import Verification
from enterprise_pdf_rag.processing.context_builder import (
    BlockKind,
    ContextBlock,
    PageContextBlock,
    PageContextMember,
)

SNAPSHOT = "1" * 64
FIRST = "a" * 64
SECOND = "b" * 64


def _block(member_id: str) -> ContextBlock:
    return ContextBlock(
        SNAPSHOT, member_id, BlockKind.TEXT, 0, "literal", Verification.VERIFIED, "d"
    )


def _page_context() -> PageContextBlock:
    return PageContextBlock(
        0, None, None, (PageContextMember("neighbour", BlockKind.TEXT, "A neighbour."),)
    )


def _answer(*member_ids: str) -> ModelAnswer:
    return ModelAnswer(
        abstain=False,
        abstain_reason=None,
        answer="",
        claims=tuple(
            ModelClaim(
                claim_id=f"c{index}",
                member_id=member_id,
                kind="quote",
                field_path="fragments.s1",
                text="t",
            )
            for index, member_id in enumerate(member_ids)
        ),
    )


def test_aliases_are_minted_in_prompt_order_and_page_context_gets_none() -> None:
    blocks = (_block(FIRST), _page_context(), _block(SECOND))

    assert member_aliases(blocks) == {FIRST: "m1", SECOND: "m2"}


def test_the_prompt_heads_each_block_with_its_alias_beside_the_real_member_id() -> None:
    blocks = (_block(FIRST), _page_context(), _block(SECOND))

    prompt = build_prompt("q", blocks, (), member_aliases(blocks))

    assert f"[m1 | member {FIRST}] kind=text" in prompt
    assert f"[m2 | member {SECOND}] kind=text" in prompt
    # The page context is named by nothing at all, so no claim can reach for it.
    assert "[page_context page_index=0]" in prompt and "member neighbour" not in prompt


def test_without_aliases_the_prompt_prints_the_member_id_alone() -> None:
    assert f"[member {FIRST}] kind=text" in build_prompt("q", (_block(FIRST),))


def test_an_alias_is_rewritten_while_a_full_id_and_an_unknown_name_stand() -> None:
    resolved = resolve_member_aliases(_answer("m2", FIRST, "m99"), {FIRST: "m1", SECOND: "m2"})

    assert [claim.member_id for claim in resolved.claims] == [SECOND, FIRST, "m99"]


def test_an_answer_naming_no_alias_comes_back_exactly_as_the_model_wrote_it() -> None:
    model = _answer(FIRST, "m99")

    assert resolve_member_aliases(model, {FIRST: "m1", SECOND: "m2"}) == model
