"""A document's table of contents as a tree, folded deterministically from page metadata.

PageIndex (VectifyAI) reads a long document by reasoning over its table of contents
rather than over embeddings: build a hierarchy of sections once, then let a model pick
the branch a question belongs to and read those pages. ADR 0019 adopts that as a third
retrieval channel here, with one difference that the rest of this package insists on:
**the structure is deterministic, never a model's**. Every node's title is copied
verbatim from a page span, with the ADR 0013 evidence rule behind it.

Structure comes from two rules, in order:

1. **A contents page.** When a page is typed ``agenda``, each of its short lines is an
   entry, and an entry names a first-level node starting at the first later page whose
   title or section prints it. Entries are matched in page order, so ranges never cross.
2. **Dividers and section changes.** Otherwise a first-level node starts at a divider
   page (a page that prints a title and little else) or wherever the running ``section``
   header changes.

Underneath that, consecutive pages sharing one title form a second-level node, and a
second-level node covering more than one page prints one leaf per page. Every selected
page therefore belongs to exactly one leaf, and the leaves tile the document in order.

A node also carries a ``summary``, which is the one thing here a model writes
(``adapters/document_tree_extraction``). It exists **only to route a question to pages**:
it is never evidence, never indexed, never quoted, and never reaches an answer prompt.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise

from enterprise_pdf_rag.documents.models import TextSpan
from enterprise_pdf_rag.processing.page_metadata import (
    MetadataEvidence,
    MetadataValue,
    PageMetadata,
    PageType,
    fold_whitespace,
)

DOCUMENT_TREE_SCHEMA = "document-tree-v1"

# One line of a contents page: long enough to name a section, short enough not to be a
# sentence. Shorter lines are page numbers and rules; longer ones are body text.
MIN_ENTRY_CHARS = 3
MAX_ENTRY_CHARS = 80
# An entry shorter than this is matched by equality alone; substring matching on two or
# three characters would bind ``EV`` to every page whose title contains it.
MIN_SUBSTRING_ENTRY_CHARS = 5
# A contents page that names fewer than this many pages is not a contents page.
MIN_AGENDA_MATCHES = 2
# A divider page prints its section title and little else.
MAX_DIVIDER_SPANS = 8
# Page types whose pages are never read as dividers however few spans they carry.
_DENSE_PAGE_TYPES = frozenset({PageType.CHART, PageType.TABLE})

type TreeOrigin = str
"""How the first level was cut: ``agenda`` (a contents page) or ``sections``."""


@dataclass(frozen=True, slots=True)
class TreePage:
    """One selected page: its verified metadata and the spans that metadata quotes."""

    metadata: PageMetadata
    spans: tuple[TextSpan, ...] = ()

    @property
    def page_index(self) -> int:
        return self.metadata.page_index


@dataclass(frozen=True, slots=True)
class TreeNode:
    """One section of the document: a verbatim title, the pages it covers, its children.

    ``summary`` and ``key_topics`` are routing aids written by a model; they carry no
    evidence and may never be cited. ``evidence`` belongs to the *title*.
    """

    node_id: str
    title: str
    level: int
    pages: tuple[int, ...]
    children: tuple["TreeNode", ...] = ()
    evidence: MetadataEvidence | None = None
    summary: str = ""
    key_topics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise ValueError("A tree node needs an id")
        if not self.title.strip():
            raise ValueError(f"Tree node {self.node_id} needs a title")
        if self.level < 1:
            raise ValueError(f"Tree node {self.node_id} needs a level of at least one")
        if not self.pages:
            raise ValueError(f"Tree node {self.node_id} covers no page")
        if any(later <= earlier for earlier, later in pairwise(self.pages)):
            raise ValueError(f"Tree node {self.node_id} pages must ascend without repeating")
        if self.children:
            tiled = tuple(page for child in self.children for page in child.pages)
            if tiled != self.pages:
                raise ValueError(f"Tree node {self.node_id} children must tile its pages in order")
            if any(child.level != self.level + 1 for child in self.children):
                raise ValueError(f"Tree node {self.node_id} children must sit one level below it")

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def page_start(self) -> int:
        return self.pages[0]

    @property
    def page_end(self) -> int:
        return self.pages[-1]

    def walk(self) -> Iterator["TreeNode"]:
        """This node then its descendants, in document order."""
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True, slots=True)
class DocumentTree:
    """Every selected page of one document, tiled by a hierarchy of verbatim titles."""

    schema_version: str
    source_sha256: str
    origin: TreeOrigin
    roots: tuple[TreeNode, ...]
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.roots:
            raise ValueError("A document tree needs at least one node")
        if any(root.level != 1 for root in self.roots):
            raise ValueError("Every root of a document tree sits at level one")
        pages = tuple(page for root in self.roots for page in root.pages)
        if any(later <= earlier for earlier, later in pairwise(pages)):
            raise ValueError("Document tree roots must tile the pages in order, without overlap")
        ids = [node.node_id for node in self.walk()]
        if len(set(ids)) != len(ids):
            raise ValueError("Document tree node ids must be unique")

    def walk(self) -> Iterator[TreeNode]:
        """Every node, parents before children, in document order."""
        for root in self.roots:
            yield from root.walk()

    @property
    def nodes(self) -> tuple[TreeNode, ...]:
        return tuple(self.walk())

    @property
    def pages(self) -> tuple[int, ...]:
        return tuple(page for root in self.roots for page in root.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def node(self, node_id: str) -> TreeNode | None:
        return next((node for node in self.walk() if node.node_id == node_id), None)

    def pages_of(self, node_ids: Sequence[str]) -> tuple[int, ...]:
        """Every page the named nodes cover, ascending and deduplicated; unknown ids are ignored."""
        found = {page for node_id in node_ids for page in getattr(self.node(node_id), "pages", ())}
        return tuple(sorted(found))


def _title_value(page: TreePage) -> MetadataValue | None:
    return page.metadata.title


def _section_value(page: TreePage) -> MetadataValue | None:
    return page.metadata.section


def _key(value: MetadataValue | None) -> str:
    return fold_whitespace(value.text).casefold() if value is not None else ""


def _is_divider(page: TreePage) -> bool:
    """A page that prints a section title and little else — a deck's section slide."""
    if page.metadata.page_type in _DENSE_PAGE_TYPES:
        return False
    return len(page.spans) <= MAX_DIVIDER_SPANS and page.metadata.title is not None


def agenda_entries(page: TreePage) -> tuple[MetadataValue, ...]:
    """The contents lines of an ``agenda`` page, verbatim, in page order and deduplicated."""
    heading = _key(_title_value(page))
    entries: list[MetadataValue] = []
    for span in page.spans:
        text = fold_whitespace(span.text)
        if not MIN_ENTRY_CHARS <= len(text) <= MAX_ENTRY_CHARS:
            continue
        if not any(character.isalpha() for character in text):
            continue
        folded = text.casefold()
        if folded == heading or any(folded == _key(entry) for entry in entries):
            continue
        entries.append(MetadataValue(text, MetadataEvidence((span.span_id,), text)))
    return tuple(entries)


def _matches(entry: str, value: MetadataValue | None) -> bool:
    if value is None:
        return False
    candidate = fold_whitespace(value.text).casefold()
    if candidate == entry:
        return True
    if len(entry) < MIN_SUBSTRING_ENTRY_CHARS or len(candidate) < MIN_SUBSTRING_ENTRY_CHARS:
        return False
    return entry in candidate or candidate in entry


def _agenda_cuts(
    pages: Sequence[TreePage],
) -> tuple[tuple[int, MetadataValue], ...]:
    """First-level cuts named by a contents page: ``(position, title)``, in page order."""
    agenda = next(
        (
            (position, page)
            for position, page in enumerate(pages)
            if page.metadata.page_type is PageType.AGENDA and page.spans
        ),
        None,
    )
    if agenda is None:
        return ()
    position, page = agenda
    cuts: list[tuple[int, MetadataValue]] = []
    search = position + 1
    for entry in agenda_entries(page):
        folded = fold_whitespace(entry.text).casefold()
        match = next(
            (
                candidate
                for candidate in range(search, len(pages))
                if _matches(folded, _title_value(pages[candidate]))
                or _matches(folded, _section_value(pages[candidate]))
            ),
            None,
        )
        if match is None:
            continue
        cuts.append((match, entry))
        search = match + 1
    if len(cuts) < MIN_AGENDA_MATCHES:
        return ()
    # The contents page itself, and anything before it, belong to the first node.
    return ((0, cuts[0][1]), *cuts[1:]) if cuts[0][0] != 0 else tuple(cuts)


def _section_cuts(pages: Sequence[TreePage]) -> tuple[tuple[int, MetadataValue | None], ...]:
    """First-level cuts from divider pages and running-header changes."""
    cuts: list[tuple[int, MetadataValue | None]] = [
        (0, _section_value(pages[0]) or _title_value(pages[0]))
    ]
    running = _key(_section_value(pages[0]))
    for position, page in enumerate(pages[1:], start=1):
        section = _section_value(page)
        key = _key(section)
        changed = bool(key) and key != running
        if changed or _is_divider(page):
            value = section or _title_value(page)
            cuts.append((position, value))
            # A divider announces the section that follows it, so the header those pages
            # print is that same name repeated — not a change, and not a second cut.
            running = _key(value) or running
        elif key:
            running = key
    return tuple(cuts)


def _absorb(
    cuts: Sequence[tuple[int, MetadataValue | None]],
    fallback: MetadataValue | None,
) -> tuple[tuple[int, MetadataValue], ...]:
    """Drop a cut that names nothing: a titleless run belongs to the node before it."""
    titled: list[tuple[int, MetadataValue]] = []
    for position, value in cuts:
        if value is not None:
            titled.append((position, value))
        elif not titled and fallback is not None:
            titled.append((position, fallback))
    if titled and titled[0][0] != 0:
        titled[0] = (0, titled[0][1])
    return tuple(titled)


class _Counter:
    """Node ids in document order: ``n0001``, ``n0002``, …"""

    def __init__(self) -> None:
        self._next = 0

    def take(self) -> str:
        self._next += 1
        return f"n{self._next:04d}"


def _leaves(
    pages: Sequence[TreePage], level: int, parent: MetadataValue, counter: _Counter
) -> tuple[TreeNode, ...]:
    return tuple(
        TreeNode(
            counter.take(),
            (_title_value(page) or parent).text,
            level,
            (page.page_index,),
            evidence=(_title_value(page) or parent).evidence,
        )
        for page in pages
    )


def _titled_runs(pages: Sequence[TreePage]) -> tuple[tuple[int, int], ...]:
    """Maximal runs of consecutive pages printing one title; a titleless page continues its run."""
    runs: list[list[int]] = []
    running = ""
    for position, page in enumerate(pages):
        key = _key(_title_value(page))
        if not runs or (key and key != running):
            runs.append([position, position])
        else:
            runs[-1][1] = position
        if key:
            running = key
    return tuple((start, end) for start, end in runs)


def _branch(pages: Sequence[TreePage], title: MetadataValue, counter: _Counter) -> TreeNode:
    """One first-level node: its page-title groups, each printing one leaf per page."""
    node_id = counter.take()
    covered = tuple(page.page_index for page in pages)
    # One group of one page would only restate the node, so it prints no child at all.
    if len(pages) == 1:
        return TreeNode(node_id, title.text, 1, covered, (), title.evidence)
    children: list[TreeNode] = []
    for start, end in _titled_runs(pages):
        group = pages[start : end + 1]
        value = next((_title_value(page) for page in group if _title_value(page)), None) or title
        child_id = counter.take()
        grandchildren = _leaves(group, 3, value, counter) if len(group) > 1 else ()
        children.append(
            TreeNode(
                child_id,
                value.text,
                2,
                tuple(page.page_index for page in group),
                grandchildren,
                value.evidence,
            )
        )
    return TreeNode(node_id, title.text, 1, covered, tuple(children), title.evidence)


def build_document_tree(
    pages: Sequence[TreePage],
    *,
    source_sha256: str,
    display_title: MetadataValue | None = None,
) -> DocumentTree | None:
    """Fold verified page metadata into one table-of-contents tree; ``None`` without pages.

    No model runs here. ``display_title`` (the ADR 0013 document title, itself verbatim)
    is the last-resort name for a run of pages that prints no title of its own.
    """
    ordered = tuple(sorted(pages, key=lambda page: page.page_index))
    if not ordered:
        return None
    if len({page.page_index for page in ordered}) != len(ordered):
        raise ValueError("A document tree cannot be built from repeated pages")
    diagnostics: list[str] = []
    agenda = _agenda_cuts(ordered)
    origin: TreeOrigin = "agenda"
    if agenda:
        cuts = _absorb(agenda, display_title)
    else:
        origin = "sections"
        cuts = _absorb(_section_cuts(ordered), display_title)
    if not cuts:
        fallback = next(
            (_title_value(page) or _section_value(page) for page in ordered if _title_value(page)),
            None,
        )
        if fallback is None:
            diagnostics.append("no page prints a title; the tree covers the document as one node")
            return None
        cuts = ((0, fallback),)
    counter = _Counter()
    roots: list[TreeNode] = []
    for index, (start, title) in enumerate(cuts):
        end = cuts[index + 1][0] if index + 1 < len(cuts) else len(ordered)
        roots.append(_branch(ordered[start:end], title, counter))
    return DocumentTree(
        DOCUMENT_TREE_SCHEMA, source_sha256, origin, tuple(roots), tuple(diagnostics)
    )


def summary_targets(tree: DocumentTree) -> tuple[TreeNode, ...]:
    """The nodes a summary is written for: every node with children, in document order.

    A leaf is one page, which the retrieval channels already read; a summary would add
    nothing a page window does not already print.
    """
    return tuple(node for node in tree.walk() if not node.is_leaf)


def with_summaries(
    tree: DocumentTree, summaries: Mapping[str, tuple[str, tuple[str, ...]]]
) -> DocumentTree:
    """A copy of ``tree`` carrying the given routing summaries; unknown ids are ignored."""

    def annotate(node: TreeNode) -> TreeNode:
        summary, topics = summaries.get(node.node_id, (node.summary, node.key_topics))
        return replace(
            node,
            children=tuple(annotate(child) for child in node.children),
            summary=summary,
            key_topics=topics,
        )

    return replace(tree, roots=tuple(annotate(root) for root in tree.roots))


_TRUNCATION = "… (tree truncated)"


def _page_label(node: TreeNode) -> str:
    """The node's page range as a reader sees it printed: one-based and inclusive."""
    if node.page_start == node.page_end:
        return f"p{node.page_start + 1}"
    return f"p{node.page_start + 1}-{node.page_end + 1}"


def render_tree(tree: DocumentTree, *, max_chars: int = 12_000) -> str:
    """The tree as compact text for a routing prompt: id, page range, title, summary.

    Nothing here is evidence — it is a map of where to look, and the caller reads the
    pages themselves afterwards. Truncation stops on a whole line so no node is half
    described.
    """
    if max_chars < 1:
        raise ValueError("A rendered tree needs a positive character budget")
    lines: list[str] = []
    for node in tree.walk():
        indent = "  " * (node.level - 1)
        lines.append(f"{indent}{node.node_id} {_page_label(node)} {node.title}")
        if node.summary:
            lines.append(f"{indent}  summary: {node.summary}")
        if node.key_topics:
            lines.append(f"{indent}  topics: {'; '.join(node.key_topics)}")
    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered
    # The marker is part of what the caller asked to fit, so the budget pays for it too.
    budget = max_chars - len(_TRUNCATION) - 1
    kept: list[str] = []
    length = 0
    for line in lines:
        extra = len(line) + (1 if kept else 0)
        if length + extra > budget:
            break
        kept.append(line)
        length += extra
    if not kept:
        return lines[0][:max_chars]
    return "\n".join((*kept, _TRUNCATION))
