"""BM25 over scoring units: a member scores as its best unit, an unscored member never."""

from enterprise_pdf_rag.adapters.hybrid_search import build_lexical_index, lexical_rank
from enterprise_pdf_rag.answers.ports import MemberText
from ragspine.extraction.evidence.page.models import ObjectKind
from ragspine.retrieval.lexical.retrieval import bm25_scores, tokenize
from tests.enterprise_pdf_rag.adapters.test_hybrid_search import _FakeDocument

_UNITS = ("Item 2024 2023\nRevenue 1,234 1,100", "Item 2024 2023\nCost of sales (456) (400)")
_MEMBERS = (
    MemberText("m-header", ObjectKind.TEXT, 0, "Acme Interim Report 2024", units=()),
    MemberText("m-prose", ObjectKind.TEXT, 0, "Revenue grew strongly over the year"),
    MemberText("m-table", ObjectKind.TABLE, 0, "\n".join(_UNITS), units=_UNITS),
)


def test_a_member_without_units_is_byte_for_byte_the_one_unit_index() -> None:
    plain = build_lexical_index(_FakeDocument(()))
    assert plain.owners == ()


def test_a_member_scores_as_its_best_unit_and_an_unscored_member_scores_nothing() -> None:
    index = build_lexical_index(_FakeDocument((), members=_MEMBERS))
    assert index.member_ids == ("m-header", "m-prose", "m-table")
    # The corpus is the units: the prose, then the table's two rows; the header adds none.
    corpus = [tokenize(_MEMBERS[1].text), *(tokenize(unit) for unit in _UNITS)]
    assert [list(tokens) for tokens in index.docs_tokens] == corpus
    assert index.owners == (1, 2, 2)

    ranked = lexical_rank(index, "Revenue 2024", limit=10)
    expected = bm25_scores(tokenize("Revenue 2024"), corpus)
    assert [(hit.member_id, hit.score) for hit in ranked] == [
        ("m-table", max(expected[1], expected[2])),
        ("m-prose", expected[0]),
    ]
    assert "m-header" not in {hit.member_id for hit in lexical_rank(index, "Acme 2024", limit=9)}
    assert lexical_rank(index, "Interim", limit=9) == ()
