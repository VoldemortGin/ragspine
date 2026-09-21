"""A document tree folds page metadata into verbatim sections that tile the selected pages."""

import pytest

from enterprise_pdf_rag.documents.models import TextSpan
from enterprise_pdf_rag.processing.document_tree import (
    DOCUMENT_TREE_SCHEMA,
    MAX_DIVIDER_SPANS,
    MIN_AGENDA_MATCHES,
    DocumentTree,
    TreeNode,
    TreePage,
    agenda_entries,
    build_document_tree,
    render_tree,
    summary_targets,
    with_summaries,
)
from enterprise_pdf_rag.processing.page_metadata import (
    CandidateValue,
    MetadataEvidence,
    MetadataValue,
    PageMetadataCandidate,
    PageType,
    fold_whitespace,
    verify_page_metadata,
)

_SHA = "b" * 64
_DISPLAY_TITLE = MetadataValue(
    "ACME 2026 Interim Results",
    MetadataEvidence(("p0-s0",), "ACME 2026 Interim Results 20 August 2026"),
)


def _spans(page_index: int, lines: tuple[str, ...]) -> tuple[TextSpan, ...]:
    return tuple(
        TextSpan(f"p{page_index}-s{number}", text, (0.0, float(number) * 12.0, 400.0, 12.0))
        for number, text in enumerate(lines)
    )


def _cite(spans: tuple[TextSpan, ...], text: str) -> CandidateValue:
    """Quote the first span that really prints ``text``; a fixture may not invent a value."""
    span = next(span for span in spans if fold_whitespace(text) in fold_whitespace(span.text))
    return CandidateValue(text, span.span_id)


def _page(
    page_index: int,
    page_type: PageType,
    lines: tuple[str, ...],
    *,
    title: str | None = None,
    section: str | None = None,
) -> TreePage:
    """One selected page, its metadata verified against the lines the page prints."""
    spans = _spans(page_index, lines)
    candidate = PageMetadataCandidate(
        page_type,
        "en",
        None if title is None else _cite(spans, title),
        None if section is None else _cite(spans, section),
        (),
        (),
    )
    metadata = verify_page_metadata(candidate, spans, source_sha256=_SHA, page_index=page_index)
    assert metadata.diagnostics == ()
    return TreePage(metadata, spans)


def _body(*lines: str) -> tuple[str, ...]:
    """Enough printed lines that the page reads as content rather than as a section divider."""
    filler = tuple(f"Line {number} of the running text" for number in range(MAX_DIVIDER_SPANS))
    return (*lines, *filler)


def _agenda_document() -> tuple[TreePage, ...]:
    """A deck whose page 1 is a contents page naming three sections printed later."""
    return (
        _page(
            0,
            PageType.COVER,
            ("ACME 2026 Interim Results", "20 August 2026", "Hong Kong"),
            title="ACME 2026 Interim Results",
        ),
        _page(
            1,
            PageType.AGENDA,
            ("Agenda", "Business review", "Financial review", "Appendix", "1"),
            title="Agenda",
        ),
        _page(
            2,
            PageType.TEXT,
            _body("Group highlights", "Business review"),
            title="Group highlights",
            section="Business review",
        ),
        _page(
            3,
            PageType.CHART,
            _body("Distribution Mix", "Business review"),
            title="Distribution Mix",
            section="Business review",
        ),
        _page(
            4,
            PageType.TEXT,
            _body("Operating profit", "Financial review"),
            title="Operating profit",
            section="Financial review",
        ),
        _page(
            5,
            PageType.TABLE,
            _body("Operating profit", "Financial review"),
            title="Operating profit",
            section="Financial review",
        ),
        _page(
            6,
            PageType.APPENDIX,
            _body("Appendix", "Supplementary information"),
            title="Appendix",
            section="Appendix",
        ),
        _page(
            7,
            PageType.APPENDIX,
            _body("Glossary", "Appendix"),
            title="Glossary",
            section="Appendix",
        ),
    )


def _sectioned_document() -> tuple[TreePage, ...]:
    """The same deck without a contents page: a divider page and running headers cut it."""
    return (
        _page(
            0,
            PageType.COVER,
            ("ACME 2026 Interim Results", "20 August 2026", "Hong Kong"),
            title="ACME 2026 Interim Results",
        ),
        _page(
            1,
            PageType.TEXT,
            _body("Group highlights", "Business review"),
            title="Group highlights",
            section="Business review",
        ),
        _page(
            2,
            PageType.CHART,
            _body("Distribution Mix", "Business review"),
            title="Distribution Mix",
            section="Business review",
        ),
        # A section slide: one printed line, so it is a divider and opens a first-level node.
        _page(3, PageType.TEXT, ("Financial review",), title="Financial review"),
        _page(
            4,
            PageType.TEXT,
            _body("Operating profit", "Financial review"),
            title="Operating profit",
            section="Financial review",
        ),
        # A chart page prints few lines, but a chart is never read as a divider.
        _page(
            5,
            PageType.CHART,
            ("Operating profit", "Financial review", "FY25 12,345", "FY26 13,456"),
            title="Operating profit",
            section="Financial review",
        ),
        _page(
            6,
            PageType.APPENDIX,
            _body("Glossary", "Appendix"),
            title="Glossary",
            section="Appendix",
        ),
    )


def _built(pages: tuple[TreePage, ...]) -> DocumentTree:
    tree = build_document_tree(pages, source_sha256=_SHA)
    assert tree is not None
    return tree


def _node(
    node_id: str,
    title: str,
    level: int,
    pages: tuple[int, ...],
    children: tuple[TreeNode, ...] = (),
) -> TreeNode:
    return TreeNode(node_id, title, level, pages, children, MetadataEvidence(("s0",), title))


def _leaf_pages(tree: DocumentTree) -> tuple[int, ...]:
    return tuple(page for node in tree.walk() if node.is_leaf for page in node.pages)


def test_the_schema_version_is_frozen() -> None:
    assert DOCUMENT_TREE_SCHEMA == "document-tree-v1"


def test_a_contents_page_cuts_the_first_level_at_the_pages_its_lines_name() -> None:
    tree = _built(_agenda_document())

    assert tree.origin == "agenda"
    assert tree.schema_version == DOCUMENT_TREE_SCHEMA and tree.source_sha256 == _SHA
    assert tuple(root.title for root in tree.roots) == (
        "Business review",
        "Financial review",
        "Appendix",
    )
    # The contents page, and the cover before it, belong to the first section it names.
    assert tuple(root.pages for root in tree.roots) == ((0, 1, 2, 3), (4, 5), (6, 7))
    assert tuple(root.page_start for root in tree.roots) == (0, 4, 6)
    assert tuple(root.level for root in tree.roots) == (1, 1, 1)


def test_a_first_level_title_is_the_agenda_line_and_not_the_page_it_was_matched_to() -> None:
    tree = _built(_agenda_document())
    agenda_page = _agenda_document()[1]
    lines = tuple(entry.text for entry in agenda_entries(agenda_page))

    assert tuple(root.title for root in tree.roots) == lines
    for root in tree.roots:
        assert root.evidence is not None
        assert root.evidence.span_ids[0].startswith("p1-s")
    # The pages under the first section still carry their own printed titles.
    assert tuple(child.title for child in tree.roots[0].children) == (
        "ACME 2026 Interim Results",
        "Agenda",
        "Group highlights",
        "Distribution Mix",
    )


def test_consecutive_pages_under_one_title_become_one_node_printing_a_leaf_per_page() -> None:
    financial = _built(_agenda_document()).roots[1]

    assert financial.pages == (4, 5) and not financial.is_leaf
    grouped = financial.children[0]
    assert (grouped.title, grouped.level, grouped.pages) == ("Operating profit", 2, (4, 5))
    assert tuple((leaf.level, leaf.pages) for leaf in grouped.children) == ((3, (4,)), (3, (5,)))
    assert all(leaf.is_leaf for leaf in grouped.children)


def test_a_contents_page_naming_fewer_than_two_pages_is_not_a_contents_page() -> None:
    assert MIN_AGENDA_MATCHES == 2
    pages = (
        _page(0, PageType.COVER, ("ACME 2026 Interim Results",), title="ACME 2026 Interim Results"),
        _page(
            1, PageType.AGENDA, ("Agenda", "Business review", "Financial review"), title="Agenda"
        ),
        _page(
            2,
            PageType.TEXT,
            _body("Group highlights", "Business review"),
            title="Group highlights",
            section="Business review",
        ),
    )
    tree = _built(pages)

    assert tree.origin == "sections"
    assert _leaf_pages(tree) == (0, 1, 2)


def test_without_a_contents_page_dividers_and_header_changes_cut_the_first_level() -> None:
    tree = _built(_sectioned_document())

    assert tree.origin == "sections"
    assert tuple(root.title for root in tree.roots) == (
        "ACME 2026 Interim Results",
        "Business review",
        "Financial review",
        "Appendix",
    )
    assert tuple(root.pages for root in tree.roots) == ((0,), (1, 2), (3, 4, 5), (6,))


def test_a_divider_absorbs_the_pages_whose_running_header_repeats_its_name() -> None:
    financial = _built(_sectioned_document()).roots[2]

    # Page 3 is the divider that announces the section; pages 4-5 print that same name as
    # their running header, which is a repetition, not a change, so it opens no second node.
    assert (financial.title, financial.pages) == ("Financial review", (3, 4, 5))
    assert tuple(child.title for child in financial.children) == (
        "Financial review",
        "Operating profit",
    )


def test_a_chart_page_printing_few_lines_does_not_start_a_first_level_node() -> None:
    chart = _sectioned_document()[5]
    assert chart.metadata.page_type is PageType.CHART and len(chart.spans) <= MAX_DIVIDER_SPANS

    assert tuple(root.page_start for root in _built(_sectioned_document()).roots) == (0, 1, 3, 6)


def test_every_node_title_is_printed_verbatim_in_the_evidence_it_carries() -> None:
    for pages in (_agenda_document(), _sectioned_document()):
        for node in _built(pages).walk():
            assert node.evidence is not None
            assert node.evidence.span_ids
            assert node.title == fold_whitespace(node.title)
            assert node.title in node.evidence.text


def test_the_leaves_tile_the_selected_pages_exactly_once_in_document_order() -> None:
    for pages in (_agenda_document(), _sectioned_document()):
        tree = _built(pages)
        selected = tuple(sorted(page.page_index for page in pages))

        assert _leaf_pages(tree) == selected
        assert tree.pages == selected and tree.page_count == len(selected)
        for node in tree.walk():
            if not node.is_leaf:
                assert tuple(page for child in node.children for page in child.pages) == node.pages


def test_the_pages_are_sorted_before_they_are_folded() -> None:
    pages = _agenda_document()
    assert build_document_tree(tuple(reversed(pages)), source_sha256=_SHA) == _built(pages)


def test_a_non_contiguous_page_selection_tiles_and_reports_the_real_page_indices() -> None:
    pages = (
        _page(0, PageType.COVER, ("ACME 2026 Interim Results",), title="ACME 2026 Interim Results"),
        _page(
            1,
            PageType.TEXT,
            _body("Group highlights", "Business review"),
            title="Group highlights",
            section="Business review",
        ),
        _page(
            5,
            PageType.TEXT,
            _body("Operating profit", "Financial review"),
            title="Operating profit",
            section="Financial review",
        ),
        _page(
            6,
            PageType.TABLE,
            _body("Embedded value", "Financial review"),
            title="Embedded value",
            section="Financial review",
        ),
    )
    tree = _built(pages)

    assert _leaf_pages(tree) == (0, 1, 5, 6)
    assert tuple(root.pages for root in tree.roots) == ((0,), (1,), (5, 6))
    financial = tree.roots[2]
    assert (financial.page_start, financial.page_end) == (5, 6)
    assert tuple(leaf.pages for leaf in financial.children) == ((5,), (6,))


def test_the_document_title_names_the_opening_pages_that_print_no_title_of_their_own() -> None:
    pages = (
        _page(0, PageType.TEXT, _body("Nothing this page prints is a title")),
        _page(
            1,
            PageType.TEXT,
            _body("Group highlights", "Business review"),
            title="Group highlights",
            section="Business review",
        ),
    )
    named = build_document_tree(pages, source_sha256=_SHA, display_title=_DISPLAY_TITLE)
    assert named is not None
    assert tuple(root.title for root in named.roots) == (
        "ACME 2026 Interim Results",
        "Business review",
    )
    assert tuple(root.pages for root in named.roots) == ((0,), (1,))
    assert named.roots[0].title in _DISPLAY_TITLE.evidence.text

    # Without a document title the untitled opening page joins the section that follows it.
    anonymous = _built(pages)
    assert tuple(root.pages for root in anonymous.roots) == ((0, 1),)


def test_node_ids_ascend_in_document_order_without_gaps() -> None:
    for pages in (_agenda_document(), _sectioned_document()):
        ids = [node.node_id for node in _built(pages).walk()]
        assert ids == [f"n{number:04d}" for number in range(1, len(ids) + 1)]


def test_agenda_entries_are_the_contents_lines_verbatim_without_heading_or_page_numbers() -> None:
    page = _page(
        1,
        PageType.AGENDA,
        (
            "Agenda",
            "Business review",
            "1",
            "Financial   review",
            "Business  review",
            "Appendix",
            "This line is a whole sentence of running text and is far too long to name a section",
        ),
        title="Agenda",
    )
    entries = agenda_entries(page)

    assert tuple(entry.text for entry in entries) == (
        "Business review",
        "Financial review",
        "Appendix",
    )
    for entry in entries:
        assert entry.evidence.text == entry.text
        assert len(entry.evidence.span_ids) == 1


def test_a_node_whose_children_do_not_tile_its_pages_is_rejected() -> None:
    with pytest.raises(ValueError, match="children must tile its pages in order"):
        TreeNode("n1", "Business review", 1, (0, 1), (_node("n2", "Cover", 2, (0,)),))
    with pytest.raises(ValueError, match="children must tile its pages in order"):
        _node(
            "n1",
            "Business review",
            1,
            (0, 1, 2),
            (_node("n2", "Cover", 2, (0, 1)), _node("n3", "Mix", 2, (1, 2))),
        )
    with pytest.raises(ValueError, match="one level below"):
        _node("n1", "Business review", 1, (0,), (_node("n2", "Cover", 3, (0,)),))


def test_a_node_covering_no_page_or_the_same_page_twice_is_rejected() -> None:
    with pytest.raises(ValueError, match="covers no page"):
        _node("n1", "Business review", 1, ())
    with pytest.raises(ValueError, match="ascend without repeating"):
        _node("n1", "Business review", 1, (1, 1))
    with pytest.raises(ValueError, match="ascend without repeating"):
        _node("n1", "Business review", 1, (2, 1))
    with pytest.raises(ValueError, match="needs a title"):
        _node("n1", "  ", 1, (0,))
    with pytest.raises(ValueError, match="needs an id"):
        _node(" ", "Business review", 1, (0,))
    with pytest.raises(ValueError, match="level of at least one"):
        _node("n1", "Business review", 0, (0,))


def test_a_tree_whose_roots_overlap_or_run_backwards_is_rejected() -> None:
    first = _node("n1", "Business review", 1, (0, 1))
    with pytest.raises(ValueError, match="without overlap"):
        DocumentTree(DOCUMENT_TREE_SCHEMA, _SHA, "sections", (first, _node("n2", "F", 1, (1, 2))))
    with pytest.raises(ValueError, match="without overlap"):
        DocumentTree(DOCUMENT_TREE_SCHEMA, _SHA, "sections", (_node("n2", "F", 1, (2,)), first))
    with pytest.raises(ValueError, match="ids must be unique"):
        DocumentTree(DOCUMENT_TREE_SCHEMA, _SHA, "sections", (first, _node("n1", "F", 1, (2,))))
    with pytest.raises(ValueError, match="at least one node"):
        DocumentTree(DOCUMENT_TREE_SCHEMA, _SHA, "sections", ())
    with pytest.raises(ValueError, match="sits at level one"):
        DocumentTree(DOCUMENT_TREE_SCHEMA, _SHA, "sections", (_node("n1", "F", 2, (0,)),))
    # A selection with a hole in it is not an overlap: the roots may skip unselected pages.
    gapped = DocumentTree(
        DOCUMENT_TREE_SCHEMA, _SHA, "sections", (first, _node("n2", "F", 1, (5, 6)))
    )
    assert gapped.pages == (0, 1, 5, 6)


def test_a_document_with_no_pages_or_no_printed_title_folds_into_no_tree() -> None:
    assert build_document_tree((), source_sha256=_SHA) is None
    untitled = tuple(
        _page(index, PageType.TEXT, _body("Nothing this page prints is a title"))
        for index in range(3)
    )
    assert build_document_tree(untitled, source_sha256=_SHA) is None


def test_a_page_index_appearing_twice_is_rejected() -> None:
    page = _page(0, PageType.TEXT, ("Group highlights",), title="Group highlights")
    with pytest.raises(ValueError, match="repeated pages"):
        build_document_tree((page, page), source_sha256=_SHA)


def test_summary_targets_are_exactly_the_nodes_with_children_in_document_order() -> None:
    tree = _built(_agenda_document())
    targets = summary_targets(tree)

    assert targets == tuple(node for node in tree.walk() if node.children)
    assert tuple(node.node_id for node in targets) == ("n0001", "n0006", "n0007", "n0010")
    assert all(not node.is_leaf for node in targets)
    assert not any(node.is_leaf for node in targets)


def test_with_summaries_attaches_by_node_id_and_leaves_the_structure_untouched() -> None:
    tree = _built(_agenda_document())
    annotated = with_summaries(
        tree,
        {
            "n0001": ("Group results and the distribution mix.", ("VONB", "Agency")),
            "n0006": ("Operating profit for the half.", ()),
            "n9999": ("A node this tree does not have.", ("ignored",)),
        },
    )

    reviewed = annotated.node("n0001")
    assert reviewed is not None
    assert reviewed.summary == "Group results and the distribution mix."
    assert reviewed.key_topics == ("VONB", "Agency")
    assert annotated.node("n9999") is None
    assert [(node.node_id, node.title, node.pages) for node in annotated.walk()] == [
        (node.node_id, node.title, node.pages) for node in tree.walk()
    ]
    assert [node.evidence for node in annotated.walk()] == [node.evidence for node in tree.walk()]
    assert [node.summary for node in tree.walk()] == [""] * len(tree.nodes)
    assert [node.node_id for node in annotated.walk() if node.summary] == ["n0001", "n0006"]


def test_pages_of_collects_the_named_nodes_pages_and_ignores_unknown_ids() -> None:
    tree = _built(_agenda_document())

    assert tree.pages_of(("n0006", "n9999")) == (4, 5)
    assert tree.pages_of(("n0010", "n0001")) == (0, 1, 2, 3, 6, 7)
    assert tree.pages_of(()) == ()


def test_render_tree_prints_one_based_page_labels_ids_titles_and_summaries() -> None:
    tree = with_summaries(
        _built(_agenda_document()), {"n0001": ("Group results.", ("VONB", "Agency"))}
    )
    lines = render_tree(tree).splitlines()

    assert lines[:3] == [
        "n0001 p1-4 Business review",
        "  summary: Group results.",
        "  topics: VONB; Agency",
    ]
    assert "  n0002 p1 ACME 2026 Interim Results" in lines
    assert "n0006 p5-6 Financial review" in lines
    assert "    n0009 p6 Operating profit" in lines
    assert len(lines) == len(tree.nodes) + 2


def test_render_tree_truncates_on_a_whole_line_within_the_character_budget() -> None:
    marker = "… (tree truncated)"
    tree = _built(_agenda_document())
    full = render_tree(tree)
    lines = full.splitlines()
    assert render_tree(tree, max_chars=len(full)) == full
    assert render_tree(tree, max_chars=len(full) + 1000) == full

    for count in range(1, len(lines)):
        # A budget that exactly fits the first whole lines — the marker has to fit inside it too.
        budget = len("\n".join(lines[:count]))
        truncated = render_tree(tree, max_chars=budget)
        printed = truncated.splitlines()

        assert len(truncated) <= budget
        if budget >= len(lines[0]) + len(marker) + 1:
            assert printed[-1] == marker
            assert printed[:-1] == lines[: len(printed) - 1]
            assert printed[:-1] != []
        else:
            # Too tight for a whole line plus the marker: the first line alone, cut to fit.
            assert truncated == lines[0][:budget]
    # Too small even for one line: the first line is cut, and the budget still holds.
    assert len(render_tree(tree, max_chars=5)) == 5


def test_render_tree_rejects_a_non_positive_character_budget() -> None:
    tree = _built(_agenda_document())
    for budget in (0, -1):
        with pytest.raises(ValueError, match="positive character budget"):
            render_tree(tree, max_chars=budget)
