"""Within a ``shared_pdfs()`` scope a source PDF is read and opened once for every proof.

ADR 0024 (source verification cache): table, formula and chart proofs each re-observe the pinned
PDF; inside a scope they share one verified read and one opened (decrypted) document, closed when
the scope ends. Outside a scope every proof opens and closes its own, exactly as before.
"""

from pathlib import Path

import pdfspine
import pytest

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import publish_draft
from enterprise_pdf_rag.adapters.shared_pdf import opened_pdf, shared_pdfs
from tests.enterprise_pdf_rag.adapters.test_source_verification_cache import _published, _Reads


def test_formula_members_open_their_source_pdf_once_per_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opens: list[int] = []
    real_open = pdfspine.open

    def counting_open(*args: object, **kwargs: object) -> pdfspine.Document:
        opens.append(1)
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    published = _published(tmp_path, monkeypatch, page_count=2, formula_page=True)
    monkeypatch.setattr(pdfspine, "open", counting_open)
    reads = _Reads(monkeypatch)
    store = LocalDocumentStore(
        Path(published.source_store), activate_on_publish=False, persisted_receipts=False
    )
    source = store.load(published.source_manifest_id).manifest.source

    publish_draft(
        source_store=Path(published.source_store),
        processing_store=Path(published.processing_store),
        processing_id=published.published_processing_id,
    )

    assert len(opens) == 1
    # Once by ``store.load`` above, once by the publication's own source verification and once
    # for the two formula proofs together.
    assert reads.paths[store.asset_path(source)] == 3


def test_shared_pdfs_opens_identical_bytes_once_and_closes_them_with_the_scope(
    tmp_path: Path,
) -> None:
    with pdfspine.open() as document:
        document.new_page(width=100, height=100)
        pdf = document.tobytes()

    with opened_pdf(pdf) as first, opened_pdf(pdf) as second:
        assert first is not second
    with shared_pdfs():
        with opened_pdf(pdf) as first:
            pass
        with shared_pdfs(), opened_pdf(bytes(pdf)) as second:
            assert second is first
        assert first.page_count == 1  # still open: the nested scope does not close it
    with pytest.raises(Exception):  # noqa: B017 - pdfspine reports a closed document its own way
        first.load_page(0)
