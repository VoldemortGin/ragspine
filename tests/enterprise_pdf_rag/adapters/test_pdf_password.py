"""Password-protected PDFs open with PDF_INGEST_PASSWORD, or fail loudly — never as empty pages."""

import re
from hashlib import sha256
from pathlib import Path

import pdfspine
import pytest

from enterprise_pdf_rag.adapters import pdf_password
from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.pdf_ingestion import ingest_pdf
from enterprise_pdf_rag.adapters.pdf_password import PdfPasswordError, open_pdf
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from ragspine.common.evidence.configs import ROOT_DIR, get_settings
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import authored_pdf

FIXTURES = ROOT_DIR / "tests/enterprise_pdf_rag/fixtures/encrypted"
# The fixtures' synthetic user password (see the fixture README); distinctive on purpose, so a
# search for it anywhere it must not appear cannot match by accident.
PASSWORD = "zq-synthetic-pdf-pw-7Q4"
OWNER_PASSWORD = "zq-synthetic-owner-pw-3K9"
LABEL = "Vesper 1H26 Hong Kong"
# Object streams (what real producers write): pdfspine sees no pages before authentication.
OBJECT_STREAM_FIXTURES = ("aes256-objstm.pdf", "rc4-128-objstm.pdf")


def set_password(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("PDF_INGEST_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("PDF_INGEST_PASSWORD", value)
    get_settings.cache_clear()


def encrypted_pdf(
    path: Path,
    *,
    method: int = pdfspine.PDF_ENCRYPT_AES_256,
    user_password: str = PASSWORD,
    page_count: int = 3,
) -> Path:
    """An authored PDF saved encrypted by pdfspine itself (no object streams)."""
    plain = authored_pdf(path, page_count=page_count, label=LABEL, embedded_font=True)
    with pdfspine.open(stream=plain.read_bytes(), filetype="pdf") as document:
        data = document.tobytes(encryption=method, user_pw=user_password, owner_pw=OWNER_PASSWORD)
    path.write_bytes(data)
    return path


def _sources(tmp_path: Path) -> dict[str, bytes]:
    pdfspine_made = {
        f"pdfspine-{name}": encrypted_pdf(tmp_path / f"{name}.pdf", method=method).read_bytes()
        for name, method in (
            ("aes256", pdfspine.PDF_ENCRYPT_AES_256),
            ("rc4-128", pdfspine.PDF_ENCRYPT_RC4_128),
        )
    }
    committed = {name: (FIXTURES / name).read_bytes() for name in OBJECT_STREAM_FIXTURES}
    return pdfspine_made | committed


def source_span_texts(source_store: Path, manifest_id: str) -> list[str]:
    """Every saved text span of an ingested source, in page order."""
    sources = LocalDocumentStore(source_store)
    snapshot = sources.load(manifest_id)
    return [
        span.text
        for index in range(len(snapshot.manifest.pages))
        for span in read_text_sidecar(sources, snapshot, index).spans
    ]


def _page_text(document: pdfspine.Document) -> list[str]:
    return [str(document.load_page(index).get_text()) for index in range(document.page_count)]


# ---- the shared open helper --------------------------------------------------------------


@pytest.mark.parametrize("configured", [None, PASSWORD])
def test_an_unencrypted_pdf_opens_the_same_with_or_without_a_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    set_password(monkeypatch, configured)
    data = authored_pdf(tmp_path / "plain.pdf", page_count=2, label=LABEL).read_bytes()

    with open_pdf(data) as document:
        assert _page_text(document) == [f"{LABEL} page 1\n", f"{LABEL} page 2\n"]


def test_an_owner_only_pdf_opens_without_a_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_password(monkeypatch, None)
    data = encrypted_pdf(tmp_path / "owner.pdf", user_password="", page_count=1).read_bytes()

    with open_pdf(data) as document:
        assert _page_text(document) == [f"{LABEL} page 1\n"]


def test_every_encrypted_source_opens_with_the_configured_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_password(monkeypatch, PASSWORD)
    for name, data in _sources(tmp_path).items():
        with open_pdf(data) as document:
            assert _page_text(document) == [f"{LABEL} page {n}\n" for n in (1, 2, 3)], name


def test_without_a_password_an_encrypted_pdf_is_refused_with_the_setting_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_password(monkeypatch, None)
    for name, data in _sources(tmp_path).items():
        with pytest.raises(PdfPasswordError, match="password-protected") as caught:
            open_pdf(data)
        assert "PDF_INGEST_PASSWORD" in str(caught.value), name
        assert isinstance(caught.value, ValueError)


def test_a_wrong_password_is_refused_without_echoing_either_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = "zq-wrong-pdf-pw-5M1"
    set_password(monkeypatch, wrong)
    for name, data in _sources(tmp_path).items():
        with pytest.raises(PdfPasswordError, match="does not open") as caught:
            open_pdf(data)
        message = str(caught.value) + repr(caught.value)
        assert "PDF_INGEST_PASSWORD" in message, name
        assert wrong not in message and PASSWORD not in message


def test_every_pdfspine_open_in_the_package_goes_through_the_helper() -> None:
    """A direct ``pdfspine.open(stream=...)`` would read an encrypted source as empty pages."""
    package = ROOT_DIR / "src" / "enterprise_pdf_rag"
    direct = [
        f"{path.relative_to(package)}:{number}"
        for path in sorted(package.rglob("*.py"))
        if path.name != "pdf_password.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"pdfspine\.open\(\s*stream", line)
    ]
    assert direct == []
    assert "pdfspine.open(stream=" in Path(pdf_password.__file__).read_text(encoding="utf-8")


# ---- ingest ------------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", OBJECT_STREAM_FIXTURES)
def test_ingest_reads_the_decrypted_text_and_keeps_the_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: str
) -> None:
    set_password(monkeypatch, PASSWORD)
    pdf = FIXTURES / fixture

    result = ingest_pdf(pdf=pdf, output_dir=tmp_path / "ingestion")

    data = pdf.read_bytes()
    assert result.source_sha256 == sha256(data).hexdigest()
    assert result.source_page_count == 3
    sources = LocalDocumentStore(Path(result.source_store))
    source = sources.load(result.source_manifest_id)
    assert source_span_texts(Path(result.source_store), result.source_manifest_id) == [
        f"{LABEL} page {n}" for n in (1, 2, 3)
    ]
    # The store keeps the encrypted original, never a decrypted copy.
    assert sources.read_content(source.manifest.source.sha256) == data
    _assert_absent(PASSWORD, tmp_path / "ingestion")


def test_ingest_without_a_password_fails_before_writing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_password(monkeypatch, None)
    output = tmp_path / "ingestion"
    for fixture in OBJECT_STREAM_FIXTURES:
        with pytest.raises(ValueError, match="PDF_INGEST_PASSWORD"):
            ingest_pdf(pdf=FIXTURES / fixture, output_dir=output)
    pdf = encrypted_pdf(tmp_path / "pdfspine-encrypted.pdf")
    with pytest.raises(ValueError, match="PDF_INGEST_PASSWORD"):
        ingest_pdf(pdf=pdf, output_dir=output)
    assert not output.exists()


def test_the_page_adapter_extracts_the_decrypted_page_and_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_password(monkeypatch, PASSWORD)
    data = (FIXTURES / "aes256-objstm.pdf").read_bytes()
    adapter = PdfspineDocumentAdapter()

    extraction = adapter.extract_document(data)
    region = adapter.extract_region(data, page_index=1, bbox=(0.0, 0.0, 240.0, 160.0))

    assert [span.text for page in extraction.pages for span in page.text_spans] == [
        f"{LABEL} page {n}" for n in (1, 2, 3)
    ]
    assert [span.text for span in region.text_spans] == [f"{LABEL} page 2"]


def _assert_absent(needle: str, root: Path) -> None:
    encoded = needle.encode()
    leaked = [
        str(path) for path in root.rglob("*") if path.is_file() and encoded in path.read_bytes()
    ]
    assert leaked == []
