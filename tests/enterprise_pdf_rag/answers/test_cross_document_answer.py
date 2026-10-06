"""One question answered across every mounted document (ADR 0032).

The corpus is the union of the documents, but every member is read, verified and cited
from its own document: a claim can only stand on the evidence of the document it names.
"""

from pathlib import Path

from enterprise_pdf_rag.adapters.answer_service import AnswerService
from enterprise_pdf_rag.adapters.cross_document import CrossDocument
from enterprise_pdf_rag.adapters.tree_retrieval import TreeRouteDTO
from enterprise_pdf_rag.answers.models import AbstainReason, AnswerRequest, AnswerStatus
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from ragspine.extraction.evidence.metadata.document_tree import (
    DOCUMENT_TREE_SCHEMA,
    DocumentTree,
    TreeNode,
)
from tests.enterprise_pdf_rag.answers.fake_document import FakeDocument, FakeMember
from tests.enterprise_pdf_rag.answers.fake_llm import (
    Router,
    Script,
    answered,
    declined,
    scripted_client,
)

_A = "a" * 64
_B = "b" * 64
_QUESTION = "What was the revenue in the revenue table for FY2024, and how did it compare?"
_LABELS = {_A: "meridian.pdf", _B: "orion.pdf"}


class _Document(FakeDocument):
    """A fake document with its own identity, so two of them can be mounted side by side."""

    def __init__(
        self, sha: str, members: tuple[FakeMember, ...], vector_order: tuple[str, ...]
    ) -> None:
        super().__init__(members, vector_order, snapshot_id="snapshot-" + sha[:8])
        self._sha = sha

    @property
    def source_sha256(self) -> str:
        return self._sha


def _meridian() -> _Document:
    return _Document(
        _A,
        (
            FakeMember("a-rev", "Revenue table FY2024 revenue 100", page_index=0),
            FakeMember("a-note", "Meridian staff headcount grew", page_index=0),
            FakeMember("a-other", "Meridian closing remarks", page_index=1),
        ),
        ("a-rev", "a-other", "a-note"),
    )


def _orion() -> _Document:
    return _Document(
        _B,
        (
            FakeMember("b-rev", "Revenue table FY2024 revenue 200", page_index=0),
            FakeMember("b-note", "Orion office leases renewed", page_index=0),
            FakeMember("b-other", "Orion closing remarks", page_index=1),
        ),
        ("b-rev", "b-other", "b-note"),
    )


def _service(
    tmp_path: Path,
    script: Script,
    *documents: FakeDocument,
    router: Router | None = None,
    trees: dict[str, DocumentTree] | None = None,
    max_live_calls: int = 1,
) -> tuple[AnswerService, list[str]]:
    client, prompts = scripted_client(
        tmp_path / "llm", script, max_live_calls=max_live_calls, router=router
    )
    service = AnswerService(
        {document.source_sha256: document for document in documents},
        client,
        labels=_LABELS,
        trees=trees,
    )
    return service, prompts


def _quote(member_id: str, text: str) -> ModelClaim:
    return ModelClaim(
        claim_id="c1",
        member_id=member_id,
        kind="quote",
        field_path=f"fragments.{member_id}-span",
        text=text,
    )


def _claim(answer: str, member_id: str, text: str) -> Script:
    def script(prompt: str) -> ModelAnswer:
        return answered(answer, _quote(member_id, text))

    return script


def test_a_claim_is_cited_to_the_document_whose_evidence_holds_it(tmp_path: Path) -> None:
    meridian, orion = _meridian(), _orion()
    service, prompts = _service(
        tmp_path, _claim("Revenue was 200.", "b-rev", "revenue 200"), meridian, orion
    )

    result = service.answer(AnswerRequest(_QUESTION, cross_document=True))

    assert result.status is AnswerStatus.ANSWERED
    (claim,) = result.claims
    (citation,) = claim.citations
    assert (citation.member_id, citation.document_sha256) == ("b-rev", _B)
    assert result.searched_documents == (_A, _B)
    # Both same-titled tables reached the one prompt, each naming its own document.
    (prompt,) = prompts
    assert "document=meridian.pdf (aaaaaaaaaaaa)" in prompt
    assert "document=orion.pdf (bbbbbbbbbbbb)" in prompt
    assert {"a-rev", "b-rev"} <= set(result.member_ids)
    # Every fused hit is pinned back to its own document and snapshot.
    for hit in result.fused:
        owner = meridian if hit.member_id.startswith("a-") else orion
        assert (hit.document_sha256, hit.snapshot_id) == (
            owner.source_sha256,
            owner.retrieval_snapshot_id,
        )
    # The result names the first prompt member's document; its evidence was read there only.
    assert result.document_sha256 == (_A if result.member_ids[0].startswith("a-") else _B)
    assert "b-rev" in orion.resolved and "b-rev" not in meridian.resolved


def test_a_value_from_the_other_documents_table_is_rejected_not_cross_cited(
    tmp_path: Path,
) -> None:
    """Two documents print a table under the same name with different values: a claim
    citing one document's member with the other document's figure never verifies."""
    for member_id, text, folder in (
        ("b-rev", "revenue 100", "borrowed-from-a"),
        ("a-rev", "revenue 200", "borrowed-from-b"),
    ):
        service, _ = _service(
            tmp_path / folder,
            _claim(f"Revenue was {text.split()[-1]}.", member_id, text),
            _meridian(),
            _orion(),
        )
        result = service.answer(AnswerRequest(_QUESTION, cross_document=True))
        assert result.status is AnswerStatus.ABSTAINED, member_id
        assert result.claims == ()
        (rejected,) = result.rejected
        assert (rejected.member_id, rejected.reason) == (
            member_id,
            AbstainReason.CLAIM_NOT_IN_EVIDENCE,
        )


def test_page_context_never_mixes_two_documents_that_share_a_page_number(
    tmp_path: Path,
) -> None:
    service, prompts = _service(tmp_path, lambda prompt: declined(), _meridian(), _orion())

    result = service.answer(AnswerRequest(_QUESTION, cross_document=True, top_k=2))

    assert set(result.member_ids) == {"a-rev", "b-rev"}
    (prompt,) = prompts
    pages = [part for part in prompt.split("\n\n") if part.startswith("[page_context")]
    assert len(pages) == 2
    by_document = {page.splitlines()[0]: page for page in pages}
    meridian = by_document["[page_context page_index=0] document=meridian.pdf (aaaaaaaaaaaa)"]
    orion = by_document["[page_context page_index=0] document=orion.pdf (bbbbbbbbbbbb)"]
    assert "Meridian staff" in meridian and "Orion" not in meridian
    assert "Orion office" in orion and "Meridian" not in orion
    assert {window.document_sha256 for window in result.page_windows} == {_A, _B}


def test_each_channel_offers_no_more_candidates_than_for_one_document(tmp_path: Path) -> None:
    meridian, orion = _meridian(), _orion()
    service, prompts = _service(tmp_path, lambda prompt: declined(), meridian, orion)

    result = service.answer(AnswerRequest(_QUESTION, cross_document=True, top_k=1, channel_limit=1))

    # Each document hands over its own top ``channel_limit``; the merge is cut back to it.
    assert meridian.search_calls == [(_QUESTION, 1)] and orion.search_calls == [(_QUESTION, 1)]
    assert len(result.fused) <= 2  # one vector + one BM25 candidate at most
    assert len(result.member_ids) == 1 and len(prompts) == 1


def test_an_answer_in_no_document_abstains(tmp_path: Path) -> None:
    service, prompts = _service(tmp_path, lambda prompt: declined(), _meridian(), _orion())

    result = service.answer(AnswerRequest(_QUESTION, cross_document=True))

    assert result.status is AnswerStatus.ABSTAINED
    assert result.answer is None and result.claims == ()
    assert result.abstain_reason is AbstainReason.MODEL_DECLINED
    assert result.searched_documents == (_A, _B) and len(prompts) == 1


def test_one_mounted_document_answers_exactly_as_without_the_switch(tmp_path: Path) -> None:
    script = _claim("Revenue was 100.", "a-rev", "revenue 100")
    plain, _ = _service(tmp_path / "plain", script, _meridian())
    across, _ = _service(tmp_path / "across", script, _meridian())

    expected = plain.answer(AnswerRequest(_QUESTION))
    observed = across.answer(AnswerRequest(_QUESTION, cross_document=True))

    assert observed == expected and observed.searched_documents == ()


def test_a_member_both_documents_hold_byte_for_byte_is_read_once_from_the_first(
    tmp_path: Path,
) -> None:
    shared = FakeMember("shared", "Identical disclaimer text", page_index=0)
    first = _Document(_A, (shared, FakeMember("a-rev", "revenue 100")), ("shared",))
    second = _Document(_B, (shared, FakeMember("b-rev", "revenue 200")), ("shared",))
    corpus = CrossDocument((second, first))

    ids = [member.member_id for member in corpus.member_texts()]
    assert ids == sorted(ids) and ids.count("shared") == 1
    assert corpus.owner("shared") is first and corpus.document_ids == (_A, _B)
    assert corpus.members_of(second) == frozenset({"b-rev"})


def test_the_tree_of_the_leading_document_routes_and_only_its_pages_join(
    tmp_path: Path,
) -> None:
    routes: list[str] = []

    def route(prompt: str) -> TreeRouteDTO:
        routes.append(prompt)
        return TreeRouteDTO(node_ids=("n1",), pages=(), rationale="the revenue section")

    tree = DocumentTree(
        DOCUMENT_TREE_SCHEMA,
        _B,
        "sections",
        (TreeNode("n1", "Revenue", 1, (0,)), TreeNode("n2", "Closing", 1, (1,))),
    )
    service, _ = _service(
        tmp_path,
        lambda prompt: declined(),
        _meridian(),
        _orion(),
        router=route,
        trees={_B: tree},
        max_live_calls=2,
    )
    question = "What did Orion say in its closing remarks about how the reporting year went?"

    result = service.answer(AnswerRequest(question, cross_document=True, tree_route=True))

    # One routing call, over the outline of the document owning the best BM25 member.
    assert len(routes) == 1 and result.tree_route is not None
    routed = [hit for hit in result.fused if hit.tree_rank is not None]
    assert routed and {hit.document_sha256 for hit in routed} == {_B}
    # Meridian prints a page 0 too; the routed page is Orion's alone.
    assert {hit.member_id for hit in routed} == {"b-rev", "b-note"}
