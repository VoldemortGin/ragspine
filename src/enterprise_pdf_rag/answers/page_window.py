"""Weave each hit's page into the prompt: the neighbours it was read beside.

A hit block proves one object; this module adds, once per page, the rest of that page
in reading order so the model can read the hit in its context. Page context carries no
citable path, so it can only inform an answer — never be the target of a claim.
"""

from collections.abc import Sequence

from enterprise_pdf_rag.answers.ports import MemberText
from enterprise_pdf_rag.processing.context_builder import (
    BlockKind,
    ContextBlock,
    PageContextMember,
    PromptBlock,
    build_page_context_block,
)
from ragspine.extraction.evidence.objects.diagrams.diagram_models import READING_ROW_QUANTUM


def reading_key(member: MemberText) -> tuple[int, int, float, str]:
    """Rows quantised to ``READING_ROW_QUANTUM`` points, then left to right, then by id.

    The same criterion a proved diagram reads its nodes by. A member whose stored evidence
    carries no rectangle cannot be placed on the page, so it sorts after every located one,
    by id — the order stays total and deterministic either way.
    """
    if member.bbox is None:
        return (1, 0, 0.0, member.member_id)
    return (0, round(member.bbox[1] / READING_ROW_QUANTUM), member.bbox[0], member.member_id)


def with_page_context(
    blocks: Sequence[ContextBlock], members: Sequence[MemberText], *, max_chars: int
) -> tuple[PromptBlock, ...]:
    """Insert one page context block after the first hit of each page, hits excluded.

    The hit blocks keep their fused order. A member that already has its own block is left
    out of its page's context, since its full evidence is printed above; a kind that has no
    block kind (an ``IMAGE``) is skipped. The page heading is the verified metadata of the
    page's first member (it is shared page-wide, ADR 0013), and a page with nothing left to
    say gets no block.
    """
    seated = {block.member_id for block in blocks}
    pages = {block.page_index for block in blocks}
    neighbours: dict[int, list[MemberText]] = {page: [] for page in pages}
    heading: dict[int, MemberText] = {}
    for member in members:
        if member.page_index not in pages:
            continue
        heading.setdefault(member.page_index, member)
        if member.member_id not in seated and member.kind.name in BlockKind.__members__:
            neighbours[member.page_index].append(member)
    woven: list[PromptBlock] = []
    seen: set[int] = set()
    for block in blocks:
        woven.append(block)
        if block.page_index in seen:
            continue
        seen.add(block.page_index)
        first = heading.get(block.page_index)
        page = build_page_context_block(
            tuple(
                PageContextMember(member.member_id, BlockKind[member.kind.name], member.body)
                for member in sorted(neighbours[block.page_index], key=reading_key)
            ),
            page_index=block.page_index,
            page_title=None if first is None else first.page_title,
            section=None if first is None else first.section,
            max_chars=max_chars,
        )
        if page is not None:
            woven.append(page)
    return tuple(woven)
